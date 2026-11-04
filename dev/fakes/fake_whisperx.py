"""Fake WhisperX service (sandbox / CI only).

Same interface as onerahmet/openai-whisper-asr-webservice with
ASR_ENGINE=whisperx (see docs/ASR-API.md): POST /asr, multipart field
"audio_file", query parameters, and output=json returning
{"segments", "word_segments", "language"} as text/plain. It reproduces the
real gaps: a segment without a speaker, a word without timings.

Unlike the real service it is strict about its input: it refuses anything
that isn't 16 kHz mono (FAKE_STRICT=0 to relax), which checks that our
worker converted the audio. Text lines contain "I'm <name>" so the
"Speakers" insight has names to find.

    uvicorn fake_whisperx:app --port 9000
"""

import json
import os
import subprocess
import tempfile

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse

app = FastAPI(title="fake whisper-asr-webservice (whisperx)")
STRICT = os.environ.get("FAKE_STRICT", "1") != "0"
LAST: dict = {}

LINES = {
    "en": [
        ("SPEAKER_00", "Good morning everyone, I'm Ayse and I will run today's planning meeting."),
        ("SPEAKER_01", "Thanks Ayse. I'm Mehmet, I have the budget numbers ready."),
        ("SPEAKER_00", "Great. First topic is the new office move in March."),
        ("SPEAKER_01", "We decided to sign the lease on Friday. Mehmet will call the landlord."),
        (None, "[inaudible]"),
        ("SPEAKER_00", "Action item: Ayse sends the moving checklist by Wednesday."),
    ],
    "tr": [
        ("SPEAKER_00", "Herkese günaydın, ben Ayşe, bugünkü planlama toplantısını yöneteceğim."),
        ("SPEAKER_01", "Teşekkürler Ayşe. Ben Mehmet, bütçe rakamları hazır."),
        ("SPEAKER_00", "Harika. İlk konu mart ayındaki ofis taşınması."),
        ("SPEAKER_01", "Kira sözleşmesini cuma günü imzalamaya karar verdik. Mehmet ev sahibini arayacak."),
        (None, "[anlaşılmıyor]"),
        ("SPEAKER_00", "Aksiyon: Ayşe taşınma listesini çarşambaya kadar gönderecek."),
    ],
}


def probe(path: str) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=sample_rate,channels:format=duration", "-of", "json", path],
        capture_output=True, text=True, check=False,
    )
    d = json.loads(out.stdout or "{}")
    if not d.get("streams"):
        raise HTTPException(500, "Failed to load audio: not audio")
    s = d["streams"][0]
    return int(s["sample_rate"]), int(s["channels"]), float(d.get("format", {}).get("duration") or 0)


@app.post("/asr")
async def asr(
    audio_file: UploadFile = File(...),
    encode: bool = Query(True),
    task: str = Query("transcribe"),
    language: str | None = Query(None),
    initial_prompt: str | None = Query(None),
    diarize: bool = Query(False),
    min_speakers: int | None = Query(None),
    max_speakers: int | None = Query(None),
    output: str = Query("txt"),
):
    data = await audio_file.read()
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(audio_file.filename or "")[1]) as fh:
        fh.write(data)
        fh.flush()
        rate, channels, duration = probe(fh.name)
    LAST.clear()
    LAST.update(dict(filename=audio_file.filename, bytes=len(data), rate=rate, channels=channels,
                     duration=duration, language=language, diarize=diarize, task=task,
                     min_speakers=min_speakers, max_speakers=max_speakers, output=output))
    if STRICT and (rate != 16000 or channels != 1):
        raise HTTPException(400, f"fake service expects 16 kHz mono, got {rate} Hz / {channels} ch")

    lang = language or "en"
    lines = LINES.get(lang, LINES["en"])
    step = max(duration / len(lines), 0.5)
    segments = []
    for i, (speaker, text) in enumerate(lines):
        start, end = round(i * step, 2), round(i * step + step * 0.9, 2)
        words, toks = [], text.split()
        for j, tok in enumerate(toks):
            w = {"word": tok}
            if not (i == 2 and j == 1):  # one word the aligner couldn't place
                ws = start + (end - start) * j / len(toks)
                w.update(start=round(ws, 2), end=round(ws + (end - start) / len(toks) * 0.9, 2), score=0.9)
                if diarize and speaker:
                    w["speaker"] = speaker
            words.append(w)
        seg = {"start": start, "end": end, "text": " " + text, "words": words}
        if diarize and speaker:
            seg["speaker"] = speaker
        segments.append(seg)
    result = {"segments": segments, "word_segments": [w for s in segments for w in s["words"]],
              "language": lang}
    if output != "json":
        body = "\n".join((f"[{s['speaker']}]: " if "speaker" in s else "") + s["text"].strip()
                         for s in segments)
        return PlainTextResponse(body, headers={"Asr-Engine": "whisperx"})
    # The real service streams JSON with media type text/plain.
    return PlainTextResponse(json.dumps(result, ensure_ascii=False), headers={"Asr-Engine": "whisperx"})


@app.post("/detect-language")
async def detect_language(audio_file: UploadFile = File(...), encode: bool = Query(True)):
    return {"detected_language": "english", "language_code": "en", "confidence": 0.99}


@app.get("/_last")
def last():
    """Test hook: what the last /asr request looked like."""
    return LAST
