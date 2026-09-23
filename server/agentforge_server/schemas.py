"""Public API schemas. These models are intentionally small and JSON-first."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CapabilityInput(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    level: int = Field(default=1, ge=1, le=10)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentManifest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    capabilities: list[str | CapabilityInput] = Field(default_factory=list, max_length=100)
    chains: list[str] = Field(default_factory=list, max_length=100)
    endpoint_mode: Literal["outbound_events", "https", "local"] = "outbound_events"
    operator_group: str | None = Field(default=None, max_length=160)
    infrastructure_group: str | None = Field(default=None, max_length=160)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegistrationRequest(StrictModel):
    challenge_id: str = Field(min_length=1, max_length=80)
    nonce: str = Field(min_length=1, max_length=120)
    did: str = Field(min_length=10, max_length=200)
    manifest: AgentManifest
    signature: str = Field(min_length=20, max_length=300)


class Money(StrictModel):
    amount: str = "0"
    asset: str = Field(default="MOCK", max_length=32)

    @field_validator("amount")
    @classmethod
    def numeric_string(cls, value: str) -> str:
        if len(value) > 80:
            raise ValueError("amount is too long")
        try:
            from decimal import Decimal

            number = Decimal(value)
        except Exception as exc:
            raise ValueError("amount must be a decimal string") from exc
        if not number.is_finite() or number < 0:
            raise ValueError("amount must be finite and non-negative")
        # Bound exponent before formatting; a short '1e999999999' must not
        # expand into an enormous string during request parsing.
        if abs(number.as_tuple().exponent) > 80 or abs(number.adjusted()) > 80:
            raise ValueError("decimal exponent is out of bounds")
        normalized = format(number, "f")
        if len(normalized) > 80:
            raise ValueError("normalized decimal is too long")
        return normalized


class TaskEconomics(StrictModel):
    mode: Literal["REPUTATION", "BOUNTY", "SERVICE"] = "REPUTATION"
    reward: Money = Field(default_factory=Money)
    agentforge_service_fee: Money = Field(default_factory=Money)
    security_deposit: Money = Field(default_factory=Money)
    inference_budget: Money = Field(default_factory=Money)


class DemandProvenance(StrictModel):
    type: Literal[
        "external_request",
        "external_event",
        "agent_service_request",
        "ecosystem_requirement",
        "research_question",
        "validation_request",
        "maintenance",
        "synthetic_demo",
    ] = "agent_service_request"
    level: int = Field(default=0, ge=0, le=3)
    source: str | None = Field(default=None, max_length=200)
    source_adapter: str | None = Field(default=None, max_length=120)
    source_ref: str | None = Field(default=None, max_length=1000)
    observed_at: float | None = None
    novelty_hash: str | None = Field(default=None, max_length=128)
    attestation: str | None = Field(default=None, max_length=1000)


class GenerationPolicy(StrictModel):
    economic_eligibility: Literal[
        "NOT_ELIGIBLE",
        "DEMO_ONLY",
        "REPUTATION_ELIGIBLE",
        "ECONOMIC_ELIGIBLE",
        "EXTERNAL_NETWORK_VERIFIED",
    ] = "REPUTATION_ELIGIBLE"
    minimum_provenance_level: int = Field(default=1, ge=0, le=3)
    synthetic_demo: bool = False


class TaskCreate(StrictModel):
    kind: Literal["deterministic", "expert", "research", "service"] = "research"
    visibility: Literal["public", "private"] = "public"
    origin: Literal["external", "ecosystem", "agent_service", "research", "validation", "maintenance"] = "agent_service"
    #: Who is allowed to decide the task outcome.
    #:
    #: - ``deterministic``: the server evaluates the declarative acceptance
    #:   criteria (hashes, schemas, evidence) inside the submission transaction
    #:   and settles immediately. No validator is required or accepted.
    #: - ``peer_review``: an approval-listed independent validator must submit a
    #:   signed validation decision (the original MVP behavior, and the default).
    #: - ``operator``: reserved for operator-driven review; treated like
    #:   ``peer_review`` today.
    verification_strategy: Literal["deterministic", "peer_review", "operator"] = "peer_review"
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)
    chains: list[str] = Field(default_factory=list, max_length=20)
    input_data: dict[str, Any] = Field(default_factory=dict, alias="input")
    acceptance: dict[str, Any] = Field(default_factory=dict)
    demand_provenance: DemandProvenance = Field(default_factory=DemandProvenance)
    generation_policy: GenerationPolicy = Field(default_factory=GenerationPolicy)
    economics: TaskEconomics = Field(default_factory=TaskEconomics)
    parent_task_id: str | None = Field(default=None, max_length=80)
    deadline: float | None = Field(default=None, gt=0)


class EscrowView(StrictModel):
    """Escrow state attached to a task representation."""

    task_id: str
    status: str
    transition: str | None = None
    asset: str
    reward_amount: str = "0"
    deposit_amount: str = "0"
    inference_budget: str = "0"
    reserved_total: str = "0"
    released_amount: str = "0"
    refunded_amount: str = "0"
    slashed_amount: str = "0"


class TaskResponse(StrictModel):
    """Documentation model for the task representation returned by the API.

    The HTTP layer returns plain dictionaries (``task_view``); this schema
    mirrors that representation, including ``verification_strategy``, so the
    protocol contract and any client can see the exact field set.
    """

    id: str
    version: int = 1
    kind: str
    visibility: str
    origin: str
    poster_did: str
    required_capabilities: list[str] = Field(default_factory=list)
    chains: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict, alias="input")
    acceptance: dict[str, Any] = Field(default_factory=dict)
    demand_provenance: dict[str, Any] = Field(default_factory=dict)
    generation_policy: dict[str, Any] = Field(default_factory=dict)
    anti_circularity: dict[str, Any] = Field(default_factory=dict)
    economics: dict[str, Any] = Field(default_factory=dict)
    deadline: float | None = None
    status: str
    activity_eligibility: str = "NOT_ELIGIBLE"
    acceptance_hash: str
    task_hash: str
    #: Server-recorded verification strategy for this task.
    verification_strategy: str = "peer_review"
    escrow: EscrowView | None = None
    created_at: float
    updated_at: float


class InferenceRequestCreate(StrictModel):
    provider: Literal["mock", "local", "nvidia", "openrouter", "flop"] = "mock"
    model_ref: str = Field(default="mock:model-v1", min_length=1, max_length=500)
    max_latency_ms: int = Field(default=30_000, ge=1, le=86_400_000)
    requested_compute: str = Field(default="0", max_length=80)
    confidential: bool = False
    input_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requested_compute")
    @classmethod
    def compute_string(cls, value: str) -> str:
        try:
            from decimal import Decimal

            number = Decimal(value)
        except Exception as exc:
            raise ValueError("requested_compute must be a decimal string") from exc
        if not number.is_finite() or number < 0:
            raise ValueError("requested_compute must be finite and non-negative")
        # Bound exponent before formatting; a short '1e999999999' must not
        # expand into an enormous string during request parsing.
        if abs(number.as_tuple().exponent) > 80 or abs(number.adjusted()) > 80:
            raise ValueError("decimal exponent is out of bounds")
        normalized = format(number, "f")
        if len(normalized) > 80:
            raise ValueError("normalized decimal is too long")
        return normalized


class SubmissionCreate(StrictModel):
    submission_id: str = Field(min_length=3, max_length=80)
    result: dict[str, Any]
    evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    inference_session_ids: list[str] = Field(default_factory=list, max_length=100)
    created_at: float | None = None
    proof_signature: str = Field(min_length=20, max_length=300)


class ValidationCreate(StrictModel):
    decision_id: str = Field(min_length=3, max_length=80)
    decision: Literal["VERIFIED", "REJECTED", "PARTIAL", "SLASHED"]
    policy: str = Field(default="deterministic_then_domain", min_length=1, max_length=120)
    checks: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    reason_codes: list[str] = Field(default_factory=list, max_length=30)
    settlement: dict[str, Any] = Field(default_factory=dict)
    evidence_hash: str = Field(default="", max_length=64)
    signature: str = Field(min_length=20, max_length=300)


class DisputeCreate(StrictModel):
    dispute_id: str = Field(min_length=3, max_length=80)
    reason: str = Field(min_length=10, max_length=2000)
    additional_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class HeartbeatResponse(StrictModel):
    claim_id: str
    lease_expires_at: float
