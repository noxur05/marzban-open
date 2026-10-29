"""Google copies of Micky subscriptions (micky_copies)

Revision ID: e3b7c41a9d20
Revises: 7b4e2d9c1a55
Create Date: 2026-10-09 00:00:00.000000

One row per Micky user: the content key of the user's encrypted Google copies
(G1 Firestore, G2 Drive; docs/micky-protocol.md "Google copies") and the state
the marzban-google-sync service keeps. Existing Micky users get a row (and a
key) here, so their copies are made as soon as the feature is switched on.

Downgrade KEEPS the table. Its rows hold the only record of which public
Google copies exist (the service needs the ids to delete them), and the
previous image ignores an extra table. Refusing instead would make
scripts/rollback.sh stop with the new image still running. Upgrade therefore
creates the table only when it is missing, so downgrade -> upgrade
round-trips; a kept table gets a new key for every user revoked while the
previous image ran, every row marked dirty (that image did not mark its
changes) and a row for any Micky user created meanwhile.

"""
import base64
import secrets
from datetime import datetime, timedelta

from alembic import context, op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


# revision identifiers, used by Alembic.
revision = 'e3b7c41a9d20'
down_revision = '7b4e2d9c1a55'
branch_labels = None
depends_on = None

# Compared to each other (dirty_at > synced_at, synced_at >= key_changed_at):
# microseconds on MySQL, whose plain DATETIME would merge two changes made
# within one second.
PRECISE = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), 'mysql')


def _as_datetime(value) -> datetime:
    """SQLite returns text for raw SELECTs; MySQL returns datetime."""
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


def _new_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _backfill(bind, now) -> None:
    missing = bind.execute(sa.text(
        "SELECT u.id FROM users u LEFT JOIN micky_copies c ON c.user_id = u.id "
        "WHERE u.client_type = 'micky' AND c.id IS NULL")).scalars().all()
    for user_id in missing:
        key = _new_key()
        bind.execute(sa.text(
            "INSERT INTO micky_copies (user_id, content_key, key_changed_at, dirty_at, attempts, created_at) "
            "VALUES (:user_id, :key, :now, :now, 0, :now)"), {"user_id": user_id, "key": key, "now": now})


def upgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("e3b7c41a9d20 generates keys and cannot run in offline mode")
    bind = op.get_bind()
    now = datetime.utcnow()
    if sa.inspect(bind).has_table('micky_copies'):  # kept by a downgrade (see above)
        # A revoke made under the previous image didn't rotate the key: rotate
        # it now, or the re-uploaded copy (new credentials, new link) would be
        # readable with the key the revoked phones still hold.
        # sub_revoked_at keeps whole seconds (MySQL rounds them up) while
        # key_changed_at keeps microseconds: a revoke made by the new image
        # can look up to a second later than its own key. Only a revoke more
        # than a second after the key counts.
        rows = bind.execute(sa.text(
            "SELECT c.id, u.sub_revoked_at, c.key_changed_at FROM micky_copies c "
            "JOIN users u ON u.id = c.user_id WHERE u.sub_revoked_at IS NOT NULL")).all()
        revoked = [row_id for row_id, revoked_at, keyed_at in rows
                   if _as_datetime(revoked_at) > _as_datetime(keyed_at) + timedelta(seconds=1)]
        for row_id in revoked:
            bind.execute(sa.text("UPDATE micky_copies SET content_key = :key, key_changed_at = :now WHERE id = :id"),
                         {"key": _new_key(), "now": now, "id": row_id})
        bind.execute(sa.text("UPDATE micky_copies SET dirty_at = :now"), {"now": now})
        _backfill(bind, now)
        return
    op.create_table(
        'micky_copies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('content_key', sa.String(43), nullable=False),
        sa.Column('key_changed_at', PRECISE, nullable=False),
        sa.Column('firestore_doc_id', sa.String(64), nullable=True),
        sa.Column('drive_file_id', sa.String(128), nullable=True),
        sa.Column('content_sha', sa.String(64), nullable=True),
        sa.Column('dirty_at', PRECISE, nullable=False),
        sa.Column('synced_at', PRECISE, nullable=True),
        sa.Column('checked_at', sa.DateTime(), nullable=True),
        sa.Column('last_upload_at', sa.DateTime(), nullable=True),
        sa.Column('last_issued_at', sa.BigInteger(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.String(255), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
    )
    op.create_index('ix_micky_copies_dirty_at', 'micky_copies', ['dirty_at'])
    _backfill(bind, now)


def downgrade() -> None:
    pass  # keeps micky_copies (see above)
