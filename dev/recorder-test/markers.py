"""Tone markers: make a fake-mic clip, and check a recording against it.

    python markers.py make out.wav [--seconds 8]
    python markers.py check recording.webm [--pauses 1]

The clip: second k plays a tone of BASE + k*STEP Hz on the LEFT channel, and a
steady 300 Hz on the RIGHT channel. Chromium's fake microphone loops it.

The check decodes the recording, finds the dominant left-channel frequency in
50 ms frames, and collapses them into runs. A recording that is "the same
audio, more or less" has runs of about 1 s each, stepping k -> k+1 (mod N).
A lost, duplicated or reordered piece shows up as a short/long run or a jump.
Pauses (MediaRecorder.pause) are allowed jumps; pass how many to expect.
Prints JSON; exit code 1 if the check fails.
"""

import argparse
import json
import subprocess
import sys
import wave

import numpy as np

RATE = 48000
BASE, STEP, RIGHT_HZ = 600.0, 150.0, 300.0
FRAME = 0.05


def make(path: str, seconds: int) -> None:
    t = np.arange(int(RATE * seconds)) / RATE
    k = np.floor(t).astype(int)
    left = 0.5 * np.sin(2 * np.pi * (BASE + STEP * k) * t)
    right = 0.5 * np.sin(2 * np.pi * RIGHT_HZ * t)
    pcm = (np.stack([left, right], axis=1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())


def decode(path: str) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, dtype="<f4").reshape(-1, 2)


def dominant(x: np.ndarray) -> tuple[float, float]:
    n = len(x)
    spec = np.abs(np.fft.rfft(x * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    i = int(np.argmax(spec[1:]) + 1)
    return float(freqs[i]), float(spec[i] / (spec.sum() + 1e-9))


def check(path: str, seconds: int, pauses: int) -> dict:
    audio = decode(path)
    duration = len(audio) / RATE
    hop = int(RATE * FRAME)
    marks = []
    for start in range(0, len(audio) - hop + 1, hop):
        f, purity = dominant(audio[start : start + hop, 0])
        k = round((f - BASE) / STEP)
        marks.append(k if 0 <= k < seconds and abs(f - (BASE + k * STEP)) < STEP / 3 else None)

    runs = []  # [marker, length_s]
    for m in marks:
        if m is None:
            continue
        if runs and runs[-1][0] == m:
            runs[-1][1] += FRAME
        else:
            runs.append([m, FRAME])
    # Merge flicker at boundaries (runs shorter than 0.15 s).
    runs = [r for r in runs if r[1] >= 0.15]
    merged = []
    for m, length in runs:
        if merged and merged[-1][0] == m:
            merged[-1][1] += length
        else:
            merged.append([m, length])

    jumps, edges = [], set()
    for i, ((a, _), (b, _)) in enumerate(zip(merged, merged[1:], strict=False)):
        if b != (a + 1) % seconds:
            jumps.append({"from": a, "to": b})
            edges.update({i, i + 1})  # the tones either side of a pause are cut short
    # Partial tones are only allowed at the very start/end and around an
    # allowed pause; everywhere else each tone must last about one second.
    allowed_edges = edges if len(jumps) <= pauses else set()
    bad_runs = [
        r for i, r in enumerate(merged)
        if 0 < i < len(merged) - 1 and i not in allowed_edges and not 0.8 <= r[1] <= 1.25
    ]

    right = audio[:, 1]
    right_hz = dominant(right[: min(len(right), RATE * 2)])[0] if len(right) else None
    channels_distinct = right_hz is not None and abs(right_hz - RIGHT_HZ) < 30

    ok = len(jumps) <= pauses and not bad_runs and len(merged) >= 2
    return {
        "ok": ok,
        "duration_s": round(duration, 2),
        "markers": [m for m, _ in merged],
        "run_lengths_s": [round(length, 2) for _, length in merged],
        "jumps": jumps,
        "expected_max_jumps": pauses,
        "bad_runs": bad_runs,
        "stereo_preserved": channels_distinct,
        "right_channel_hz": round(right_hz, 1) if right_hz else None,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("path")
    m.add_argument("--seconds", type=int, default=8)
    c = sub.add_parser("check")
    c.add_argument("path")
    c.add_argument("--seconds", type=int, default=8)
    c.add_argument("--pauses", type=int, default=0)
    args = p.parse_args()
    if args.cmd == "make":
        make(args.path, args.seconds)
        print(json.dumps({"made": args.path, "seconds": args.seconds}))
    else:
        result = check(args.path, args.seconds, args.pauses)
        print(json.dumps(result))
        sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
