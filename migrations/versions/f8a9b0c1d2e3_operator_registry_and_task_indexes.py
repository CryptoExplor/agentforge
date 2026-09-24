"""operator registry and SQL task query indexes

Additive migration on top of ``e7f8a9b0c1d2`` (task verification strategy).

1. ``operator_role_grants``: the operator registry for explicit, revocable
   marketplace roles. Submitting a validation decision to the peer-validation
   endpoints requires an ``ACTIVE`` ``validator`` grant for the submitting
   agent (development ``OPEN_OPERATORS=true`` mode additionally self-grants
   the role to capability-declaring agents at registration). The table starts
   empty, so no existing agent gains or loses authority from this migration
   alone; ``status`` records revocation without destroying the audit trail and
   the unique ``(agent_did, role)`` constraint makes grants idempotent.

2. Indexes ``ix_task_kind`` and ``ix_task_verification_strategy`` on ``tasks``:
   ``GET /api/v1/tasks`` now filters, orders and paginates in SQL, so the
   equality-filter columns need covering indexes. ``ix_task_status`` already
   exists from the initial schema. ``tasks(kind)`` and
   ``tasks(verification_strategy)`` are low-cardinality, so plain b-tree
   indexes are sufficient at MVP scale.

No existing column, row or index is modified or dropped; the downgrade simply
removes what this revision added.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-09-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'operator_role_grants',
        sa.Column('id', sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column('agent_did', sa.String(length=200), sa.ForeignKey('agents.did'), nullable=False),
        sa.Column('role', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=24), nullable=False, server_default='ACTIVE'),
        sa.Column('granted_by', sa.String(length=200), nullable=True),
        sa.Column('reason', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('revoked_at', sa.Float(), nullable=True),
        sa.UniqueConstraint('agent_did', 'role', name='uq_operator_role_grant'),
    )
    op.create_index('ix_operator_role_grant_agent', 'operator_role_grants', ['agent_did'])
    op.create_index('ix_task_kind', 'tasks', ['kind'])
    op.create_index('ix_task_verification_strategy', 'tasks', ['verification_strategy'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_task_verification_strategy', table_name='tasks')
    op.drop_index('ix_task_kind', table_name='tasks')
    op.drop_index('ix_operator_role_grant_agent', table_name='operator_role_grants')
    op.drop_table('operator_role_grants')
