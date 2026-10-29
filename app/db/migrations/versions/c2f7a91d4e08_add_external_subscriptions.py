"""add external_subscriptions + admins.can_use_external_hosts

Revision ID: c2f7a91d4e08
Revises: a4c9e1b73f80
Create Date: 2026-08-13 00:00:00.000000

Foreign-panel subscription mirroring. A sudo admin registers the
subscription URL of an ordinary user living on another panel (#2/#3); an
out-of-process Rust poller fetches each one, geo-verifies and relabels the
hosts, and publishes render-ready payloads to Redis. At subscription-render
time the panel appends those hosts for users whose owning admin carries
`can_use_external_hosts`.

Two objects, both additive:

  * `admins.can_use_external_hosts` — default FALSE, so every existing admin
    is unaffected. Traffic through a foreign host is metered on the REMOTE
    panel and this panel receives no usage statistics for it at all, so the
    flag exists to keep it away from admins who are billed per traffic (they
    would consume it for free). Grant it only to per-user/per-device billed
    admins.
  * `external_subscriptions` — sudo-authored registry rows. This is
    configuration, not cache: the poller only ever READS it (sqlx,
    read-only) and writes exclusively to Redis. Losing Redis self-heals on
    the next poll cycle; losing this table loses the feature.

No index on `url`. utf8mb4 makes a VARCHAR(512) unique index 2048 bytes,
which fits InnoDB's 3072-byte limit only with DYNAMIC row format — and this
schema has already produced one prod migration failure over index handling
(errno 1553, fixed in 1a0ca75). The router rejects duplicates instead; the
table holds a handful of rows.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c2f7a91d4e08'
down_revision = 'a4c9e1b73f80'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('admins') as batch_op:
        batch_op.add_column(sa.Column(
            'can_use_external_hosts',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('0'),
        ))

    op.create_table(
        'external_subscriptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        # Operator-facing label, shown in the panel UI and in the poller's
        # log lines. Not sent anywhere.
        sa.Column('name', sa.String(64), nullable=False),
        # Base subscription URL of the remote panel's user, WITHOUT a
        # trailing client-type segment: the poller appends
        # /{client_type} itself to force each format server-side.
        sa.Column('url', sa.String(512), nullable=False),
        # HWID this subscription is fetched with. Remote panels in this family
        # require an X-Hwid header on every subscription fetch (verified live:
        # a fetch without it gets 403 "HWID header required", and the response
        # carries subscription-always-hwid-enable: true). Per-row rather than
        # one global value so the operator can paste the HWID already
        # registered as a device slot on the remote user — a prefilled slot
        # means the poller never auto-claims one, and on this panel family a
        # claimed slot is freed only by an admin block, never by time.
        # NULL = fall back to the poller's own static identity.
        sa.Column('hwid', sa.String(128), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False,
                  server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('external_subscriptions')

    with op.batch_alter_table('admins') as batch_op:
        batch_op.drop_column('can_use_external_hosts')
