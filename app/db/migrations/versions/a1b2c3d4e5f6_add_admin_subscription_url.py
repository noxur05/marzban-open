"""add admin subscription_url

Revision ID: a1b2c3d4e5f6
Revises: 2b231de97dc3
Create Date: 2026-05-02 18:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '2b231de97dc3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('admins') as batch_op:
        batch_op.add_column(sa.Column(
            'subscription_url', sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('admins') as batch_op:
        batch_op.drop_column('subscription_url')
