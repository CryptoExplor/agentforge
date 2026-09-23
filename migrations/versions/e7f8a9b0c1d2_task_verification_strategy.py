"""task verification strategy

Adds ``tasks.verification_strategy`` so a task can declare how its outcome is
decided:

- ``deterministic``: the server evaluates the declarative acceptance criteria
  when a proof is submitted and settles the task in the same transaction.
  No independent validator is required, and a later validator decision is
  refused.
- ``peer_review``: an approval-listed independent validator must submit a
  signed validation decision before settlement. This is the default and
  preserves the behavior of every task created before this migration.
- ``operator``: reserved for operator-driven review; treated like peer review
  today.

The column is ``NOT NULL`` with ``server_default='peer_review'`` so existing
rows are backfilled with the safe (manual) strategy instead of being left
unreadable or accidentally auto-settling. The server default is kept afterwards
so raw SQL inserts and rolling deploys that still run the previous application
version remain valid on both SQLite and PostgreSQL.

``batch_alter_table`` keeps the statement portable: PostgreSQL receives a plain
``ALTER TABLE ... ADD COLUMN``, while SQLite (which cannot add a NOT NULL column
with a default in place) gets Alembic's table-copy implementation, which also
recreates the existing indexes and constraints.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-09-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, Sequence[str], None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.add_column(
            sa.Column(
                'verification_strategy',
                sa.String(length=32),
                nullable=False,
                server_default='peer_review',
            )
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.drop_column('verification_strategy')
