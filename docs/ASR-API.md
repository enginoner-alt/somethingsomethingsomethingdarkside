# WhisperX service API (what the worker talks to)

Researched from the **source code** of `ahmetoner/whisper-asr-webservice`
(v1.10 / main; Docker image `onerahmet/openai-whisper-asr-webservice`) with
`ASR_ENGINE=whisperx`, and of `m-bain/whisperX` v3.4.5. Not yet run against
a real server; `dev/fakes/fake_whisperx.py` mimics this interface for tests.

## Request

`POST /asr`, multipart, file field **`audio_file`**, options in the **query string**:

| Parameter | Default | Notes |
|---|---|---|
| `output` | `txt` | `txt`, `vtt`, `srt`, `tsv`, **`json`** (we use json) |
| `language` | auto (first 30 s) | `en`, `tr`, … |
| `task` | `transcribe` | or `translate` (to English) |
| `diarize` | `false` | only works with `HF_TOKEN` set on the service, otherwise silently ignored |
| `min_speakers`, `max_speakers` | none | hints for diarization |
| `initial_prompt` | none | vocabulary hint |
| `encode` | `true` | ffmpeg → 16 kHz mono first |

```bash
curl -X POST "http://whisperx:9000/asr?output=json&language=tr&diarize=true" -F "audio_file=@meeting.flac"
```

Synchronous (blocks until done), one request at a time, whole upload read
into memory. No authentication: our network isolation protects it (D26).
Also `POST /detect-language` and Swagger at `/docs`, spec at `/openapi.json`.

## Response (`output=json`, media type `text/plain`)

```json
{"language": "tr",
 "segments": [{"start": 0.52, "end": 4.1, "text": "…", "speaker": "SPEAKER_00",
               "words": [{"word": "…", "start": 0.52, "end": 1.3, "score": 0.91, "speaker": "SPEAKER_00"}]}],
 "word_segments": ["…all words…"]}
```

- `speaker` is **missing** on segments/words no diarized speaker overlaps.
- `start` / `end` / `score` are **missing** on words the aligner couldn't place; `words` may be `[]`.
- `txt`/`srt`/`vtt` prefix `[SPEAKER_00]: `; `tsv` has no speakers.
- Alignment models exist for `en` and `tr` (`mpoyraz/wav2vec2-xls-r-300m-cv7-turkish`) among others;
  a language without one makes the request **fail**.
- Diarization model: `pyannote/speaker-diarization-3.1`. No speaker embeddings in the output.

## Service settings (env)

`ASR_ENGINE=whisperx`, `ASR_MODEL` (default `base`; try `large-v3`), `ASR_QUANTIZATION`
(default `float32` on GPU: use `float16`/`int8` on 8 GB), `HF_TOKEN` (speakers),
`MODEL_IDLE_TIMEOUT` (unload when idle; for GPU sharing), model cache at `/root/.cache`.

## What our worker does

16 kHz mono FLAC of the chosen channel (`app/audio_prep.py`) → `POST /asr` with
`output=json`, the admin's language/speaker settings → parses the body as JSON
(it says text/plain) → stores segments, words, speakers; flags "speakers requested
but none returned".

Unconfirmed until run for real: exact pyannote version, whether VAD also needs the
token, the image's version tags.
