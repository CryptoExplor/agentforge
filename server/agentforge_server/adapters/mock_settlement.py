"""Mock settlement provider - preserves existing local escrow behavior.

This is the concrete implementation behind the SettlementProvider boundary.
It contains the exact mock escrow semantics previously implemented directly
in ``services.py``:

- FULL_RELEASE, PARTIAL_RELEASE, REFUND, SLASH transitions
- Decimal/string accounting
- Ledger idempotency keys (task:{id}:fund, task:{id}:release:{decision},
  task:{id}:refund:{decision}, task:{id}:slash)
- Append-only audit events with transition, amounts, mock_burn destination
- mock_burn slash behavior: requester-subject slash credits no account
- Terminal escrow exclusivity (FUNDED/FROZEN -> terminal only)
- Conservation invariant: executor_release + requester_refund + slash == reserved_total
- Private balance authorization is enforced at the API layer, not here
- No external calls, no FLOP contracts, no TCLK, no airdrop logic

The provider is stateless and operates on the given SQLAlchemy session.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Escrow, Task
from ..services import (
    ZERO,
    add_audit,
    dec,
    ensure_account,
    money_string,
    now,
    post_ledger_event,
)


class MockSettlementProvider:
    """Local mock ledger - the only provider in the pre-testnet MVP."""

    def fund(self, db: Session, task: Task) -> Escrow | None:
        economics = task.economics or {}
        reward = economics.get("reward") or {}
        deposit = economics.get("security_deposit") or {}
        inference = economics.get("inference_budget") or {}
        asset = str(reward.get("asset", "MOCK"))

        amounts = [
            dec(reward.get("amount", "0")),
            dec(deposit.get("amount", "0")),
            dec(inference.get("amount", "0")),
        ]
        for item, amount in [(deposit, amounts[1]), (inference, amounts[2])]:
            if amount > ZERO and str(item.get("asset", asset)) != asset:
                raise ValueError("all funded MVP escrow amounts must use the reward asset")

        total = sum(amounts, ZERO)
        if total == ZERO:
            task.status = "OPEN"
            return None

        post_ledger_event(
            db,
            did=task.poster_did,
            asset=asset,
            delta=-total,
            reason="TASK_FUND",
            idempotency_key=f"task:{task.id}:fund",
            task_id=task.id,
        )
        escrow = Escrow(
            task_id=task.id,
            payer_did=task.poster_did,
            asset=asset,
            reward_amount=money_string(amounts[0]),
            deposit_amount=money_string(amounts[1]),
            inference_budget=money_string(amounts[2]),
            reserved_total=money_string(total),
            status="FUNDED",
            created_at=now(),
            updated_at=now(),
        )
        db.add(escrow)
        task.status = "FUNDED"
        return escrow

    def settle(
        self,
        db: Session,
        *,
        task: Task,
        executor_did: str,
        decision: str,
        settlement: dict[str, Any] | None = None,
    ) -> Escrow | None:
        escrow = db.scalar(select(Escrow).where(Escrow.task_id == task.id))
        if not escrow:
            return None
        if escrow.status not in {"FUNDED", "FROZEN"}:
            raise ValueError(f"escrow is already terminal: {escrow.status}")

        reward = dec(escrow.reward_amount)
        deposit = dec(escrow.deposit_amount)
        inference = dec(escrow.inference_budget)
        settlement = settlement or {}
        slashed: Decimal = ZERO
        slash_subject: str | None = None
        transition = ""

        if decision == "VERIFIED":
            transition = "FULL_RELEASE"
            executor_amount = reward
            requester_refund = deposit + inference
            escrow.status = "RELEASED"
        elif decision == "REJECTED":
            transition = "REFUND"
            executor_amount = ZERO
            requester_refund = reward + deposit + inference
            escrow.status = "REFUNDED"
        elif decision == "PARTIAL":
            transition = "PARTIAL_RELEASE"
            executor_amount = dec(settlement.get("executor_amount", "0"))
            if executor_amount > reward:
                raise ValueError("partial executor amount exceeds reward")
            requester_refund = reward - executor_amount + deposit + inference
            escrow.status = "PARTIAL"
        elif decision == "SLASHED":
            transition = "SLASH"
            slash_subject = settlement.get("slash_subject", "requester")
            if slash_subject not in {"requester", "executor"}:
                raise ValueError("slash_subject must be requester or executor")
            executor_amount = ZERO
            # MVP has requester collateral only. If the executor is the subject,
            # the reward and requester deposit are refunded; executor collateral can
            # be added by a future adapter. A requester-subject slash burns the
            # deposit to the documented mock destination: no account is credited.
            requester_refund = reward + inference
            if slash_subject == "executor":
                requester_refund += deposit
            slashed = deposit if slash_subject == "requester" else ZERO
            escrow.status = "SLASHED"
        else:
            raise ValueError("unsupported settlement decision")

        if executor_amount + requester_refund + slashed != dec(escrow.reserved_total):
            raise ValueError("escrow settlement does not conserve reserved value")

        if executor_amount:
            post_ledger_event(
                db,
                did=executor_did,
                asset=escrow.asset,
                delta=executor_amount,
                reason=transition,
                idempotency_key=f"task:{task.id}:release:{decision}",
                task_id=task.id,
            )
        if requester_refund:
            post_ledger_event(
                db,
                did=escrow.payer_did,
                asset=escrow.asset,
                delta=requester_refund,
                reason=transition,
                idempotency_key=f"task:{task.id}:refund:{decision}",
                task_id=task.id,
            )
        if decision == "SLASHED":
            # The mock ledger burns a requester-subject slash by leaving it
            # uncredited. The zero-delta event records the destination and remains
            # idempotent even when the collateral amount is zero.
            post_ledger_event(
                db,
                did=escrow.payer_did,
                asset=escrow.asset,
                delta=ZERO,
                reason="SLASH",
                idempotency_key=f"task:{task.id}:slash",
                task_id=task.id,
            )

        add_audit(
            db,
            actor_did=None,
            kind="ESCROW_TRANSITION",
            aggregate_type="task",
            aggregate_id=task.id,
            payload={
                "transition": transition,
                "decision": decision,
                "executor_amount": money_string(executor_amount),
                "requester_refund": money_string(requester_refund),
                "slashed_amount": money_string(slashed),
                "slash_subject": slash_subject,
                "slash_destination": "mock_burn" if slashed > ZERO else "none",
            },
        )
        escrow.released_amount = money_string(executor_amount)
        escrow.refunded_amount = money_string(requester_refund)
        escrow.slashed_amount = money_string(slashed)
        escrow.updated_at = now()
        return escrow
