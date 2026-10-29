"""add is_blocked to user_subscription_devices

Revision ID: a8c4e7b21f53
Revises: f3c1d8e92a17
Create Date: 2026-05-13 09:00:00.000000

Adds a one-way "blocked" flag on subscription-device rows. A blocked
device fails the global active filter in app/db/crud.py — it is hidden
from every UI (modal, count, statistics, end-user subscription page)
and frees its device_limit slot. On subscription fetch, the panel
returns an empty body with a Turkmen "Blok Edildiniz" profile-title
so Happ surfaces the block to the user.

Block is by design irreversible from the admin UI — there is no
unblock endpoint. Reactivation requires a direct DB write.

Fully additive — every existing row defaults to is_blocked=FALSE,
preserving current behavior.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a8c4e7b21f53'
down_revision = 'f3c1d8e92a17'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('user_subscription_devices') as batch_op:
        batch_op.add_column(sa.Column(
            'is_blocked',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('0'),
        ))


def downgrade() -> None:
    with op.batch_alter_table('user_subscription_devices') as batch_op:
        batch_op.drop_column('is_blocked')
