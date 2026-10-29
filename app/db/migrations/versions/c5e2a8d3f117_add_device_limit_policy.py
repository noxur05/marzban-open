"""add device limit policy

Revision ID: c5e2a8d3f117
Revises: d3f1e7a92b04
Create Date: 2026-05-12 12:00:00.000000

Adds two opt-in columns on `users` so an admin can pin a user to a
specific client and cap how many devices may fetch the subscription:
  - client_type (ENUM, currently only 'happ')
  - device_limit (INT)
Both NULL = feature off (legacy users unchanged).

Relaxes `user_subscription_devices.first_seen_at` / `.last_seen_at` to
NULLable so admin-prefilled HWID slots can exist before any client has
actually connected. Telemetry rows still populate them via the model's
Python-side default; nothing changes for the existing fetch path.

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c5e2a8d3f117'
down_revision = 'd3f1e7a92b04'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column(
            'client_type',
            sa.Enum('happ', name='clienttype'),
            nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'device_limit', sa.Integer(), nullable=True,
        ))

    with op.batch_alter_table('user_subscription_devices') as batch_op:
        batch_op.alter_column(
            'first_seen_at',
            existing_type=sa.DateTime(),
            existing_server_default=sa.func.current_timestamp(),
            nullable=True,
        )
        batch_op.alter_column(
            'last_seen_at',
            existing_type=sa.DateTime(),
            existing_server_default=sa.func.current_timestamp(),
            nullable=True,
        )


def downgrade() -> None:
    # Backfill any admin-prefilled (never-connected) rows so the NOT NULL
    # constraint can be re-applied without breaking.
    op.execute(
        "UPDATE user_subscription_devices "
        "SET first_seen_at = COALESCE(first_seen_at, CURRENT_TIMESTAMP), "
        "    last_seen_at  = COALESCE(last_seen_at,  CURRENT_TIMESTAMP)"
    )

    with op.batch_alter_table('user_subscription_devices') as batch_op:
        batch_op.alter_column(
            'last_seen_at',
            existing_type=sa.DateTime(),
            existing_server_default=sa.func.current_timestamp(),
            nullable=False,
        )
        batch_op.alter_column(
            'first_seen_at',
            existing_type=sa.DateTime(),
            existing_server_default=sa.func.current_timestamp(),
            nullable=False,
        )

    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('device_limit')
        batch_op.drop_column('client_type')
