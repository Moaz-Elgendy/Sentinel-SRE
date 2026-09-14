"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-08

"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

notification_channel = postgresql.ENUM(
    "email", "sms",
    name="notificationchannel",
    create_type=False,
)
notification_status = postgresql.ENUM(
    "Sent", "Failed",
    name="notificationstatus",
    create_type=False,
)


def upgrade() -> None:
    notification_channel.create(op.get_bind(), checkfirst=True)
    notification_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # No FK: citizen_id / request_id belong to citizen-service's DB.
        sa.Column("citizen_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("channel", notification_channel, nullable=False, server_default="email"),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", notification_status, nullable=False, server_default="Sent"),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_notifications_citizen_id", "notifications", ["citizen_id"])
    op.create_index("ix_notifications_request_id", "notifications", ["request_id"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    op.drop_table("notifications")
    notification_status.drop(op.get_bind(), checkfirst=True)
    notification_channel.drop(op.get_bind(), checkfirst=True)
