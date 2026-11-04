"""Clients for the two external services (D25): the WhisperX transcription
service and an OpenAI-compatible LLM. Both are just URLs from the admin
panel. See docs/ASR-API.md for the WhisperX service's interface."""

import json
import logging
from pathlib import Path

import httpx

from app import secrets
from app.directory import Step
from app.settings_store import ServicesSettings
from app.tls import ssl_context

log = logging.getLogger("services")


class ServiceError(Exception):
    pass


# --- WhisperX (whisper-asr-webservice, ASR_ENGINE=whisperx) ------------------------


def transcribe(s: ServicesSettings, flac: Path, language: str | None) -> dict:
    """POST the prepared 16 kHz mono FLAC to /asr, return the parsed JSON:
    {"segments": [...], "word_segments": [...], "language": "..."}."""
    if not s.asr_url:
        raise ServiceError("transcription service URL not set (Admin -> Services)")
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
        raise ServiceError(f"transcription service unreachable: {exc!r}") from exc
    if r.status_code != 200:
        raise ServiceError(f"transcription service answered {r.status_code}: {r.text[:300]}")
    try:
        # The real service sends JSON with media type text/plain.
        result = json.loads(r.text)
    except ValueError as exc:
        raise ServiceError("transcription service returned something that isn't JSON") from exc
    if not isinstance(result.get("segments"), list):
        raise ServiceError("transcription service JSON has no 'segments' list")
    return result


def verify_asr(s: ServicesSettings) -> list[Step]:
    steps = [Step("URL", bool(s.asr_url), s.asr_url or "not set")]
    if not s.asr_url:
        return steps
    try:
        r = httpx.get(s.asr_url.rstrip("/") + "/openapi.json", timeout=10, verify=ssl_context())
        r.raise_for_status()
        spec = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        steps.append(Step("Reachable", False, f"{exc.__class__.__name__}: {exc}"))
        return steps
    steps.append(Step("Reachable", True, f"{spec.get('info', {}).get('title', '?')}"))
    asr = spec.get("paths", {}).get("/asr", {}).get("post")
    steps.append(
        Step(
            "POST /asr",
            asr is not None,
            "present" if asr else "missing: not a whisper-asr-webservice?",
        )
    )
    if asr:
        names = {p.get("name") for p in asr.get("parameters", [])}
        steps.append(
            Step(
                "Speaker parameters",
                "diarize" in names,
                "diarize offered"
                if "diarize" in names
                else "no 'diarize': engine isn't whisperx or HF_TOKEN missing on the service",
            )
        )
    return steps


# --- OpenAI-compatible LLM ------------------------------------------------------------


def _llm_headers() -> dict:
    key = secrets.read_secret(secrets.LLM_API_KEY)
    return {"Authorization": f"Bearer {key}"} if key else {}


def chat(s: ServicesSettings, system: str, user: str) -> str:
    if not (s.llm_base_url and s.llm_model):
        raise ServiceError("LLM base URL / model not set (Admin -> Services)")
    body = {
        "model": s.llm_model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": s.llm_temperature,
    }
    try:
        r = httpx.post(
            s.llm_base_url.rstrip("/") + "/chat/completions",
            json=body,
            headers=_llm_headers(),
            timeout=httpx.Timeout(connect=10, read=s.llm_timeout_seconds, write=60, pool=10),
            verify=ssl_context(),
        )
    except httpx.HTTPError as exc:
        raise ServiceError(f"LLM unreachable: {exc!r}") from exc
    if r.status_code != 200:
        raise ServiceError(f"LLM answered {r.status_code}: {r.text[:300]}")
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ServiceError("LLM reply isn't an OpenAI chat completion") from exc


def verify_llm(s: ServicesSettings) -> list[Step]:
    steps = [
        Step("Base URL", bool(s.llm_base_url), s.llm_base_url or "not set"),
        Step("Model", bool(s.llm_model), s.llm_model or "not set"),
        Step(
            "API key",
            True,
            "set (secrets/llm/llm_api_key)"
            if secrets.read_secret(secrets.LLM_API_KEY)
            else "none (fine if your LLM needs no key)",
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
        steps.append(Step("Models offered", True, ", ".join(map(str, ids[:10])) or "none listed"))
        if s.llm_model and ids and s.llm_model not in ids:
            steps.append(Step("Model available", False, f"{s.llm_model!r} not in the list"))
    except (httpx.HTTPError, ValueError) as exc:
        steps.append(Step("Models offered", False, f"{exc.__class__.__name__}: {exc}"))
        return steps
    if s.llm_model:
        try:
            reply = chat(s, "You are a health check.", "Reply with the single word OK.")
            steps.append(Step("Test question", True, reply[:80]))
        except ServiceError as exc:
            steps.append(Step("Test question", False, str(exc)))
    return steps
