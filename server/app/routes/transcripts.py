"""Recording detail: transcript, speakers, insights (JSON API + page)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import i18n, insights, pipeline, recordings
from app.db import get_db
from app.models import Insight, Recording, Segment, Speaker, Transcript, User
from app.recordings import RecordingError
from app.routes.recordings import as_dict, error_response
from app.web import render, require_user, resolve_locale, verify_csrf_header

router = APIRouter()


def owned(db: Session, user: User, rec_id: str) -> Recording:
    return recordings.get_owned(db, user, rec_id)


def transcript_dict(db: Session, rec: Recording) -> dict:
    tr = db.scalar(select(Transcript).where(Transcript.recording_id == rec.id))
    if tr is None:
        return {"status": "none"}
    names = {
        sp.label: sp.name
        for sp in db.scalars(select(Speaker).where(Speaker.recording_id == rec.id))
    }
    segs = list(
        db.scalars(select(Segment).where(Segment.transcript_id == tr.id).order_by(Segment.idx))
    )
    return {
        "status": tr.status,
        "language": tr.language,
        "channel": tr.channel,
        "error": tr.error,
        "speakers": [{"label": k, "name": v} for k, v in sorted(names.items())],
        "segments": [
            {"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text} for s in segs
        ],
    }


def insight_dict(ins: Insight) -> dict:
    return {
        "id": ins.id,
        "kind": ins.kind,
        "question": ins.question,
        "status": ins.status,
        "answer": ins.answer,
        "data": ins.data,
        "model": ins.model,
        "error": ins.error,
        "created_at": ins.created_at.isoformat() if ins.created_at else None,
    }


class TranscribeBody(BaseModel):
    language: str | None = None
    channel: str | None = None


class InsightBody(BaseModel):
    kind: str
    question: str | None = None


class SpeakersBody(BaseModel):
    names: dict[str, str]


@router.get("/recordings/{rec_id}")
def recording_page(
    rec_id: str,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = owned(db, user, rec_id)
    except RecordingError:
        return render(request, "not_found.html", user, status_code=404)
    locale = resolve_locale(request, user)
    js = {
        k: i18n.translate(locale, k)
        for k in i18n.load_locales()["en"]
        if k.startswith(("tx.", "rec."))
    }
    return render(request, "recording.html", user, rec=as_dict(rec), js_strings=js)


@router.get("/api/recordings/{rec_id}/transcript")
def get_transcript(
    rec_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = owned(db, user, rec_id)
    except RecordingError as exc:
        return error_response(exc)
    return {"recording": as_dict(rec), "transcript": transcript_dict(db, rec)}


@router.post(
    "/api/recordings/{rec_id}/transcribe",
    status_code=202,
    dependencies=[Depends(verify_csrf_header)],
)
def transcribe(
    rec_id: str,
    body: TranscribeBody,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = owned(db, user, rec_id)
    except RecordingError as exc:
        return error_response(exc)
    if not rec.finalized_at or rec.error:
        return JSONResponse({"error": "not_ready", "message": "recording isn't saved yet"}, 409)
    if body.channel and body.channel != "mix" and not body.channel.isdigit():
        return JSONResponse(
            {"error": "bad_channel", "message": "channel must be 'mix' or a number"}, 400
        )
    tr = pipeline.request_transcription(db, rec, body.language, body.channel)
    return {"status": tr.status}


@router.patch("/api/recordings/{rec_id}/speakers", dependencies=[Depends(verify_csrf_header)])
def rename_speakers(
    rec_id: str,
    body: SpeakersBody,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = owned(db, user, rec_id)
    except RecordingError as exc:
        return error_response(exc)
    by_label = {
        sp.label: sp for sp in db.scalars(select(Speaker).where(Speaker.recording_id == rec.id))
    }
    for label, name in body.names.items():
        if label in by_label:
            by_label[label].name = (name or "").strip()[:100] or None
    db.commit()
    return transcript_dict(db, rec)["speakers"]


@router.get("/api/recordings/{rec_id}/insights")
def list_insights(
    rec_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = owned(db, user, rec_id)
    except RecordingError as exc:
        return error_response(exc)
    rows = db.scalars(
        select(Insight).where(Insight.recording_id == rec.id).order_by(Insight.created_at.desc())
    )
    return [insight_dict(i) for i in rows]


@router.post(
    "/api/recordings/{rec_id}/insights", status_code=202, dependencies=[Depends(verify_csrf_header)]
)
def ask(
    rec_id: str,
    body: InsightBody,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = owned(db, user, rec_id)
    except RecordingError as exc:
        return error_response(exc)
    if body.kind not in insights.KINDS:
        return JSONResponse({"error": "bad_kind", "message": f"unknown insight {body.kind!r}"}, 400)
    question = (body.question or "").strip()[:2000] or None
    if body.kind == "question" and not question:
        return JSONResponse({"error": "no_question", "message": "type a question first"}, 400)
    tr = db.scalar(select(Transcript).where(Transcript.recording_id == rec.id))
    if tr is None or tr.status != "done":
        return JSONResponse(
            {"error": "no_transcript", "message": "no finished transcript yet"}, 409
        )
    ins = pipeline.request_insight(db, rec, body.kind, question, user.id)
    return insight_dict(ins)


def owned_insight(db: Session, user: User, insight_id: int) -> Insight:
    ins = db.get(Insight, insight_id)
    rec = db.get(Recording, ins.recording_id) if ins else None
    if ins is None or rec is None or rec.user_id != user.id or rec.deleted_at is not None:
        raise RecordingError("not_found", "insight not found")
    return ins


@router.delete("/api/insights/{insight_id}", dependencies=[Depends(verify_csrf_header)])
def delete_insight(
    insight_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        ins = owned_insight(db, user, insight_id)
    except RecordingError as exc:
        return error_response(exc)
    db.delete(ins)
    db.commit()
    return {"deleted": insight_id}


@router.post(
    "/api/insights/{insight_id}/apply-speakers", dependencies=[Depends(verify_csrf_header)]
)
def apply_speakers(
    insight_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    """Use the 'Speakers' insight's suggestions as the speakers' names."""
    try:
        ins = owned_insight(db, user, insight_id)
    except RecordingError as exc:
        return error_response(exc)
    suggestions = (ins.data or {}).get("suggestions") or []
    by_label = {
        sp.label: sp
        for sp in db.scalars(select(Speaker).where(Speaker.recording_id == ins.recording_id))
    }
    applied = 0
    for item in suggestions:
        sp = by_label.get(item.get("label"))
        if sp is not None and item.get("name"):
            sp.name = str(item["name"])[:100]
            applied += 1
    db.commit()
    return {"applied": applied}
