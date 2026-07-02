"""content_tasks: наводящие вопросы + флаг цитаты

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("content_tasks", sa.Column("pending_questions", sa.Text(), nullable=True))
    op.add_column(
        "content_tasks",
        sa.Column("is_quote", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("content_tasks", "is_quote")
    op.drop_column("content_tasks", "pending_questions")
