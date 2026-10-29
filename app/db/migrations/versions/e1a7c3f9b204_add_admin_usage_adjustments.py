"""add admin_usage_adjustments (manual traffic corrections audit)

Revision ID: e1a7c3f9b204
Revises: b8d4f2c7a913
Create Date: 2026-08-27 00:00:00.000000

Sudo operators can add traffic to an admin on a chosen local day to correct
usage that this panel could not meter (external-subscription traffic is
metered on the remote panel and is invisible here). Each such adjustment
edits the admin's historical `admin_hourly_usages` snapshots with a
carry-forward offset and bumps `admins.users_usage`; this table is the
audit trail of those edits — one row per (admin, day, amount) — so an
operator can see what was added and, if needed, reverse it.

Additive only. `admin_id` and `created_by_admin_id` are plain ints with NO
FK (same convention as `announcements.created_by_admin_id`): the audit row
must survive deletion of either admin. `for_day` is the Asia/Ashgabat local
day the traffic was attributed to.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e1a7c3f9b204'
down_revision = 'b8d4f2c7a913'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'admin_usage_adjustments',
        sa.Column('id', sa.Integer(), primary_key=True),
        # Whose usage was adjusted. Plain int, no FK — survives admin deletion.
        sa.Column('admin_id', sa.Integer(), nullable=False),
        # The Asia/Ashgabat local day the traffic was attributed to.
        sa.Column('for_day', sa.Date(), nullable=False),
        # Bytes added to that day (already ramped across its hourly snapshots).
        sa.Column('added_bytes', sa.BigInteger(), nullable=False),
        # The sudo admin who performed it. Plain int, no FK, nullable — audit
        # only, survives that admin's deletion.
        sa.Column('created_by_admin_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index(
        'ix_admin_usage_adjustments_admin_id',
        'admin_usage_adjustments',
        ['admin_id'],
    )


def downgrade() -> None:
    # drop_table cascades the index; an explicit drop_index first would fail
    # on MySQL (errno 1553) if the index backed a constraint — same lesson as
    # f3c1d8e92a17 / b8d4f2c7a913. This index backs no FK, but keep the safe
    # order regardless.
    op.drop_table('admin_usage_adjustments')
