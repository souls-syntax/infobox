from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Email(Base):
    __tablename__ = "emails"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gmail_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    thread_id: Mapped[str] = mapped_column(String, default="")
    sender: Mapped[str] = mapped_column(String, default="")
    sender_email: Mapped[str] = mapped_column(String, default="")
    recipient: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(String, default="(no subject)")
    body: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_html_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    classification: Mapped[str | None] = mapped_column(String, nullable=True)
    classification_source: Mapped[str | None] = mapped_column(String, nullable=True)
    classification_status: Mapped[str] = mapped_column(String, default="pending")
    drafts: Mapped[list["Draft"]] = relationship(back_populates="email", cascade="all, delete-orphan")


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("email_id", name="uq_draft_email"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String, default="generated")
    source: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    email: Mapped[Email] = relationship(back_populates="drafts")


class OAuthCredential(Base):
    __tablename__ = "oauth_credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    token_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
