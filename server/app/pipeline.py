"""Background jobs for M3: transcription (gpu queue, worker-asr) and
insights (llm queue, worker-llm). Imported by app.worker to register them.

Errors are recorded on the transcript / insight row (shown to the user) and
the job ends; the user can press "again". Nothing is retried blindly.
"""

import logging
import tempfile
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import audio_prep, errors, insights, jobs, recordings, services
from app.db import utcnow
from app.models import Insight, Job, Queue, Recording, Segment, Speaker, Transcript
from app.registry import handler
from app.settings_store import get_services

log = logging.getLogger("pipeline")


# --- requesting work (api side) -------------------------------------------------------


def request_transcription(
    db: Session, rec: Recording, language: str | None = None, channel: str | None = None
) -> Transcript:
    s = get_services(db)
    tr = db.scalar(select(Transcript).where(Transcript.recording_id == rec.id))
    if tr is None:
        tr = Transcript(recording_id=rec.id)
        db.add(tr)
    tr.status, tr.error, tr.finished_at = "queued", None, None
    tr.requested_language = language or s.asr_language
    tr.channel = channel or s.default_channel
    db.commit()
    jobs.enqueue(
        db, Queue.GPU, "transcribe", {"recording_id": rec.id}, user_id=rec.user_id, max_attempts=1
    )
    return tr


def maybe_auto_transcribe(db: Session, rec: Recording) -> None:
    """Called when a recording is saved (finalized, interrupted, uploaded)."""
    if rec.finalized_at and not rec.error and get_services(db).auto_transcribe:
        request_transcription(db, rec)


def request_insight(
    db: Session, rec: Recording, kind: str, question: str | None, user_id: int
) -> Insight:
    ins = Insight(
        recording_id=rec.id, user_id=user_id, kind=kind, question=question, status="queued"
    )
    db.add(ins)
    db.commit()
    jobs.enqueue(db, Queue.LLM, "insight", {"insight_id": ins.id}, user_id=user_id, max_attempts=1)
    return ins


# --- doing work (worker side) ---------------------------------------------------------------


@handler("transcribe")
def transcribe_job(db: Session, job: Job) -> dict:
    rec = db.get(Recording, job.payload["recording_id"])
    tr = db.scalar(select(Transcript).where(Transcript.recording_id == job.payload["recording_id"]))
    if rec is None or tr is None or rec.deleted_at is not None:
        return {"skipped": "recording gone"}
    tr.status = "running"
    db.commit()
    s = get_services(db)
    try:
        with tempfile.TemporaryDirectory(prefix="darkside-asr-") as tmp:
            flac = audio_prep.prepare(
                recordings.original_path(rec), Path(tmp) / f"{rec.id}.flac", tr.channel
            )
            language = services.choose_language(s, flac, tr.requested_language)
            result = services.transcribe(s, flac, language)
            if not language and result.get("language") in s.asr_banned_languages:
                # Detection didn't run (or failed): the service guessed a banned language.
                result = services.transcribe(s, flac, s.asr_fallback_language or None)
    except (audio_prep.PrepError, services.ServiceError) as exc:
        tr.status, tr.error, tr.finished_at = "failed", exc.stored(), utcnow()
        db.commit()
        log.warning("transcription of %s failed: %s", rec.id, exc)
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        tr.status, tr.error, tr.finished_at = "failed", errors.encode("os", str(exc)), utcnow()
        db.commit()
        log.warning("transcription of %s failed: %s", rec.id, exc)
        return {"ok": False, "error": str(exc)}

    db.execute(delete(Segment).where(Segment.transcript_id == tr.id))
    labels: set[str] = set()
    for i, seg in enumerate(result["segments"]):
        speaker = seg.get("speaker")
        if speaker:
            labels.add(speaker)
        db.add(
            Segment(
                transcript_id=tr.id,
                idx=i,
                start=seg.get("start"),
                end=seg.get("end"),
                speaker=speaker,
                text=str(seg.get("text", "")).strip(),
                words=seg.get("words"),
            )
        )
    known = {sp.label for sp in db.scalars(select(Speaker).where(Speaker.recording_id == rec.id))}
    for label in sorted(labels - known):  # keep names given earlier to the same labels
        db.add(Speaker(recording_id=rec.id, label=label))
    tr.raw, tr.language = result, result.get("language") or tr.requested_language
    tr.status, tr.finished_at = "done", utcnow()
    tr.rev = (tr.rev or 0) + 1  # new text: insights made from the old one are outdated
    if s.asr_diarize and not labels:
        tr.error = errors.encode("no_speakers")
    db.commit()
    log.info(
        "transcribed %s: %d segments, %d speakers, %s",
        rec.id,
        len(result["segments"]),
        len(labels),
        tr.language,
    )
    return {"ok": True, "segments": len(result["segments"]), "speakers": len(labels)}


@handler("insight")
def insight_job(db: Session, job: Job) -> dict:
    ins = db.get(Insight, job.payload["insight_id"])
    if ins is None:
        return {"skipped": "insight gone"}
    ins.status = "running"
    db.commit()
    try:
        insights.run(db, ins, get_services(db))
    except services.ServiceError as exc:
        db.rollback()
        ins = db.get(Insight, job.payload["insight_id"])
        ins.status, ins.error, ins.finished_at = "failed", exc.stored(), utcnow()
        db.commit()
        return {"ok": False, "error": str(exc)}
    ins.status, ins.finished_at = "done", utcnow()
    db.commit()
    return {"ok": True, "kind": ins.kind}
