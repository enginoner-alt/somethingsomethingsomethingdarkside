"""Fake OpenAI-compatible LLM (sandbox / CI only).

GET /v1/models and POST /v1/chat/completions, the subset the app uses.
Answers are predictable and derived from the prompt, so tests can check
the whole round trip:
  - asks for speaker names as JSON -> maps SPEAKER_xx to names found in
    "SPEAKER_xx: ... I'm <Name>" / "ben <Name>" lines of the transcript
  - asks for a title/subject      -> short title
  - summary / action items / decisions / free question -> canned text that
    quotes something from the prompt
If FAKE_LLM_KEY is set, requests must send "Authorization: Bearer <key>".

    uvicorn fake_llm:app --port 8000
"""

import json
import os
import re

from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="fake OpenAI-compatible LLM")
KEY = os.environ.get("FAKE_LLM_KEY", "")
MODEL = os.environ.get("FAKE_LLM_MODEL", "fake-model")
CALLS: list = []


def check_auth(authorization: str | None) -> None:
    if KEY and authorization != f"Bearer {KEY}":
        raise HTTPException(401, {"error": {"message": "invalid api key", "type": "auth"}})


@app.get("/v1/models")
def models(authorization: str | None = Header(None)):
    check_auth(authorization)
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "fake"}]}


def answer(prompt: str) -> str:
    # Decide what was asked from the instructions only: the transcript itself
    # contains words like "Aksiyon" that must not steer the fake.
    instructions = re.split(r"Transcript( part)?:", prompt)[0]
    low = instructions.lower()
    q = re.search(r"Question:\s*(.+)", prompt)
    if q:  # a free question; checked first, the transcript itself contains keywords
        return f"FAKE ANSWER to: {q.group(1).strip()}"
    if "json" in low and "speaker" in low:
        names = {}
        for spk, name in re.findall(r"(SPEAKER_\d+)[^\n]*?\b(?:I'm|[Bb]en)\s+([A-ZÇĞİÖŞÜ][\wçğıöşü]+)", prompt):
            names.setdefault(spk, name)
        return json.dumps({"speakers": [{"label": k, "name": v, "evidence": f"introduces as {v}"}
                                        for k, v in names.items()]}, ensure_ascii=False)
    if "title" in low or "subject" in low or "başlık" in low:
        return "Office move planning" if "office" in low else "Ofis taşınma planlaması"
    if "action item" in low or "aksiyon" in low:
        return "- Ayse: send the moving checklist by Wednesday\n- Mehmet: call the landlord"
    if "decision" in low or "karar" in low:
        return "- Sign the office lease on Friday"
    if "summar" in low or "özet" in low:
        part = " (part summary)" if "part" in low else ""
        return f"FAKE SUMMARY{part}: planning meeting about the office move; lease to be signed Friday."
    return f"FAKE ANSWER to: {prompt[-80:]}"


@app.post("/v1/chat/completions")
async def chat(request: Request, authorization: str | None = Header(None)):
    check_auth(authorization)
    body = await request.json()
    messages = body.get("messages") or []
    prompt = "\n".join(str(m.get("content", "")) for m in messages)
    CALLS.append({"model": body.get("model"), "chars": len(prompt), "temperature": body.get("temperature")})
    system = str(messages[0].get("content", "")) if messages else ""
    if "Meetings:" in system:  # Ask: a chat over meetings, with earlier turns
        meetings = len(re.findall(r"^Meeting \d+:", system, re.M))
        last = str(messages[-1].get("content", "")).strip()
        text = f"FAKE CHAT ANSWER to: {last} (meetings: {meetings}, earlier turns: {len(messages) - 2})"
    else:
        text = answer(prompt)
    return {
        "id": f"fake-{len(CALLS)}",
        "object": "chat.completion",
        "model": body.get("model") or MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(text) // 4,
                  "total_tokens": (len(prompt) + len(text)) // 4},
    }


@app.get("/_calls")
def calls():
    return CALLS
