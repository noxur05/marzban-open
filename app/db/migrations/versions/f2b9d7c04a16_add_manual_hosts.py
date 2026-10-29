"""add manual_hosts + manual_host_admins

Revision ID: f2b9d7c04a16
Revises: e1a7c3f9b204
Create Date: 2026-08-28 00:00:00.000000

Operator-authored proxy hosts, the manual twin of external_subscriptions: a
sudo admin pastes a full share link (vless://, vmess://, trojan://, ss://) for a
server that lives elsewhere — one this panel cannot represent as a ProxyHost,
which only overrides the address of a local inbound and cannot carry a foreign
server's own uuid/keys. At subscription-render time the panel appends the link,
verbatim, to the v2ray configs of users whose owning admin the host is assigned
to (app/subscription/manual.py, reusing external.merge_into).

Two objects, both additive:

  * `manual_hosts` — sudo-authored registry rows holding the opaque link. This
    is CONFIGURATION; unlike external_subscriptions there is no poller — the
    link is served straight from here (with a Redis cache in front).
  * `manual_host_admins` — which admins receive each host. Shape mirrors
    external_subscription_admins: composite PK, both FKs ON DELETE CASCADE,
    index on admin_id. **Empty = nobody**, deliberately (like external subs,
    not like host_admins) — "scope to specific admins" must fail closed.

No unique index on `link` (utf8mb4 × a long VARCHAR overflows InnoDB's 3072-byte
index limit — the errno-1553 lesson from f3c1d8e92a17 / b8d4f2c7a913); duplicates
are harmless and the operator owns the content.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f2b9d7c04a16'
down_revision = 'e1a7c3f9b204'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'manual_hosts',
        sa.Column('id', sa.Integer(), primary_key=True),
        # Operator-facing label, shown in the panel UI. Not sent anywhere.
        sa.Column('name', sa.String(64), nullable=False),
        # The full share link, stored opaquely and appended verbatim.
        sa.Column('link', sa.String(2048), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False,
                  server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )

    op.create_table(
        'manual_host_admins',
        sa.Column('manual_host_id', sa.Integer(), nullable=False),
        sa.Column('admin_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['manual_host_id'], ['manual_hosts.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['admin_id'], ['admins.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('manual_host_id', 'admin_id'),
    )
    op.create_index(
        'ix_manual_host_admins_admin_id',
        'manual_host_admins',
        ['admin_id'],
    )


def downgrade() -> None:
    # drop_table cascades the FK constraints and their dependent indexes on
    # MySQL. An explicit drop_index first fails with errno 1553 when the index
    # backs an FK — same lesson as b8d4f2c7a913. Drop the child table first.
    op.drop_table('manual_host_admins')
    op.drop_table('manual_hosts')
