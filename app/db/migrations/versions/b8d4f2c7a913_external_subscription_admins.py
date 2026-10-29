"""scope external subscriptions to admins via a junction table

Revision ID: b8d4f2c7a913
Revises: c2f7a91d4e08
Create Date: 2026-08-13 00:00:00.000000

Replaces the all-or-nothing `admins.can_use_external_hosts` flag with a
many-to-many `external_subscription_admins(external_subscription_id, admin_id)`,
so each registered subscription is served only to the admins it is assigned to.
Before this, one flag meant an admin's users received EVERY registered
subscription.

Shape mirrors the existing `host_admins` junction (migration f3c1d8e92a17):
composite PK, both FKs ON DELETE CASCADE, index on admin_id for the per-admin
lookup the subscription render path performs.

**The empty set means the OPPOSITE of `host_admins`, deliberately.** A host with
no rows in `host_admins` is global — visible to everyone. An external
subscription with no rows here is served to NOBODY. These hosts cost money on
someone else's panel and their whole reason for existing behind a grant is
billing containment, so "unassigned" must fail closed. Do not "fix" this into
consistency with host_admins.

The flag is dropped, not kept alongside — one grant mechanism, not two. Existing
grants are seeded first so nothing silently switches off: every admin currently
holding `can_use_external_hosts` is linked to every registered subscription,
which reproduces exactly what those admins' users receive today.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8d4f2c7a913'
down_revision = 'c2f7a91d4e08'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'external_subscription_admins',
        sa.Column('external_subscription_id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ['external_subscription_id'],
            ['external_subscriptions.id'],
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(['admin_id'], ['admins.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('external_subscription_id', 'admin_id'),
    )
    op.create_index(
        'ix_external_subscription_admins_admin_id',
        'external_subscription_admins',
        ['admin_id'],
    )

    # Seed BEFORE dropping the column: every currently-granted admin keeps
    # receiving every registered subscription, which is what they get today.
    # CROSS JOIN is spelled as an unrestricted two-table SELECT so the statement
    # runs on both MySQL and SQLite.
    op.execute(sa.text(
        "INSERT INTO external_subscription_admins "
        "(external_subscription_id, admin_id) "
        "SELECT es.id, a.id FROM external_subscriptions es, admins a "
        "WHERE a.can_use_external_hosts = 1"
    ))

    with op.batch_alter_table('admins') as batch_op:
        batch_op.drop_column('can_use_external_hosts')


def downgrade() -> None:
    with op.batch_alter_table('admins') as batch_op:
        batch_op.add_column(sa.Column(
            'can_use_external_hosts',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('0'),
        ))

    # Restore the coarse flag from the junction: an admin linked to at least one
    # subscription had the grant. This necessarily widens things back to
    # all-or-nothing — that is what the flag could express.
    op.execute(sa.text(
        "UPDATE admins SET can_use_external_hosts = 1 "
        "WHERE id IN (SELECT admin_id FROM external_subscription_admins)"
    ))

    # drop_table cascades the FK constraints and their dependent indexes on
    # MySQL. An explicit drop_index first fails with errno 1553 ("needed in a
    # foreign key constraint") because MySQL reuses our explicit
    # ix_external_subscription_admins_admin_id to satisfy the admin_id FK rather
    # than creating a parallel auto-FK index. Same lesson as f3c1d8e92a17 and
    # f3a9c1e84b7d — caught here by scripts/test-migration.sh before deploy.
    op.drop_table('external_subscription_admins')
