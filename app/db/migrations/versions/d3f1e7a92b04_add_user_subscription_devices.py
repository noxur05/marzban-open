"""add user_subscription_devices

Revision ID: d3f1e7a92b04
Revises: a1b2c3d4e5f6
Create Date: 2026-05-11 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd3f1e7a92b04'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'user_subscription_devices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('device_key', sa.String(length=128), nullable=False),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('client_name', sa.String(length=64), nullable=True),
        sa.Column('client_version', sa.String(length=32), nullable=True),
        sa.Column('platform', sa.String(length=32), nullable=True),
        sa.Column('os_version', sa.String(length=32), nullable=True),
        sa.Column('device_model', sa.String(length=64), nullable=True),
        sa.Column('locale', sa.String(length=16), nullable=True),
        sa.Column('last_ip', sa.String(length=45), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column('fetch_count', sa.Integer(), nullable=False,
                  server_default='1'),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'device_key', name='uq_user_device'),
    )
    op.create_index(
        'ix_user_recent_devices',
        'user_subscription_devices',
        ['user_id', 'last_seen_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_user_recent_devices', table_name='user_subscription_devices')
    op.drop_table('user_subscription_devices')
