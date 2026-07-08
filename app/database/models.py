import enum
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TaskStatus(str, enum.Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    WAITING_FOR_ANSWERS = "waiting_for_answers"
    COLLECTING_MEDIA = "collecting_media"
    GENERATING = "generating"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    REVISION_REQUESTED = "revision_requested"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    CANCELLED = "cancelled"


class ApprovalAction(str, enum.Enum):
    DRAFT_GENERATED = "draft_generated"
    SENT_FOR_APPROVAL = "sent_for_approval"
    REVISION_REQUESTED = "revision_requested"
    ALTERNATIVE_REQUESTED = "alternative_requested"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    PUBLISH_STARTED = "publish_started"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"


class MediaType(str, enum.Enum):
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"


class UserRole(str, enum.Enum):
    OWNER = "owner"
    ADMIN = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(20), default=UserRole.OWNER.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    telegram_channel_id: Mapped[str] = mapped_column(String(64))
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ContentTask(Base):
    __tablename__ = "content_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channels.id"), nullable=True)
    publish_date: Mapped[date] = mapped_column(Date, index=True)
    publish_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # когда бот готовит черновик (независимо от даты/времени публикации)
    draft_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    draft_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    # наводящие вопросы текущего раунда (по одному на строку), пока не отвечены
    pending_questions: Mapped[str | None] = mapped_column(Text, nullable=True)
    # публиковать весь текст как цитату (Telegram blockquote)
    is_quote: Mapped[bool] = mapped_column(Boolean, default=False)
    # либо отдельные строки текста — как цитаты (взаимоисключимо с is_quote);
    # несколько выбранных строк хранятся через "\n", каждая — независимый фрагмент
    quote_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    rubric: Mapped[str] = mapped_column(String(255), default="")
    topic: Mapped[str] = mapped_column(String(500), default="")
    goal: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.SCHEDULED.value, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    recurrence: Mapped[str] = mapped_column(String(20), default="none")  # none | weekly
    final_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_post_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    channel: Mapped[Channel | None] = relationship(lazy="selectin")
    answers: Mapped[list["TaskAnswer"]] = relationship(
        back_populates="task", lazy="selectin", order_by="TaskAnswer.created_at"
    )
    media: Mapped[list["TaskMedia"]] = relationship(
        back_populates="task", lazy="selectin", order_by="TaskMedia.sort_order"
    )
    posts: Mapped[list["GeneratedPost"]] = relationship(
        back_populates="task", lazy="selectin", order_by="GeneratedPost.version_number"
    )
    logs: Mapped[list["ApprovalLog"]] = relationship(
        back_populates="task", lazy="selectin", order_by="ApprovalLog.created_at"
    )


class TaskAnswer(Base):
    __tablename__ = "task_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    answer_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[ContentTask] = relationship(back_populates="answers")


class TaskMedia(Base):
    __tablename__ = "task_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    # либо telegram_file_id (из чата), либо content+mime_type (загрузка из Mini App)
    telegram_file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    media_type: Mapped[str] = mapped_column(String(20))
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[ContentTask] = relationship(back_populates="media")


class GeneratedPost(Base):
    __tablename__ = "generated_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    text: Mapped[str] = mapped_column(Text)
    generation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_provider: Mapped[str] = mapped_column(String(50), default="")
    ai_model: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[ContentTask] = relationship(back_populates="posts")


class ApprovalLog(Base):
    __tablename__ = "approval_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("content_tasks.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(50))
    old_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[ContentTask] = relationship(back_populates="logs")


class AppSetting(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
