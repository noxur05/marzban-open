"""drop the reseller / device-billed-user columns

Revision ID: d404461618c5
Revises: f2b9d7c04a16
Create Date: 2026-09-26 00:00:00.000000

Retires the device-billed-user ("reselling") feature added by c8e3a1f7b520.
Its API, CLI and dashboard surface are removed in the same change, so nothing
reads or writes these columns any more:

  - admins.can_create_billed_users
  - admins.is_billing_target
  - users.reseller_admin_id

What happens to existing billed users: nothing visible to them. A billed user
was always OWNED by the billing-target admin (`users.admin_id`), which is where
its traffic, statistics and admin-scoped hosts already came from. Dropping
`reseller_admin_id` only removes the reseller's secondary view/modify access;
the billing-target admin keeps the user as an ordinary owned user. Reassign with
`PUT /api/user/{username}/set-owner` if a former reseller should own it.

c8e3a1f7b520 created `reseller_admin_id` with no FK constraint and no index
(see its docstring), so there is nothing to drop before the column itself.

**Downgrade restores the schema, not the data.** It re-adds the three columns
empty (flags false, reseller NULL) so an older image can run against the
database again — which is what scripts/rollback.sh needs — but every reseller
link and billing flag is gone for good once upgrade() has run. Take the value
from the pre-deploy backup if it is ever genuinely needed.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd404461618c5'
down_revision = 'f2b9d7c04a16'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('reseller_admin_id')

    with op.batch_alter_table('admins', schema=None) as batch_op:
        batch_op.drop_column('is_billing_target')
        batch_op.drop_column('can_create_billed_users')


def downgrade() -> None:
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
