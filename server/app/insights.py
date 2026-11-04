"""Insights: the default buttons and free questions, answered by the LLM.

Prompt templates are editable in Admin -> Services; these are the defaults.
Placeholders: {language} (name of the transcript's language: answers come
in that language), {transcript}, {question}.
"""

import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services
from app.models import Insight, Recording, Segment, Speaker, Transcript
from app.settings_store import ServicesSettings, get_prompts

KINDS = ("summary", "subject", "speakers", "action_items", "decisions", "question")

SYSTEM = (
    "You analyse meeting transcripts. Each line is '[time] SPEAKER: text'. "
    "Only use what is in the transcript; say so when something isn't there. "
    "Answer in {language}."
)

DEFAULT_PROMPTS = {
    "summary": (
        "Write a concise summary of this meeting in {language}: the purpose, the main "
        "points discussed, and the outcome. Use short paragraphs or bullet points.\n\n"
        "Transcript:\n{transcript}"
    ),
    "subject": (
        "Give this meeting a short title (at most 8 words) in {language} that states its "
        "subject. Reply with the title only, no quotes.\n\nTranscript:\n{transcript}"
    ),
    "speakers": (
        "The speakers are labelled SPEAKER_00, SPEAKER_01, ... Find each speaker's real "
        'name if the transcript reveals it (introductions like "I\'m Ayşe", being addressed '
        "by name, signing off). Reply with JSON only, in this shape:\n"
        '{{"speakers": [{{"label": "SPEAKER_00", "name": "Ayşe", "evidence": "introduces herself"}}]}}\n'  # noqa: E501
        "Leave out speakers whose name isn't clear.\n\nTranscript:\n{transcript}"
    ),
    "action_items": (
        "List the action items agreed in this meeting, in {language}, one per line as "
        "'- who: what (by when)'. If there are none, say so.\n\nTranscript:\n{transcript}"
    ),
    "decisions": (
        "List the decisions made in this meeting, in {language}, one per line. "
        "If there are none, say so.\n\nTranscript:\n{transcript}"
    ),
    "question": (
        "Answer the user's question about this meeting in {language}, using only the "
        "transcript.\n\nQuestion: {question}\n\nTranscript:\n{transcript}"
    ),
}

PART_PROMPT = (
    "This is part {n} of {total} of a long meeting transcript. Write detailed notes in "
    "{language}: who said what, decisions, action items, names mentioned. Keep speaker "
    "labels.\n\nTranscript part:\n{transcript}"
)

LANGUAGE_NAMES = {
    "en": "English",
    "tr": "Turkish",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "nl": "Dutch",
    "ru": "Russian",
    "ar": "Arabic",
}


def ts(seconds: float | None) -> str:
    s = int(seconds or 0)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def transcript_lines(db: Session, rec: Recording) -> tuple[list[str], str]:
    tr = db.scalar(select(Transcript).where(Transcript.recording_id == rec.id))
    if tr is None or tr.status != "done":
        raise services.ServiceError("no_transcript")
    names = {
        sp.label: sp.name
        for sp in db.scalars(select(Speaker).where(Speaker.recording_id == rec.id))
    }
    lines = []
    for seg in db.scalars(
        select(Segment).where(Segment.transcript_id == tr.id).order_by(Segment.idx)
    ):
        who = seg.speaker or "UNKNOWN"
        if names.get(seg.speaker):
            who = f"{seg.speaker} ({names[seg.speaker]})"
        lines.append(f"[{ts(seg.start)}] {who}: {seg.text.strip()}")
    return lines, tr.language or "en"


def split(lines: list[str], max_chars: int) -> list[str]:
    parts, cur, size = [], [], 0
    for line in lines:
        if cur and size + len(line) + 1 > max_chars:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


def parse_speakers(answer: str) -> list[dict]:
    """Pull {"speakers": [...]} out of the reply, even if wrapped in prose or ```."""
    m = re.search(r"\{.*\}", answer, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for item in data.get("speakers", []) if isinstance(data, dict) else []:
        label, name = str(item.get("label", "")).strip(), str(item.get("name", "")).strip()
        if re.fullmatch(r"SPEAKER_\d+", label) and name:
            out.append(
                {
                    "label": label,
                    "name": name[:100],
                    "evidence": str(item.get("evidence", ""))[:200],
                }
            )
    return out


def run(db: Session, ins: Insight, s: ServicesSettings) -> None:
    """Answer one insight. Long transcripts: notes per part first (map), then
    the real question on the combined notes (reduce)."""
    rec = db.get(Recording, ins.recording_id)
    lines, lang = transcript_lines(db, rec)
    language = LANGUAGE_NAMES.get(lang, lang)
    prompts = get_prompts(db)
    system = SYSTEM.format(language=language)
    parts = split(lines, max(s.llm_max_chars, 2000))

    if ins.kind == "speakers" and len(parts) > 1:
        found: dict[str, dict] = {}
        for part in parts:
            reply = services.chat(
                s,
                system,
                prompts["speakers"].format(language=language, transcript=part, question=""),
            )
            for item in parse_speakers(reply):
                found.setdefault(item["label"], item)
        suggestions = list(found.values())
        ins.answer = json.dumps({"speakers": suggestions}, ensure_ascii=False)
    else:
        material = parts[0] if parts else ""
        if len(parts) > 1:
            notes = [
                services.chat(
                    s,
                    system,
                    PART_PROMPT.format(n=i + 1, total=len(parts), language=language, transcript=p),
                )
                for i, p in enumerate(parts)
            ]
            material = "\n\n".join(f"Notes, part {i + 1}:\n{n}" for i, n in enumerate(notes))
        ins.answer = services.chat(
            s,
            system,
            prompts[ins.kind].format(
                language=language, transcript=material, question=ins.question or ""
            ),
        )
        suggestions = parse_speakers(ins.answer) if ins.kind == "speakers" else None

    if ins.kind == "speakers":
        ins.data = {"suggestions": suggestions}
    if ins.kind == "subject" and ins.answer:
        title = ins.answer.strip().strip('"').strip("'").splitlines()[0][:200]
        if title:
            rec.title = title
    ins.model = s.llm_model
