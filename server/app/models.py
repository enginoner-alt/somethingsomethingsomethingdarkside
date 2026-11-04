from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, utcnow


class AuthSource:
    LOCAL = "local"
    OIDC = "oidc"
    LDAP = "ldap"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"),
        UniqueConstraint("ad_guid", name="uq_users_ad_guid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    # Null for users that only log in via OIDC.
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    locale: Mapped[str | None] = mapped_column(String(10))
    auth_source: Mapped[str] = mapped_column(String(10), default=AuthSource.LOCAL)
    # OIDC identity: (issuer, subject) is the stable key, never the username.
    oidc_issuer: Mapped[str | None] = mapped_column(String(500))
    oidc_subject: Mapped[str | None] = mapped_column(String(255))
    # Active Directory objectGUID (lowercase, no braces). Links the same
    # person's SSO and direct-AD logins to one account.
    ad_guid: Mapped[str | None] = mapped_column(String(36))
    # Bumped to revoke all of this user's sessions (disable, password reset).
    auth_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginFailure(Base):
    """One row per failed local login, for throttling. Old rows are pruned."""

    __tablename__ = "login_failures"
    __table_args__ = (
        Index("ix_login_failures_ip_created", "ip", "created_at"),
        Index("ix_login_failures_user_ip", "username", "ip"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150))
    ip: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Queue:
    # GPU work (WhisperX). Concurrency 1 in the POC.
    GPU = "gpu"
    # LLM calls (insights). No GPU in this process; the LLM is a remote API.
    LLM = "llm"

    ALL = (GPU, LLM)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_queue_status_id", "queue", "status", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    queue: Mapped[str] = mapped_column(String(20))
    type: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    """Runtime settings edited in the admin panel (never secrets)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RecordingStatus:
    RECORDING = "recording"  # chunks still arriving
    UPLOADED = "uploaded"  # finalized: complete file on disk
    INTERRUPTED = "interrupted"  # browser vanished; kept what arrived
    FAILED = "failed"  # file unreadable (see error)


class Recording(Base):
    __tablename__ = "recordings"
    __table_args__ = (Index("ix_recordings_user_created", "user_id", "created_at"),)

    # Random UUID: not guessable, safe to put in URLs.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default=RecordingStatus.RECORDING)
    source: Mapped[str] = mapped_column(String(10), default="browser")  # browser | upload
    mime_type: Mapped[str] = mapped_column(String(100))
    file_ext: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    channels: Mapped[int | None] = mapped_column(Integer)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_chunk_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Soft delete: in the trash until purged after TRASH_RETENTION_DAYS.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecordingChunk(Base):
    """Bookkeeping for chunk uploads: makes PUTs idempotent and lets
    finalize check that 0..n all arrived. The bytes live on disk."""

    __tablename__ = "recording_chunks"

    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transcript(Base):
    """One per recording; replaced when transcribed again."""

    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[str] = mapped_column(
        ForeignKey("recordings.id", ondelete="CASCADE"), unique=True
    )
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed
    language: Mapped[str | None] = mapped_column(String(10))
    requested_language: Mapped[str | None] = mapped_column(String(10))
    channel: Mapped[str] = mapped_column(String(10), default="mix")
    error: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # the service's JSON, as received
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (Index("ix_segments_transcript_idx", "transcript_id", "idx"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    transcript_id: Mapped[int] = mapped_column(ForeignKey("transcripts.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    start: Mapped[float | None] = mapped_column(Float)
    end: Mapped[float | None] = mapped_column(Float)
    speaker: Mapped[str | None] = mapped_column(String(50))  # label, e.g. SPEAKER_00
    text: Mapped[str] = mapped_column(Text)
    words: Mapped[list[Any] | None] = mapped_column(JSON)


class Speaker(Base):
    """Display name for a diarization label, per recording."""

    __tablename__ = "speakers"
    __table_args__ = (UniqueConstraint("recording_id", "label", name="uq_speakers_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(50))
    name: Mapped[str | None] = mapped_column(String(100))


class Insight(Base):
    """One LLM answer about a recording (a default button or the user's question)."""

    __tablename__ = "insights"
    __table_args__ = (Index("ix_insights_recording", "recording_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(
        String(30)
    )  # summary|subject|speakers|action_items|decisions|question
    question: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed
    answer: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON
    )  # structured extras (speaker suggestions)
    model: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
