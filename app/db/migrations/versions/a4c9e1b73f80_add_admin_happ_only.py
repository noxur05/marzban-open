"""add admins.happ_only (restrict an admin to creating Happ users only)

Revision ID: a4c9e1b73f80
Revises: b7e2f4a91c83
Create Date: 2026-07-27 00:00:00.000000

Per-admin RESTRICTION, not a grant: when TRUE the admin may create ONLY
Happ device-limited users — every user they create must carry
client_type='happ' AND a device_limit. Enforced at create time in
routers/user.add_user; sudo admins bypass it.

Default FALSE so every existing admin keeps its current, unrestricted
behaviour. Nothing about existing users changes: client_type and
device_limit are immutable after create, so the restriction is
create-time only and no backfill is required.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a4c9e1b73f80'
down_revision = 'b7e2f4a91c83'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('admins') as batch_op:
        batch_op.add_column(sa.Column(
            'happ_only',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('0'),
        ))


def downgrade() -> None:
    with op.batch_alter_table('admins') as batch_op:
        batch_op.drop_column('happ_only')
