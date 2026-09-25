"""server-anchored received_at on claims and submissions

Additive migration on top of ``a9b8c7d6e5f4`` (platform fee amount).

Adds ``claims.received_at`` and ``submissions.received_at``: the authoritative
server receipt time of the request that created the row (Grok roadmap 1.4).

- ``claims.received_at`` is the anchor for the lease. ``lease_expires_at`` is
  always ``received_at + lease``, so a client clock — including a signed
  ``X-Agent-Timestamp`` that runs ahead — can never lengthen an execution lease.
- ``submissions.received_at`` is the arrival time used for deadline decisions.
  ``created_at`` stays the executor-declared instant inside the signed proof and
  is deliberately left untouched, so existing proof signatures remain valid.

Existing rows are backfilled from ``created_at``, which was already written by
the server, so no historical row is left without a usable anchor. The columns
are ``NOT NULL`` with a ``'0'`` server default only for the duration of the
``ADD COLUMN`` statement; ``batch_alter_table`` keeps this portable (plain
``ALTER TABLE ... ADD COLUMN`` on PostgreSQL, Alembic's table copy on SQLite,
which also recreates the existing indexes and constraints).

Revision ID: b0c9d8e7f6a5
Revises: a9b8c7d6e5f4
Create Date: 2026-09-25 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b0c9d8e7f6a5'
down_revision: Union[str, Sequence[str], None] = 'a9b8c7d6e5f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ('claims', 'submissions')


def upgrade() -> None:
    """Upgrade schema."""
    for table in TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column('received_at', sa.Float(), nullable=False, server_default='0')
            )
        # created_at was already server-written, so it is a correct anchor for
        # every row that predates server-anchored receipt times.
        op.execute(sa.text(f'UPDATE {table} SET received_at = created_at WHERE received_at = 0'))


def downgrade() -> None:
    """Downgrade schema."""
    for table in reversed(TABLES):
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_column('received_at')
