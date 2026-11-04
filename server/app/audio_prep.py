"""Turn any recording into what the transcription service gets:
16 kHz, mono, FLAC (lossless: no second quality loss on top of the
browser's compression; ~100 MB for 2 h).

Channels (D22): "mix" averages every channel with equal weight (an explicit
pan filter, so 3-, 4- or 6-channel files mix the same way regardless of
what layout ffmpeg guesses), or "1".."N" keeps one channel, e.g. the
processed beam of a conference mic like the XVF3800.
"""

import json
import subprocess
from pathlib import Path

from app.errors import CodedError

TARGET_RATE = 16000


class PrepError(CodedError):
    pass


def channel_count(src: Path) -> int:
    res = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels",
            "-of",
            "json",
            str(src),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    streams = json.loads(res.stdout or "{}").get("streams") or []
    if res.returncode != 0 or not streams or not streams[0].get("channels"):
        raise PrepError("no_audio", src.name)
    return int(streams[0]["channels"])


def pan_filter(channels: int, choice: str) -> str:
    if choice in ("", "mix"):
        if channels == 1:
            return "pan=mono|c0=c0"
        weight = 1.0 / channels
        return "pan=mono|c0=" + "+".join(f"{weight:.6f}*c{i}" for i in range(channels))
    try:
        n = int(choice)
    except ValueError as exc:
        raise PrepError("bad_channel", repr(choice)) from exc
    if not 1 <= n <= channels:
        raise PrepError("no_such_channel", n=n, channels=channels)
    return f"pan=mono|c0=c{n - 1}"


def prepare(src: Path, dst: Path, channel: str = "mix") -> Path:
    """Write `dst` (16 kHz mono FLAC) from `src`. Returns dst."""
    channels = channel_count(src)
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-af",
        pan_filter(channels, channel),
        "-ar",
        str(TARGET_RATE),
        "-ac",
        "1",
        "-sample_fmt",
        "s16",
        "-c:a",
        "flac",
        str(dst),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)
    if res.returncode != 0 or not dst.is_file():
        raise PrepError("ffmpeg", (res.stderr or "").strip()[:500])
    return dst
