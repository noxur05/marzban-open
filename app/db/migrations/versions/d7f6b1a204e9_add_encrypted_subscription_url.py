"""add encrypted_subscription_url to users

Revision ID: d7f6b1a204e9
Revises: c5e2a8d3f117
Create Date: 2026-05-12 14:00:00.000000

Adds a single nullable column on `users` that holds the Happ `crypt4`
ciphertext for a given user's subscription URL. Populated only when
`client_type='happ'`; NULL for legacy users (the API surfaces the plain
URL in that case).

Fully additive — no backfill required. Legacy users keep current behavior.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd7f6b1a204e9'
down_revision = 'c5e2a8d3f117'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column(
            'encrypted_subscription_url', sa.String(length=1024), nullable=True,
        ))


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('encrypted_subscription_url')
