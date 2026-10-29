"""Micky device slots survive reinstalls (M-Device-Id)

Revision ID: 7b4e2d9c1a55
Revises: 5d2c8e1a9f30
Create Date: 2026-09-30 00:00:00.000000

A Micky device now claims its slot by a stable id ("id:<M-Device-Id>",
docs/micky-protocol.md "Device identity"). A reinstall brings a new device
key with the same id; the slot is rebound to the new key instead of using a
new slot, at most a few times per 24 h. These columns hold the currently
bound key and that rebind budget. Additive only: NULL / 0 on every existing
row, so the previous image runs unchanged against this schema.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7b4e2d9c1a55'
down_revision = '5d2c8e1a9f30'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user_subscription_devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bound_pub_key', sa.String(46), nullable=True))
        batch_op.add_column(sa.Column('rebind_window_start', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('rebind_count', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('user_subscription_devices', schema=None) as batch_op:
        batch_op.drop_column('rebind_count')
        batch_op.drop_column('rebind_window_start')
        batch_op.drop_column('bound_pub_key')
