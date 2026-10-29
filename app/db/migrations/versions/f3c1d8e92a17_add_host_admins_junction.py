"""add host_admins junction table

Revision ID: f3c1d8e92a17
Revises: d7f6b1a204e9
Create Date: 2026-05-13 12:00:00.000000

Adds a many-to-many `host_admins(host_id, admin_id)` so a `ProxyHost`
can be scoped to one or more admins. Semantics enforced at the
subscription-merge layer (app/subscription/share.py):

  - Host with zero rows in host_admins -> global (visible to every user).
  - Host with one or more rows         -> visible only to users whose
                                          users.admin_id matches one of
                                          the linked admins.

`ON DELETE CASCADE` on both FKs: deleting an admin drops their links,
which can widen a host to global. Deleting a host removes its links.

Fully additive. No backfill required — existing hosts have no rows in
the new table, so they default to "global", which is the existing
behavior.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f3c1d8e92a17'
down_revision = 'd7f6b1a204e9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'host_admins',
        sa.Column('host_id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['host_id'], ['hosts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['admin_id'], ['admins.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('host_id', 'admin_id'),
    )
    op.create_index('ix_host_admins_admin_id', 'host_admins', ['admin_id'])


def downgrade() -> None:
    # drop_table cascades the FK constraints and dependent indexes on
    # MySQL automatically. An explicit drop_index before drop_table
    # fails with errno 1553 because the FK on admin_id pins
    # ix_host_admins_admin_id (MySQL reuses our explicit index to
    # satisfy the FK rather than creating a parallel auto-FK index).
    op.drop_table('host_admins')
