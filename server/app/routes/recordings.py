"""Recording API (JSON, for the recorder page) and the audio download."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import pipeline, recordings
from app.config import get_config
from app.db import get_db, like_pattern
from app.models import Recording, User
from app.recordings import RecordingError
from app.web import require_user, verify_csrf_header

router = APIRouter()

STATUS = {
    "not_found": 404,
    "bad_type": 415,
    "consent": 400,
    "quota": 413,
    "too_large": 413,
    "closed": 409,
    "too_long": 409,
    "conflict": 409,
    "missing": 409,
    "bad_seq": 400,
    "empty": 400,
    "unreadable": 422,
}


def error_response(exc: RecordingError) -> JSONResponse:
    return JSONResponse(
        {"error": exc.code, "message": exc.message, **exc.extra},
        status_code=STATUS.get(exc.code, 400),
    )


def as_dict(rec: Recording) -> dict:
    return {
        "id": rec.id,
        "title": rec.title,
        "status": rec.status,
        "source": rec.source,
        "mime_type": rec.mime_type,
        "size_bytes": rec.size_bytes,
        "duration_seconds": rec.duration_seconds,
        "channels": rec.channels,
        "sample_rate": rec.sample_rate,
        "error": rec.error,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "audio_url": f"/recordings/{rec.id}/audio" if rec.finalized_at and not rec.error else None,
        "page_url": f"/recordings/{rec.id}",
    }


def list_for(
    db: Session, user: User, q: str = "", limit: int | None = None, offset: int = 0
) -> tuple[list[Recording], int]:
    """The user's recordings, newest first; optionally a title search and one page."""
    for rec in recordings.close_stale(db, user.id):
        pipeline.maybe_auto_transcribe(db, rec)
    where = [Recording.user_id == user.id, Recording.deleted_at.is_(None)]
    if q.strip():
        pattern = like_pattern(q.strip())
        where.append(Recording.title.ilike(pattern, escape="\\"))
    total = db.scalar(select(func.count()).select_from(Recording).where(*where)) or 0
    query = select(Recording).where(*where).order_by(Recording.created_at.desc()).offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return list(db.scalars(query)), total


class CreateBody(BaseModel):
    mime_type: str
    title: str = ""
    consent: bool = False


class FinalizeBody(BaseModel):
    last_seq: int


@router.get("/api/recordings")
def api_list(
    response: Response,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    q: str = "",
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    """A JSON list as before; with ?limit= one page of it. X-Total-Count has the total."""
    rows, total = list_for(db, user, q, limit, offset)
    response.headers["X-Total-Count"] = str(total)
    return [as_dict(r) for r in rows]


@router.post("/api/recordings", status_code=201, dependencies=[Depends(verify_csrf_header)])
def api_create(
    body: CreateBody,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = recordings.create(db, user, body.mime_type, body.title, body.consent)
    except RecordingError as exc:
        return error_response(exc)
    return {**as_dict(rec), "chunk_seconds": get_config().recorder_chunk_seconds}


@router.put("/api/recordings/{rec_id}/chunks/{seq}", dependencies=[Depends(verify_csrf_header)])
async def api_put_chunk(
    rec_id: str,
    seq: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    limit = get_config().max_chunk_bytes
    declared = int(request.headers.get("content-length") or 0)
    if declared > limit:
        return error_response(RecordingError("too_large", "chunk too large"))
    data = await request.body()
    try:
        rec = recordings.get_owned(db, user, rec_id)
        result = recordings.put_chunk(db, rec, seq, data)
    except RecordingError as exc:
        return error_response(exc)
    return {"seq": seq, "result": result}


@router.post("/api/recordings/{rec_id}/finalize", dependencies=[Depends(verify_csrf_header)])
def api_finalize(
    rec_id: str,
    body: FinalizeBody,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = recordings.get_owned(db, user, rec_id)
        was_open = rec.finalized_at is None
        rec = recordings.finalize(db, rec, body.last_seq)
    except RecordingError as exc:
        return error_response(exc)
    if was_open:
        pipeline.maybe_auto_transcribe(db, rec)
    return as_dict(rec)


@router.post("/api/recordings/upload", status_code=201, dependencies=[Depends(verify_csrf_header)])
def api_upload(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form()] = "",
    consent: Annotated[bool, Form()] = False,
):
    try:
        rec = recordings.upload_file(db, user, file.filename or "", file.file, title, consent)
    except RecordingError as exc:
        return error_response(exc)
    pipeline.maybe_auto_transcribe(db, rec)
    return as_dict(rec)


@router.delete("/api/recordings/{rec_id}", dependencies=[Depends(verify_csrf_header)])
def api_delete(
    rec_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        recordings.delete(db, recordings.get_owned(db, user, rec_id))
    except RecordingError as exc:
        return error_response(exc)
    return {"deleted": rec_id}


@router.get("/recordings/{rec_id}/audio")
def audio(
    rec_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        rec = recordings.get_owned(db, user, rec_id)
    except RecordingError as exc:
        return error_response(exc)
    path = recordings.original_path(rec)
    if not rec.finalized_at or rec.error or not path.is_file():
        return error_response(RecordingError("not_found", "audio not available"))
    name = f"{rec.title}.{rec.file_ext}".replace('"', "")
    # FileResponse answers Range requests, so the <audio> element can seek.
    return FileResponse(
        path, media_type=rec.mime_type, filename=name, content_disposition_type="inline"
    )
