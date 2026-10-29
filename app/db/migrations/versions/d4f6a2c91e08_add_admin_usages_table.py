"""add admin_usages snapshot table

Stores a periodic snapshot of every admin's *cumulative lifetime*
traffic so the dashboard can render per-admin day-by-day usage.

The snapshotted value is monotonic: it is
    admins.users_usage + Σ(admin_usage_logs.used_traffic_at_reset)
i.e. it survives a `reset_admin_usage` call (which zeroes
admins.users_usage). Day-by-day usage is computed as the diff of
consecutive daily snapshots, so it must never go backwards.

No DB-level FK constraint on admin_id, consistent with
reseller_admin_id in revision c8e3a1f7b520: a plain FK would block
admin deletion (RESTRICT) and we want snapshot history retained even
after an admin is removed. The ORM model declares the column without a
ForeignKey for the same reason.

Revision ID: d4f6a2c91e08
Revises: c8e3a1f7b520
Create Date: 2026-05-17

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4f6a2c91e08'
down_revision = 'c8e3a1f7b520'
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index('ix_admin_usages_admin_id_created_at',
                  table_name='admin_usages')
    op.drop_table('admin_usages')
