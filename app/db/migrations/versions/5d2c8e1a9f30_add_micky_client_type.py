"""add 'micky' to the clienttype enum

Revision ID: 5d2c8e1a9f30
Revises: d404461618c5
Create Date: 2026-09-29 00:00:00.000000

A user's subscription is bound to one app: Happ (c5e2a8d3f117) or now Micky
(docs/micky-protocol.md). Widens the MySQL ENUM; on SQLite the enum is a plain
string column, so the ALTER is a no-op there.

Downgrade refuses while Micky users exist rather than silently turning them
into legacy users: that would drop their device limit and hand out their plain
subscription URL. Convert or delete them first. Right after a deploy (the case
scripts/rollback.sh covers) there are none, so rollback still works.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5d2c8e1a9f30'
down_revision = 'd404461618c5'
branch_labels = None
depends_on = None

OLD = sa.Enum('happ', name='clienttype')
NEW = sa.Enum('happ', 'micky', name='clienttype')


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('client_type', existing_type=OLD, type_=NEW, existing_nullable=True)


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM users WHERE client_type = 'micky'")).scalar()
    if n:
        raise RuntimeError(
            f"{n} Micky user(s) exist; convert or delete them before downgrading "
            "below 5d2c8e1a9f30 (dropping 'micky' would expose their plain links)"
        )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.alter_column('client_type', existing_type=NEW, type_=OLD, existing_nullable=True)
