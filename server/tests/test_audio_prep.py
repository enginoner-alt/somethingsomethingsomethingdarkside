"""Any input -> 16 kHz mono FLAC; 4-channel mix and single-channel pick."""

import json
import subprocess

import numpy as np
import pytest

from app import audio_prep

TONES = [400.0, 700.0, 1100.0, 1600.0]  # one per channel


@pytest.fixture(scope="session")
def four_channel(tmp_path_factory):
    """4-channel 48 kHz WAV: channel i plays TONES[i]."""
    import wave

    rate, secs = 48000, 2
    t = np.arange(rate * secs) / rate
    data = np.stack([0.4 * np.sin(2 * np.pi * f * t) for f in TONES], axis=1)
    path = tmp_path_factory.mktemp("prep") / "four.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(4)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((data * 32767).astype("<i2").tobytes())
    return path


def info(path):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,channels,sample_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    d = json.loads(out)
    s = d["streams"][0]
    return (
        s["codec_name"],
        int(s["channels"]),
        int(s["sample_rate"]),
        float(d["format"]["duration"]),
    )


def peaks(path):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-"],
        capture_output=True,
        check=True,
    ).stdout
    x = np.frombuffer(raw, dtype="<f4")
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    freqs = np.fft.rfftfreq(len(x), 1 / 16000)
    strong = spec > spec.max() * 0.2
    return {f for f in TONES if strong[np.argmin(abs(freqs - f))]}


def test_four_channels_mixed_to_mono_16k_flac(four_channel, tmp_path):
    out = audio_prep.prepare(four_channel, tmp_path / "out.flac")
    codec, ch, rate, dur = info(out)
    assert (codec, ch, rate) == ("flac", 1, 16000)
    assert dur == pytest.approx(2.0, abs=0.05)
    assert peaks(out) == set(TONES)  # every channel made it into the mix


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_pick_one_channel(four_channel, tmp_path, n):
    out = audio_prep.prepare(four_channel, tmp_path / "out.flac", channel=str(n))
    assert info(out)[:3] == ("flac", 1, 16000)
    assert peaks(out) == {TONES[n - 1]}


def test_bad_channel_choices(four_channel, tmp_path):
    with pytest.raises(audio_prep.PrepError, match="has 4"):
        audio_prep.prepare(four_channel, tmp_path / "x.flac", channel="5")
    with pytest.raises(audio_prep.PrepError, match="mix"):
        audio_prep.prepare(four_channel, tmp_path / "x.flac", channel="left")


@pytest.mark.parametrize(
    "args",
    [
        ["-ac", "1", "-c:a", "libopus"],  # browser-like mono opus
        ["-ac", "2", "-c:a", "libopus"],  # stereo opus (Chrome)
        ["-ac", "2", "-c:a", "aac"],  # Safari-like m4a
        ["-ac", "2", "-c:a", "libmp3lame"],  # mp3 upload
    ],
)
def test_common_inputs(tmp_path, args):
    ext = {"libopus": "webm", "aac": "m4a", "libmp3lame": "mp3"}[args[-1]]
    src = tmp_path / f"in.{ext}"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=700:duration=2",
            *args,
            str(src),
        ],
        check=True,
    )
    out = audio_prep.prepare(src, tmp_path / "out.flac")
    assert info(out)[:3] == ("flac", 1, 16000)


def test_not_audio(tmp_path):
    bad = tmp_path / "x.webm"
    bad.write_bytes(b"nope" * 100)
    with pytest.raises(audio_prep.PrepError):
        audio_prep.prepare(bad, tmp_path / "x.flac")
