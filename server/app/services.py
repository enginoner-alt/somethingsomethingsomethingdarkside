"""Clients for the two external services (D25): the WhisperX transcription
service and an OpenAI-compatible LLM. Both are just URLs from the admin
panel. See docs/ASR-API.md for the WhisperX service's interface."""

import json
import logging
from pathlib import Path

import httpx

from app import secrets
from app.directory import Step
from app.errors import CodedError
from app.settings_store import ServicesSettings
from app.tls import ssl_context

log = logging.getLogger("services")


class ServiceError(CodedError):
    pass


# --- WhisperX (whisper-asr-webservice, ASR_ENGINE=whisperx) ------------------------


def transcribe(s: ServicesSettings, flac: Path, language: str | None) -> dict:
    """POST the prepared 16 kHz mono FLAC to /asr, return the parsed JSON:
    {"segments": [...], "word_segments": [...], "language": "..."}."""
    if not s.asr_url:
        raise ServiceError("asr_not_set")
    params: dict = {"output": "json", "task": "transcribe", "encode": "true"}
    if language and language != "auto":
        params["language"] = language
    if s.asr_diarize:
        params["diarize"] = "true"
        if s.asr_min_speakers:
            params["min_speakers"] = s.asr_min_speakers
        if s.asr_max_speakers:
            params["max_speakers"] = s.asr_max_speakers
    if s.asr_initial_prompt:
        params["initial_prompt"] = s.asr_initial_prompt
    timeout = httpx.Timeout(connect=10, read=s.asr_timeout_seconds, write=600, pool=10)
    try:
        with flac.open("rb") as fh:
            r = httpx.post(
                s.asr_url.rstrip("/") + "/asr",
                params=params,
                files={"audio_file": (flac.name, fh, "audio/flac")},
                timeout=timeout,
                verify=ssl_context(),
            )
    except httpx.HTTPError as exc:
        raise ServiceError("asr_unreachable", repr(exc)) from exc
    if r.status_code != 200:
        raise ServiceError("asr_http", r.text[:300], status=r.status_code)
    try:
        # The real service sends JSON with media type text/plain.
        result = json.loads(r.text)
    except ValueError as exc:
        raise ServiceError("asr_not_json") from exc
    if not isinstance(result.get("segments"), list):
        raise ServiceError("asr_no_segments")
    return result


def detect_language(s: ServicesSettings, flac: Path) -> str:
    """POST /detect-language: the service's guess from the first 30 s, e.g. "tr"."""
    try:
        with flac.open("rb") as fh:
            r = httpx.post(
                s.asr_url.rstrip("/") + "/detect-language",
                params={"encode": "true"},
                files={"audio_file": (flac.name, fh, "audio/flac")},
                timeout=httpx.Timeout(connect=10, read=600, write=600, pool=10),
                verify=ssl_context(),
            )
        r.raise_for_status()
        return str(r.json()["language_code"]).lower()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise ServiceError("asr_detect", repr(exc)) from exc


def choose_language(s: ServicesSettings, flac: Path, requested: str | None) -> str | None:
    """The language to send. Asked for one: that one. Detecting automatically with
    banned languages set: detect first, and use the fallback instead of a banned guess."""
    if requested and requested != "auto":
        return requested
    if not s.asr_banned_languages:
        return None  # let the service detect
    try:
        guess = detect_language(s, flac)
    except ServiceError as exc:
        log.warning("language detection failed, transcribing without it: %s", exc)
        return None
    if guess in s.asr_banned_languages:
        log.info("detected %r is banned; using %r", guess, s.asr_fallback_language)
        return s.asr_fallback_language or None
    return guess


def verify_asr(s: ServicesSettings) -> list[Step]:
    steps = [Step("url", bool(s.asr_url), s.asr_url, note="" if s.asr_url else "not_set")]
    if not s.asr_url:
        return steps
    try:
        r = httpx.get(s.asr_url.rstrip("/") + "/openapi.json", timeout=10, verify=ssl_context())
        r.raise_for_status()
        spec = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        steps.append(Step("reachable", False, f"{exc.__class__.__name__}: {exc}"))
        return steps
    steps.append(Step("reachable", True, f"{spec.get('info', {}).get('title', '?')}"))
    asr = spec.get("paths", {}).get("/asr", {}).get("post")
    steps.append(Step("post_asr", asr is not None, note="present" if asr else "asr_missing"))
    if asr:
        names = {p.get("name") for p in asr.get("parameters", [])}
        steps.append(
            Step(
                "speaker_params",
                "diarize" in names,
                note="diarize_offered" if "diarize" in names else "no_diarize",
            )
        )
    return steps


# --- OpenAI-compatible LLM ------------------------------------------------------------


def _llm_headers() -> dict:
    key = secrets.read_secret(secrets.LLM_API_KEY)
    return {"Authorization": f"Bearer {key}"} if key else {}


def chat(s: ServicesSettings, system: str, user: str) -> str:
    return chat_messages(
        s, [{"role": "system", "content": system}, {"role": "user", "content": user}]
    )


def chat_messages(s: ServicesSettings, messages: list[dict]) -> str:
    """One OpenAI chat completion; `messages` as the API takes them."""
    if not (s.llm_base_url and s.llm_model):
        raise ServiceError("llm_not_set")
    body = {"model": s.llm_model, "messages": messages, "temperature": s.llm_temperature}
    try:
        r = httpx.post(
            s.llm_base_url.rstrip("/") + "/chat/completions",
            json=body,
            headers=_llm_headers(),
            timeout=httpx.Timeout(connect=10, read=s.llm_timeout_seconds, write=60, pool=10),
            verify=ssl_context(),
        )
    except httpx.HTTPError as exc:
        raise ServiceError("llm_unreachable", repr(exc)) from exc
    if r.status_code != 200:
        raise ServiceError("llm_http", r.text[:300], status=r.status_code)
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ServiceError("llm_bad_reply") from exc


def verify_llm(s: ServicesSettings) -> list[Step]:
    steps = [
        Step(
            "base_url",
            bool(s.llm_base_url),
            s.llm_base_url,
            note="" if s.llm_base_url else "not_set",
        ),
        Step("model", bool(s.llm_model), s.llm_model, note="" if s.llm_model else "not_set"),
        Step(
            "api_key",
            True,
            note="key_set" if secrets.read_secret(secrets.LLM_API_KEY) else "key_none",
        ),
    ]
    if not s.llm_base_url:
        return steps
    try:
        r = httpx.get(
            s.llm_base_url.rstrip("/") + "/models",
            headers=_llm_headers(),
            timeout=10,
            verify=ssl_context(),
        )
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", [])]
        steps.append(
            Step("models_offered", True, ", ".join(map(str, ids[:10])), note="" if ids else "none")
        )
        if s.llm_model and ids and s.llm_model not in ids:
            steps.append(Step("model_available", False, s.llm_model, note="not_listed"))
    except (httpx.HTTPError, ValueError) as exc:
        steps.append(Step("models_offered", False, f"{exc.__class__.__name__}: {exc}"))
        return steps
    if s.llm_model:
        try:
            reply = chat(s, "You are a health check.", "Reply with the single word OK.")
            steps.append(Step("test_question", True, reply[:80]))
        except ServiceError as exc:
            steps.append(Step("test_question", False, str(exc)))
    return steps
