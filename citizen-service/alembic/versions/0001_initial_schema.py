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

request_status = postgresql.ENUM(
    "Pending", "Under Review", "Approved", "Rejected", "Completed",
    name="requeststatus",
    create_type=False,
)


def upgrade() -> None:
    request_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "citizens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("national_id", sa.String(30), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_unique_constraint("uq_citizens_national_id", "citizens", ["national_id"])
    op.create_unique_constraint("uq_citizens_email", "citizens", ["email"])
    op.create_index("ix_citizens_national_id", "citizens", ["national_id"])
    op.create_index("ix_citizens_email", "citizens", ["email"])

    op.create_table(
        "services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("required_documents", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("estimated_processing_days", sa.Integer(), nullable=False, server_default="7"),
    )
    op.create_unique_constraint("uq_services_name", "services", ["name"])

    op.create_table(
        "requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("citizen_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("citizens.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("services.id"), nullable=False),
        sa.Column("status", request_status, nullable=False, server_default="Pending"),
        sa.Column("submission_date", sa.DateTime(), nullable=False),
        sa.Column("last_update", sa.DateTime(), nullable=False),
        sa.Column("employee_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_requests_citizen_id", "requests", ["citizen_id"])
    op.create_index("ix_requests_service_id", "requests", ["service_id"])

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("requests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_documents_request_id", "documents", ["request_id"])


def downgrade() -> None:
    op.drop_table("documents")
    op.drop_table("requests")
    op.drop_table("services")
    op.drop_table("citizens")
    request_status.drop(op.get_bind(), checkfirst=True)
