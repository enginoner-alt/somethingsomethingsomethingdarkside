"""Ask: chats with follow-ups over one or more of the user's recordings.

Every turn sends the LLM: the chat prompt (admin-editable, Prompts -> Ask) with
the selected meetings' transcripts, then the earlier turns, then the new
question. ServicesSettings.chat_max_chars caps the whole request: meetings that
don't fit can't be picked, and when a conversation grows, the oldest turns are
left out first (the meetings themselves always stay).
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import jobs, services
from app.db import utcnow
from app.errors import CodedError
from app.insights import transcript_lines
from app.models import Chat, ChatMessage, ChatSource, Job, Queue, Recording, Segment, Transcript
from app.registry import handler
from app.settings_store import ServicesSettings, get_prompts, get_services

log = logging.getLogger("chats")

# Per-line overhead of the transcript format ("[00:01:02] SPEAKER_00 (Name): ").
LINE_OVERHEAD = 32


class ChatError(CodedError):
    pass


def sizes(db: Session, user_id: int) -> dict[str, int]:
    """{recording id: transcript size in characters} for the user's recordings
    that have a finished transcript (the ones Ask can use)."""
    rows = db.execute(
        select(Recording.id, func.coalesce(func.sum(func.length(Segment.text) + LINE_OVERHEAD), 0))
        .join(Transcript, Transcript.recording_id == Recording.id)
        .join(Segment, Segment.transcript_id == Transcript.id, isouter=True)
        .where(
            Recording.user_id == user_id,
            Recording.deleted_at.is_(None),
            Transcript.status == "done",
        )
        .group_by(Recording.id)
    )
    return {rid: int(n) for rid, n in rows}


def sources(db: Session, chat: Chat) -> list[Recording]:
    return list(
        db.scalars(
            select(Recording)
            .join(ChatSource, ChatSource.recording_id == Recording.id)
            .where(ChatSource.chat_id == chat.id, Recording.deleted_at.is_(None))
            .order_by(ChatSource.position)
        )
    )


def material(db: Session, recs: list[Recording]) -> str:
    parts = []
    for i, rec in enumerate(recs, 1):
        lines, _ = transcript_lines(db, rec)
        when = rec.created_at.strftime("%Y-%m-%d %H:%M") if rec.created_at else ""
        parts.append(f"Meeting {i}: {rec.title} ({when})\n" + "\n".join(lines))
    return "\n\n".join(parts)


def create(
    db: Session, user_id: int, recording_ids: list[str], question: str, s: ServicesSettings
) -> Chat:
    ids = list(dict.fromkeys(recording_ids))  # keep order, drop repeats
    if not ids:
        raise ChatError("chat_no_meetings")
    usable = sizes(db, user_id)
    if any(rid not in usable for rid in ids):
        raise ChatError("chat_not_ready")
    used = sum(usable[rid] for rid in ids) + len(question)
    if used > s.chat_max_chars:
        raise ChatError("chat_too_large", used=used, limit=s.chat_max_chars)
    recs = {r.id: r for r in db.scalars(select(Recording).where(Recording.id.in_(ids)))}
    title = recs[ids[0]].title if len(ids) == 1 else question[:80]
    chat = Chat(user_id=user_id, title=title[:200] or "…")
    db.add(chat)
    db.flush()
    for pos, rid in enumerate(ids):
        db.add(ChatSource(chat_id=chat.id, recording_id=rid, position=pos))
    db.commit()
    ask(db, chat, question)
    return chat


def ask(db: Session, chat: Chat, question: str) -> ChatMessage:
    pending = db.scalar(
        select(ChatMessage).where(
            ChatMessage.chat_id == chat.id, ChatMessage.status.in_(("queued", "running"))
        )
    )
    if pending is not None:
        raise ChatError("chat_busy")
    db.add(ChatMessage(chat_id=chat.id, role="user", content=question))
    reply = ChatMessage(chat_id=chat.id, role="assistant", status="queued")
    db.add(reply)
    chat.updated_at = utcnow()
    db.commit()
    jobs.enqueue(
        db, Queue.LLM, "chat", {"message_id": reply.id}, user_id=chat.user_id, max_attempts=1
    )
    return reply


def build_messages(db: Session, chat: Chat, reply: ChatMessage, s: ServicesSettings) -> list:
    recs = sources(db, chat)
    if not recs:
        raise ChatError("chat_no_meetings")
    system = get_prompts(db)["chat"].format(transcript=material(db, recs), language="", question="")
    turns = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.chat_id == chat.id, ChatMessage.id < reply.id)
            .order_by(ChatMessage.id)
        )
    )
    question, earlier = turns[-1], turns[:-1]
    budget = s.chat_max_chars - len(system) - len(question.content)
    if budget < 0:
        raise ChatError("chat_too_large", used=s.chat_max_chars - budget, limit=s.chat_max_chars)
    # Keep the newest earlier turns that fit; only answered ones, in pairs.
    kept: list[ChatMessage] = []
    for msg in reversed(earlier):
        if msg.role == "assistant" and msg.status != "done":
            continue
        if len(msg.content) > budget:
            break
        budget -= len(msg.content)
        kept.insert(0, msg)
    while kept and kept[0].role != "user":
        kept.pop(0)  # don't start the history with an answer whose question was dropped
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": m.role, "content": m.content} for m in kept]
    msgs.append({"role": "user", "content": question.content})
    return msgs


@handler("chat")
def chat_job(db: Session, job: Job) -> dict:
    reply = db.get(ChatMessage, job.payload["message_id"])
    if reply is None:
        return {"skipped": "message gone"}
    chat = db.get(Chat, reply.chat_id)
    reply.status = "running"
    db.commit()
    s = get_services(db)
    try:
        reply.content = services.chat_messages(s, build_messages(db, chat, reply, s))
    except (services.ServiceError, ChatError) as exc:
        db.rollback()
        reply = db.get(ChatMessage, job.payload["message_id"])
        reply.status, reply.error, reply.finished_at = "failed", exc.stored(), utcnow()
        db.commit()
        log.warning("chat %s: %s", reply.chat_id, exc)
        return {"ok": False, "error": str(exc)}
    reply.status, reply.model, reply.finished_at = "done", s.llm_model, utcnow()
    db.commit()
    return {"ok": True}
