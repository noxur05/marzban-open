"""add announcements table + admins.can_announce

Per-admin and global-fallback subscription announcements, delivered to
clients via the Happ `announce` response header.

Append-only history: every change — including a *disable* — INSERTs a new
row. Rows are never UPDATEd or DELETEd (except the ON DELETE CASCADE when
the owning admin is removed).

  admin_id NULL  -> the single global fallback announcement.
  enabled        -> a row can be a "disable" marker.

Resolution (in the app, not SQL): the LATEST row per admin (MAX(id)) — if
it is enabled, use it; otherwise fall back to the latest global row if it
is enabled; else send no header. NB: the latest row *if* enabled, NOT the
latest *enabled* row (a disable must override an older enabled row).

admins.can_announce gates whether an admin may post their own announcement
(sudo-toggled). Revoking appends a disabled row so the admin's users fall
back to the global announcement on their next fetch.

Revision ID: f3a9c1e84b7d
Revises: d2b8f04c7a31
Create Date: 2026-06-07

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3a9c1e84b7d'
down_revision = 'd2b8f04c7a31'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'announcements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=True),
        sa.Column('text', sa.String(length=200), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by_admin_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['admin_id'], ['admins.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # "latest per admin" = MAX(id) WHERE admin_id=? and the global
    # (admin_id IS NULL) lookup are index-backed by this composite.
    op.create_index(
        'ix_announcements_admin_id', 'announcements', ['admin_id', 'id'],
        unique=False)

    with op.batch_alter_table('admins', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('can_announce', sa.Boolean(), nullable=False,
                      server_default='1'))


def downgrade() -> None:
    with op.batch_alter_table('admins', schema=None) as batch_op:
        batch_op.drop_column('can_announce')
    # drop_table removes the index + FK together. Dropping the index first
    # fails on MySQL (errno 1553) — it backs the admin_id foreign key.
    op.drop_table('announcements')
