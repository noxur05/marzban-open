"""add reseller and billing-target columns

Adds the device-billed-user feature:
  - admins.can_create_billed_users : reseller may create billed users
  - admins.is_billing_target       : the single account that absorbs
                                     billed users' traffic
  - users.reseller_admin_id        : reseller who created / manages a
                                     billed user (NULL for normal users)

No DB-level FK constraint is created for reseller_admin_id on purpose:
naming and dropping FK constraints under batch_alter_table is fragile on
MySQL 8 (see revision f3c1d8e92a17 / fix 5e79e41). The ORM declares
ForeignKey("admins.id") on the column for relationship resolution;
referential cleanup of a deleted reseller is not required for v1 — a
dangling id simply stops matching the reselling filter.

Revision ID: c8e3a1f7b520
Revises: a8c4e7b21f53
Create Date: 2026-05-17

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8e3a1f7b520'
down_revision = 'a8c4e7b21f53'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('admins', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'can_create_billed_users', sa.Boolean(),
            nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column(
            'is_billing_target', sa.Boolean(),
            nullable=False, server_default=sa.false()))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'reseller_admin_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('reseller_admin_id')

    with op.batch_alter_table('admins', schema=None) as batch_op:
        batch_op.drop_column('is_billing_target')
        batch_op.drop_column('can_create_billed_users')
