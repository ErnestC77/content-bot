"""content_tasks: несколько независимых цитат вместо одного quote_text

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_quotes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("content_tasks.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(op.f("ix_task_quotes_task_id"), "task_quotes", ["task_id"], unique=False)

    # переносим уже сохранённые quote_text (по одному на пост) в новую таблицу,
    # чтобы существующие цитаты не потерялись при миграции
    op.execute(
        """
        INSERT INTO task_quotes (task_id, text, created_at)
        SELECT id, quote_text, now() FROM content_tasks
        WHERE quote_text IS NOT NULL AND quote_text != ''
        """
    )
    op.drop_column("content_tasks", "quote_text")


def downgrade() -> None:
    op.add_column("content_tasks", sa.Column("quote_text", sa.Text(), nullable=True))
    # best-effort: возвращаем только самую раннюю цитату каждого поста —
    # даунгрейд схемы не может восстановить несколько цитат в одну колонку
    op.execute(
        """
        UPDATE content_tasks
        SET quote_text = q.text
        FROM (
            SELECT DISTINCT ON (task_id) task_id, text
            FROM task_quotes
            ORDER BY task_id, created_at ASC
        ) q
        WHERE content_tasks.id = q.task_id
        """
    )
    op.drop_index(op.f("ix_task_quotes_task_id"), table_name="task_quotes")
    op.drop_table("task_quotes")
