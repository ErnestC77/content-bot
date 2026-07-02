"""content_tasks: отдельные дата/время подготовки черновика

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_tasks", sa.Column("draft_date", sa.Date(), nullable=True))
    op.add_column("content_tasks", sa.Column("draft_time", sa.Time(), nullable=True))
    op.create_index("ix_content_tasks_draft_date", "content_tasks", ["draft_date"])


def downgrade() -> None:
    op.drop_index("ix_content_tasks_draft_date", table_name="content_tasks")
    op.drop_column("content_tasks", "draft_time")
    op.drop_column("content_tasks", "draft_date")
