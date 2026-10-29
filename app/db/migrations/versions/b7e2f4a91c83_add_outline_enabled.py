"""add outline access gate (users.outline_enabled + admins.can_enable_outline)

Revision ID: b7e2f4a91c83
Revises: f3a9c1e84b7d
Create Date: 2026-06-22 00:00:00.000000

Per-user gate for the Outline (Shadowsocks dynamic-key) subscription.
Outline clients send no X-Hwid, so an Outline user is an uncountable
device the device-limit policy can't enforce — so access is opt-in per
user. Default FALSE (all existing users included): the /outline endpoint
returns an empty `{}` config unless enabled. A user's flag may be turned
on but never off (one-way grant, enforced in crud.update_user).

`admins.can_enable_outline` is the per-admin permission to grant that
flag. Default FALSE; sudo admins always may. Both columns default FALSE
so the feature is dark until explicitly granted.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e2f4a91c83'
down_revision = 'f3a9c1e84b7d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column(
            'outline_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('0'),
        ))
    with op.batch_alter_table('admins') as batch_op:
        batch_op.add_column(sa.Column(
            'can_enable_outline',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('0'),
        ))


def downgrade() -> None:
    with op.batch_alter_table('admins') as batch_op:
        batch_op.drop_column('can_enable_outline')
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('outline_enabled')
