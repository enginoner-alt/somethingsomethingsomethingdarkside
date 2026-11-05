"""Recordings: chunked browser uploads, finalize, existing-file uploads, trash.

Files (never in the database, never in the image):

    {AUDIO_DIR}/{user_id}/{recording_id}/chunks/000000.part ...   while recording
    {AUDIO_DIR}/{user_id}/{recording_id}/original.{webm|ogg|mp4|...} after finalize

Browsers' MediaRecorder output is one stream cut into pieces: only the first
chunk carries the container header, so chunks must be joined in order, all
of them, before the file is valid. Finalize checks 0..last are present,
concatenates, and validates the result with ffprobe.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import utcnow
from app.models import Recording, RecordingChunk, RecordingStatus, User

log = logging.getLogger("recordings")

# MIME type (without codecs) -> file extension. Browsers: Chrome/Edge/Android
# webm, Firefox ogg, Safari/iOS mp4. Uploads may also be mp3/wav/m4a/flac.
MIME_EXT = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/x-m4a": "m4a",
    "audio/m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/flac": "flac",
    "video/webm": "webm",  # some browsers label audio-only webm as video
    "video/mp4": "mp4",
}
UPLOAD_EXT = {"webm", "ogg", "opus", "mp4", "m4a", "mp3", "wav", "flac", "aac"}
MAX_SEQ = 100_000


class RecordingError(Exception):
    """A request we refuse; `code` maps to an HTTP status in the routes."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def base_mime(mime: str) -> str:
    return (mime or "").split(";")[0].strip().lower()


def rec_dir(rec: Recording) -> Path:
    return Path(get_config().audio_dir) / str(rec.user_id) / rec.id


def chunk_path(rec: Recording, seq: int) -> Path:
    return rec_dir(rec) / "chunks" / f"{seq:06d}.part"


def original_path(rec: Recording) -> Path:
    return rec_dir(rec) / f"original.{rec.file_ext}"


def used_bytes(db: Session, user_id: int) -> int:
    finished = db.scalar(
        select(func.coalesce(func.sum(Recording.size_bytes), 0)).where(Recording.user_id == user_id)
    )
    in_progress = db.scalar(
        select(func.coalesce(func.sum(RecordingChunk.size_bytes), 0))
        .join(Recording, Recording.id == RecordingChunk.recording_id)
        .where(Recording.user_id == user_id, Recording.status == RecordingStatus.RECORDING)
    )
    return int(finished) + int(in_progress)


def check_quota(db: Session, user_id: int, extra: int) -> None:
    quota = get_config().user_quota_mb * 1024 * 1024
    if quota and used_bytes(db, user_id) + extra > quota:
        raise RecordingError("quota", "storage quota reached")


def get_owned(db: Session, user: User, rec_id: str) -> Recording:
    """The user's own, not-deleted recording, or 404. Never reveals whether
    someone else's recording with that id exists."""
    rec = db.get(Recording, rec_id)
    if rec is None or rec.user_id != user.id or rec.deleted_at is not None:
        raise RecordingError("not_found", "recording not found")
    return rec


# --- browser recording --------------------------------------------------------------


def create(db: Session, user: User, mime_type: str, title: str, consent: bool) -> Recording:
    mime = base_mime(mime_type)
    if mime not in MIME_EXT:
        raise RecordingError("bad_type", f"unsupported audio type {mime_type!r}")
    if not consent:
        raise RecordingError("consent", "consent notice not acknowledged")
    check_quota(db, user.id, 0)
    rec = Recording(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title=(title or "").strip()[:200] or utcnow().strftime("Recording %Y-%m-%d %H:%M"),
        mime_type=mime,
        file_ext=MIME_EXT[mime],
        consent_at=utcnow(),
        status=RecordingStatus.RECORDING,
    )
    db.add(rec)
    db.commit()
    (rec_dir(rec) / "chunks").mkdir(parents=True, exist_ok=True)
    log.info("recording %s started by user %s (%s)", rec.id, user.id, mime)
    return rec


def put_chunk(db: Session, rec: Recording, seq: int, data: bytes) -> str:
    """Store chunk `seq`. Same bytes again: no-op ("duplicate"), so the browser
    can safely retry. Different bytes for an existing seq: refused."""
    cfg = get_config()
    if rec.status != RecordingStatus.RECORDING:
        raise RecordingError("closed", f"recording is {rec.status}")
    if not 0 <= seq <= MAX_SEQ:
        raise RecordingError("bad_seq", "chunk number out of range")
    if not data:
        raise RecordingError("empty", "empty chunk")
    if len(data) > cfg.max_chunk_bytes:
        raise RecordingError("too_large", "chunk too large")
    if utcnow() - rec.created_at > timedelta(seconds=cfg.max_recording_seconds + 120):
        raise RecordingError("too_long", "maximum recording length reached")

    digest = hashlib.sha256(data).hexdigest()
    existing = db.get(RecordingChunk, (rec.id, seq))
    if existing is not None:
        if existing.sha256 == digest:
            return "duplicate"
        raise RecordingError("conflict", f"chunk {seq} already received with different content")

    check_quota(db, rec.user_id, len(data))
    path = chunk_path(rec, seq)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # atomic: a half-written chunk is never visible
    db.add(RecordingChunk(recording_id=rec.id, seq=seq, size_bytes=len(data), sha256=digest))
    rec.last_chunk_at = utcnow()
    db.commit()
    return "stored"


def _received(db: Session, rec: Recording) -> list[int]:
    return list(
        db.scalars(
            select(RecordingChunk.seq)
            .where(RecordingChunk.recording_id == rec.id)
            .order_by(RecordingChunk.seq)
        )
    )


def _assemble(db: Session, rec: Recording, seqs: list[int], status: str) -> Recording:
    out = original_path(rec)
    tmp = out.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        for seq in seqs:
            with chunk_path(rec, seq).open("rb") as part:
                shutil.copyfileobj(part, fh)
    os.replace(tmp, out)
    rec.size_bytes = out.stat().st_size
    _apply_probe(rec, out)
    if rec.error is None:
        rec.status = status
    rec.finalized_at = utcnow()
    db.query(RecordingChunk).filter(RecordingChunk.recording_id == rec.id).delete()
    db.commit()
    shutil.rmtree(rec_dir(rec) / "chunks", ignore_errors=True)
    log.info(
        "recording %s %s: %d chunks, %d bytes, %.1f s, %s ch",
        rec.id,
        rec.status,
        len(seqs),
        rec.size_bytes,
        rec.duration_seconds or 0,
        rec.channels,
    )
    return rec


def finalize(db: Session, rec: Recording, last_seq: int) -> Recording:
    """Browser pressed Stop and every chunk was acked. Checks 0..last_seq are
    all here; if not, says which are missing so the browser can resend."""
    if rec.status != RecordingStatus.RECORDING:
        return rec  # already finalized: idempotent
    have = set(_received(db, rec))
    missing = [s for s in range(last_seq + 1) if s not in have]
    if missing:
        raise RecordingError("missing", "chunks missing", missing=missing[:1000])
    extra = sorted(s for s in have if s > last_seq)
    if extra:
        raise RecordingError("conflict", f"chunks after last_seq exist: {extra[:10]}")
    return _assemble(db, rec, list(range(last_seq + 1)), RecordingStatus.UPLOADED)


def close_stale(db: Session, user_id: int | None = None) -> list[Recording]:
    """Recordings whose browser went away (no chunk for RECORDING_STALE_SECONDS):
    keep the unbroken run of chunks from 0 and mark them "interrupted".
    Nothing is thrown away silently."""
    cutoff = utcnow() - timedelta(seconds=get_config().recording_stale_seconds)
    q = select(Recording.id).where(Recording.status == RecordingStatus.RECORDING)
    if user_id is not None:
        q = q.where(Recording.user_id == user_id)
    closed: list[Recording] = []
    for rec_id in db.scalars(q).all():
        # Two requests (say, the list page and another tab) can get here at once:
        # lock the row; if another request was closing it, this waits for it and
        # then finds it already closed. One row at a time, so no deadlocks.
        rec = db.scalar(
            select(Recording)
            .where(Recording.id == rec_id, Recording.status == RecordingStatus.RECORDING)
            .with_for_update()
        )
        if rec is None:
            continue
        last = rec.last_chunk_at or rec.created_at
        if last > cutoff:
            db.commit()  # release the lock
            continue
        seqs = []
        for s in _received(db, rec):
            if s != len(seqs):
                break  # stop at the first gap: later chunks can't be decoded without it
            seqs.append(s)
        if not seqs:
            rec.status, rec.error, rec.finalized_at = (
                RecordingStatus.INTERRUPTED,
                "no audio received",
                utcnow(),
            )
            db.commit()
        else:
            _assemble(db, rec, seqs, RecordingStatus.INTERRUPTED)
        closed.append(rec)
    return closed


# --- existing files ----------------------------------------------------------------


def upload_file(db: Session, user: User, filename: str, stream, title: str, consent: bool):
    cfg = get_config()
    ext = Path(filename or "").suffix.lower().lstrip(".")
    if ext not in UPLOAD_EXT:
        raise RecordingError("bad_type", f"unsupported file type .{ext}")
    if not consent:
        raise RecordingError("consent", "consent notice not acknowledged")
    check_quota(db, user.id, 0)
    rec = Recording(
        id=str(uuid.uuid4()),
        user_id=user.id,
        title=(title or "").strip()[:200] or Path(filename).stem[:200],
        source="upload",
        mime_type=f"audio/{ext}",
        file_ext=ext,
        consent_at=utcnow(),
        status=RecordingStatus.RECORDING,
    )
    rec_dir(rec).mkdir(parents=True, exist_ok=True)
    out = original_path(rec)
    size = 0
    with out.open("wb") as fh:
        while block := stream.read(1024 * 1024):
            size += len(block)
            if size > cfg.max_upload_bytes:
                fh.close()
                shutil.rmtree(rec_dir(rec), ignore_errors=True)
                raise RecordingError("too_large", "file too large")
            fh.write(block)
    try:
        check_quota(db, user.id, size)
    except RecordingError:
        shutil.rmtree(rec_dir(rec), ignore_errors=True)
        raise
    rec.size_bytes = size
    _apply_probe(rec, out)
    if rec.error:
        shutil.rmtree(rec_dir(rec), ignore_errors=True)
        raise RecordingError("unreadable", rec.error)
    rec.status, rec.finalized_at = RecordingStatus.UPLOADED, utcnow()
    db.add(rec)
    db.commit()
    log.info(
        "upload %s by user %s: %s, %.1f s", rec.id, user.id, filename, rec.duration_seconds or 0
    )
    return rec


# --- inspection ------------------------------------------------------------------------


@dataclass
class Probe:
    duration: float | None
    channels: int | None
    sample_rate: int | None


def probe(path: Path) -> Probe:
    """ffprobe the file. Raises RecordingError if it isn't readable audio."""
    try:
        res = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,channels,sample_rate,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecordingError("unreadable", f"ffprobe failed: {exc}") from exc
    if res.returncode != 0:
        raise RecordingError("unreadable", (res.stderr or "ffprobe failed").strip()[:300])
    info = json.loads(res.stdout or "{}")
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise RecordingError("unreadable", "no audio stream")
    duration = info.get("format", {}).get("duration") or audio[0].get("duration")
    if duration in (None, "N/A"):
        # MediaRecorder webm has no duration in its header: decode to measure.
        duration = _decoded_duration(path)
    return Probe(
        duration=float(duration) if duration not in (None, "N/A") else None,
        channels=audio[0].get("channels"),
        sample_rate=int(audio[0]["sample_rate"]) if audio[0].get("sample_rate") else None,
    )


def _decoded_duration(path: Path) -> float | None:
    """Duration from the last audio packet (timestamp + its length). Needed
    because MediaRecorder's WebM doesn't write a duration into its header."""
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "packet=pts_time,duration_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    for line in reversed(res.stdout.strip().splitlines()):
        parts = [p for p in line.split(",") if p not in ("", "N/A")]
        if parts:
            return sum(float(p) for p in parts[:2])
    return None


def _apply_probe(rec: Recording, path: Path) -> None:
    try:
        p = probe(path)
        rec.duration_seconds, rec.channels, rec.sample_rate = p.duration, p.channels, p.sample_rate
        rec.error = None
    except RecordingError as exc:
        rec.status, rec.error = RecordingStatus.FAILED, exc.message


# --- trash ------------------------------------------------------------------------------


def delete(db: Session, rec: Recording) -> None:
    rec.deleted_at = utcnow()
    db.commit()


def purge_expired(db: Session) -> int:
    """Hard-delete recordings that sat in the trash longer than the retention."""
    cutoff = utcnow() - timedelta(days=get_config().trash_retention_days)
    gone = 0
    for rec in db.scalars(select(Recording).where(Recording.deleted_at < cutoff)).all():
        shutil.rmtree(rec_dir(rec), ignore_errors=True)
        db.delete(rec)
        gone += 1
    db.commit()
    return gone
