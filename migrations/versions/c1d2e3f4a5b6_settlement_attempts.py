"""Durable chain-agnostic settlement intent, attempt and transition journal.

Additive only; existing mock escrow/ledger rows are not changed or enqueued.
Downgrade destroys attempt evidence: stop all producers/workers and archive it
before a human-authorized rollback. Prefer rolling back application code alone.

Revision ID: c1d2e3f4a5b6
Revises: b0c9d8e7f6a5
"""
from alembic import op
import sqlalchemy as sa

revision = "c1d2e3f4a5b6"
down_revision = "b0c9d8e7f6a5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "settlement_intents",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("task_id", sa.String(80), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("slot", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False, unique=True),
        sa.Column("target", sa.String(200), nullable=False),
        sa.Column("canonical_intent", sa.String(8000), nullable=False),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("task_id", "slot", name="uq_settlement_intent_slot"),
        sa.CheckConstraint("slot IN ('hold', 'terminal')", name="ck_settlement_intent_slot"),
    )
    op.create_index("ix_settlement_intent_target", "settlement_intents", ["target"])
    op.create_table(
        "settlement_attempts",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("intent_id", sa.String(80), sa.ForeignKey("settlement_intents.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("transaction_ref", sa.String(200)),
        sa.Column("submission_started_at", sa.Float()),
        sa.Column("lease_owner", sa.String(80)),
        sa.Column("lease_expires_at", sa.Float()),
        sa.Column("next_attempt_at", sa.Float(), nullable=False),
        sa.Column("verification_attempts", sa.Integer(), nullable=False),
        sa.Column("block_ref", sa.String(200)),
        sa.Column("finality_depth", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(40)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint("status IN ('PENDING', 'SUBMITTED', 'CONFIRMED', 'FINALIZED', "
                           "'FAILED', 'REPLACED', 'REORGED', 'MANUAL_REVIEW')", name="ck_settlement_attempt_state"),
        sa.CheckConstraint("number > 0 AND verification_attempts >= 0 AND finality_depth >= 0",
                           name="ck_settlement_attempt_counters"),
        sa.CheckConstraint("(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
                           "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
                           name="ck_settlement_attempt_lease"),
        sa.UniqueConstraint("intent_id", "number", name="uq_settlement_attempt_number"),
        sa.UniqueConstraint("intent_id", "transaction_ref", name="uq_settlement_attempt_transaction"),
    )
    op.create_index("uq_settlement_active_attempt", "settlement_attempts", ["intent_id"], unique=True,
                    sqlite_where=sa.text("status NOT IN ('FAILED', 'REPLACED')"),
                    postgresql_where=sa.text("status NOT IN ('FAILED', 'REPLACED')"))
    op.create_index("ix_settlement_attempt_due", "settlement_attempts", ["status", "next_attempt_at"])
    op.create_table(
        "settlement_attempt_events",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("attempt_id", sa.String(80), sa.ForeignKey("settlement_attempts.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(24)),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_settlement_attempt_event", "settlement_attempt_events", ["attempt_id", "created_at"])


def downgrade():
    op.drop_table("settlement_attempt_events")
    op.drop_table("settlement_attempts")
    op.drop_table("settlement_intents")
