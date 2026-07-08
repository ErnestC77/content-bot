"""content_tasks: тип задачи (пост/опрос) + необязательная связь с постом

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content_tasks",
        sa.Column("task_type", sa.String(length=20), nullable=False, server_default="post"),
    )
    op.create_index(
        op.f("ix_content_tasks_task_type"), "content_tasks", ["task_type"], unique=False
    )
    op.add_column(
        "content_tasks",
        sa.Column("related_task_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_content_tasks_related_task_id",
        "content_tasks", "content_tasks",
        ["related_task_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_content_tasks_related_task_id", "content_tasks", type_="foreignkey")
    op.drop_column("content_tasks", "related_task_id")
    op.drop_index(op.f("ix_content_tasks_task_type"), table_name="content_tasks")
    op.drop_column("content_tasks", "task_type")
