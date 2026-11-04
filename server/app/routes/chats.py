"""Ask: chats over one or more of the user's own recordings (pages + JSON API)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import chats, errors, i18n
from app.db import get_db
from app.models import Chat, ChatMessage, Recording, User
from app.settings_store import get_services
from app.web import render, require_user, resolve_locale, verify_csrf_header

router = APIRouter()

STATUS = {"chat_busy": 409, "chat_too_large": 413, "not_found": 404}


def fail(exc: errors.CodedError) -> JSONResponse:
    return JSONResponse(
        {"error": exc.code, "message": str(exc), **exc.params},
        status_code=STATUS.get(exc.code, 400),
    )


def owned_chat(db: Session, user: User, chat_id: int) -> Chat:
    chat = db.get(Chat, chat_id)
    if chat is None or chat.user_id != user.id:
        raise chats.ChatError("not_found")
    return chat


def message_dict(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "status": m.status,
        "error": errors.decode(m.error),
        "model": m.model,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def chat_dict(db: Session, chat: Chat, with_messages: bool = False) -> dict:
    recs = chats.sources(db, chat)
    out = {
        "id": chat.id,
        "title": chat.title,
        "updated_at": chat.updated_at.isoformat() if chat.updated_at else None,
        "page_url": f"/ask/{chat.id}",
        "meetings": [
            {"id": r.id, "title": r.title, "page_url": f"/recordings/{r.id}"} for r in recs
        ],
    }
    if with_messages:
        msgs = db.scalars(
            select(ChatMessage).where(ChatMessage.chat_id == chat.id).order_by(ChatMessage.id)
        )
        out["messages"] = [message_dict(m) for m in msgs]
    return out


def page(request: Request, user: User, chat_id: int | None):
    js = i18n.strings(resolve_locale(request, user), "ask.", "err.", "common.", "rec.")
    with_ids = request.query_params.get("with", "")  # preselected meetings
    return render(request, "ask.html", user, chat_id=chat_id, js_strings=js, with_ids=with_ids)


@router.get("/ask")
def ask_page(request: Request, user: Annotated[User, Depends(require_user)]):
    return page(request, user, None)


@router.get("/ask/{chat_id}")
def chat_page(
    chat_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        owned_chat(db, user, chat_id)
    except chats.ChatError:
        return render(request, "not_found.html", user, status_code=404)
    return page(request, user, chat_id)


@router.get("/api/chats")
def list_chats(
    db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]
):
    rows = db.scalars(
        select(Chat).where(Chat.user_id == user.id).order_by(Chat.updated_at.desc()).limit(50)
    )
    return [chat_dict(db, c) for c in rows]


@router.get("/api/chats/sources")
def chat_sources(
    db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(require_user)]
):
    """The meetings Ask can use (finished transcripts) with their size, and the limit."""
    size = chats.sizes(db, user.id)
    recs = db.scalars(
        select(Recording)
        .where(Recording.id.in_(list(size)) if size else Recording.id.is_(None))
        .order_by(Recording.created_at.desc())
    )
    return {
        "limit": get_services(db).chat_max_chars,
        "meetings": [
            {
                "id": r.id,
                "title": r.title,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "duration_seconds": r.duration_seconds,
                "chars": size[r.id],
            }
            for r in recs
        ],
    }


class NewChat(BaseModel):
    recording_ids: list[str]
    question: str


class Question(BaseModel):
    question: str


def clean(q: str) -> str:
    return (q or "").strip()[:4000]


@router.post("/api/chats", status_code=201, dependencies=[Depends(verify_csrf_header)])
def new_chat(
    body: NewChat,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    question = clean(body.question)
    if not question:
        return fail(chats.ChatError("no_question"))
    try:
        chat = chats.create(db, user.id, body.recording_ids, question, get_services(db))
    except chats.ChatError as exc:
        return fail(exc)
    return chat_dict(db, chat, with_messages=True)


@router.get("/api/chats/{chat_id}")
def get_chat(
    chat_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        return chat_dict(db, owned_chat(db, user, chat_id), with_messages=True)
    except chats.ChatError as exc:
        return fail(exc)


@router.post(
    "/api/chats/{chat_id}/messages", status_code=202, dependencies=[Depends(verify_csrf_header)]
)
def follow_up(
    chat_id: int,
    body: Question,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    question = clean(body.question)
    if not question:
        return fail(chats.ChatError("no_question"))
    try:
        chat = owned_chat(db, user, chat_id)
        chats.ask(db, chat, question)
    except chats.ChatError as exc:
        return fail(exc)
    return chat_dict(db, chat, with_messages=True)


@router.delete("/api/chats/{chat_id}", dependencies=[Depends(verify_csrf_header)])
def delete_chat(
    chat_id: int,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(require_user)],
):
    try:
        chat = owned_chat(db, user, chat_id)
    except chats.ChatError as exc:
        return fail(exc)
    db.delete(chat)
    db.commit()
    return {"ok": True}
