"""Shared request-admission counters; preserve marketplace and outbox data."""
from alembic import op
import sqlalchemy as sa

revision = "d6e7f8a9b0c1"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "request_quotas",
        sa.Column("bucket", sa.String(80), primary_key=True),
        sa.Column("window", sa.Integer(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_request_quotas_window", "request_quotas", ["window"])
    op.create_index("ix_used_nonces_created_at", "used_nonces", ["created_at"])
    op.create_index("ix_registration_challenges_expiry", "registration_challenges", ["expires_at"])


def downgrade():
    op.drop_index("ix_registration_challenges_expiry", table_name="registration_challenges")
    op.drop_index("ix_used_nonces_created_at", table_name="used_nonces")
    op.drop_index("ix_request_quotas_window", table_name="request_quotas")
    op.drop_table("request_quotas")
