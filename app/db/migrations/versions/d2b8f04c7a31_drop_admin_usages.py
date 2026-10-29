"""drop the legacy admin_usages snapshot table

The daily admin_usages snapshot mechanism is replaced by the hourly
admin_hourly_usages table (revision c1a7e93b6f20), written by the
out-of-process Rust stats collector. The old daily cron and its
crud/ORM symbols are gone, so the table is dead weight.

admin_usage_logs is KEPT — the collector's monotonic traffic formula
(admins.users_usage + Σ admin_usage_logs.used_traffic_at_reset) still
depends on it.

downgrade() recreates admin_usages exactly as revision d4f6a2c91e08
left it, so the chain stays reversible (data is not restored).

Revision ID: d2b8f04c7a31
Revises: c1a7e93b6f20
Create Date: 2026-06-06

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd2b8f04c7a31'
down_revision = 'c1a7e93b6f20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index('ix_admin_usages_admin_id_created_at',
                  table_name='admin_usages')
    op.drop_table('admin_usages')


def downgrade() -> None:
    op.create_table(
        'admin_usages',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=False),
        sa.Column('used_traffic', sa.BigInteger(), nullable=False,
                  server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_admin_usages_admin_id_created_at',
        'admin_usages', ['admin_id', 'created_at'], unique=False)
