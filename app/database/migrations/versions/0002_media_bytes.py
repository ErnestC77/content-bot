"""task_media: хранение байтов загруженного медиа (Mini App)

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-02

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_media", sa.Column("content", sa.LargeBinary(), nullable=True))
    op.add_column("task_media", sa.Column("mime_type", sa.String(100), nullable=True))
    op.alter_column("task_media", "telegram_file_id", existing_type=sa.String(512), nullable=True)


def downgrade() -> None:
    op.alter_column("task_media", "telegram_file_id", existing_type=sa.String(512), nullable=False)
    op.drop_column("task_media", "mime_type")
    op.drop_column("task_media", "content")
