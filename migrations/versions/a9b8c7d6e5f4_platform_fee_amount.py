"""platform fee column on escrows

Additive migration on top of ``f8a9b0c1d2e3`` (operator registry and task
indexes).

Adds ``escrows.platform_fee_amount``: the net platform service fee carved out
of the executor's release at settlement time. It is an exact decimal string
like every other escrow amount and defaults to ``'0'``, so existing rows and
rolling deploys remain valid. Stored columns keep the extended conservation
invariant:

    released_amount + platform_fee_amount + refunded_amount + slashed_amount
        == reserved_total

The fee itself is a generic marketplace mechanism derived by the settlement
provider from the task's declared ``service_fee_mode`` (``none``/``fixed``/
``bps``); refunds and slashes never carry a fee. ``batch_alter_table`` keeps
the statement portable: PostgreSQL receives a plain ``ALTER TABLE ... ADD
COLUMN`` while SQLite gets Alembic's table-copy implementation, which also
recreates the existing indexes and constraints.

Revision ID: a9b8c7d6e5f4
Revises: f8a9b0c1d2e3
Create Date: 2026-09-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a9b8c7d6e5f4'
down_revision: Union[str, Sequence[str], None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('escrows') as batch_op:
        batch_op.add_column(
            sa.Column('platform_fee_amount', sa.String(length=80), nullable=False, server_default='0')
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('escrows') as batch_op:
        batch_op.drop_column('platform_fee_amount')
