"""add admin_hourly_usages table

Top-of-the-hour snapshot of every admin's traffic + activity gauges,
written by the out-of-process Rust stats collector (the panel never
writes this table; Alembic owns the DDL).

One row per admin per UTC hour, keyed on (admin_id, recorded_at) where
recorded_at is the top of the hour. The collector upserts on that
unique key (ON DUPLICATE KEY / ON CONFLICT) so a re-run within the same
hour just refreshes the gauges.

  users_usage  — MONOTONIC cumulative lifetime traffic per admin:
                 admins.users_usage + Σ(admin_usage_logs.used_traffic_at_reset).
                 Survives a reset_admin_usage call, so diffing
                 consecutive hourly snapshots never goes negative.
  device_count — active devices gauge (user_subscription_devices.is_blocked = FALSE).
  user_count   — active users gauge (users.status = 'active').

No DB-level FK on admin_id, consistent with admin_usage_logs and the
former admin_usages table: a RESTRICT FK would block admin deletion and
we want snapshot history retained even after an admin is removed.

Revision ID: c1a7e93b6f20
Revises: d4f6a2c91e08
Create Date: 2026-06-06

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1a7e93b6f20'
down_revision = 'd4f6a2c91e08'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'admin_hourly_usages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=False),
        sa.Column('recorded_at', sa.DateTime(), nullable=False),
        sa.Column('users_usage', sa.BigInteger(), nullable=False,
                  server_default='0'),
        sa.Column('device_count', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('user_count', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('admin_id', 'recorded_at', name='uq_admin_hour'),
    )
    op.create_index(
        'ix_admin_hourly_usages_admin_recorded',
        'admin_hourly_usages', ['admin_id', 'recorded_at'], unique=False)
    # Standalone recorded_at index so the sudo "all admins" range query
    # (filters only on recorded_at) is index-backed instead of a full scan.
    op.create_index(
        'ix_admin_hourly_usages_recorded',
        'admin_hourly_usages', ['recorded_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_admin_hourly_usages_recorded',
                  table_name='admin_hourly_usages')
    op.drop_index('ix_admin_hourly_usages_admin_recorded',
                  table_name='admin_hourly_usages')
    op.drop_table('admin_hourly_usages')
