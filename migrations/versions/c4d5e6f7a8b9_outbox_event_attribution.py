"""outbox event attribution and delivery telemetry

Adds the columns required by the signed event envelope:

- ``actor_did``: the DID that requested the transition, null for
  server-generated events such as claim expiry.
- ``causation``: the verified request that caused the transition
  (request id/nonce, request signature, request body hash). Null when no client
  request caused the event.
- ``last_attempt_at`` and ``last_error``: delivery telemetry used by
  ``outbox_metrics()`` and the worker, so a failing transport is visible instead
  of silent.

All four columns are nullable, so the upgrade is non-destructive and existing
rows keep working.

Revision ID: c4d5e6f7a8b9
Revises: 3293de03bb66
Create Date: 2026-09-17 20:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = '3293de03bb66'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('outbox_events') as batch_op:
        batch_op.add_column(sa.Column('actor_did', sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column('causation', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('last_attempt_at', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('last_error', sa.String(length=300), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('outbox_events') as batch_op:
        batch_op.drop_column('last_error')
        batch_op.drop_column('last_attempt_at')
        batch_op.drop_column('causation')
        batch_op.drop_column('actor_did')
