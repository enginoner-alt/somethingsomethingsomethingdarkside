"""Recording API with real audio (ffmpeg makes it, ffprobe checks it)."""

import subprocess
from datetime import timedelta

import pytest

from app import recordings
from app.db import utcnow
from app.models import Recording, RecordingChunk
from tests.conftest import csrf_from, login


@pytest.fixture(scope="session")
def webm(tmp_path_factory) -> bytes:
    """3 s stereo Opus/WebM, like Chrome's MediaRecorder output."""
    out = tmp_path_factory.mktemp("media") / "clip.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=3",
            "-filter_complex",
            "amerge=inputs=2",
            "-c:a",
            "libopus",
            "-b:a",
            "48k",
            str(out),
        ],
        check=True,
    )
    return out.read_bytes()


def media(tmp_path_factory, ext, *codec):
    out = tmp_path_factory.mktemp("media") / f"clip.{ext}"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=500:duration=2",
            *codec,
            str(out),
        ],
        check=True,
    )
    return out


def split(data: bytes, n: int) -> list[bytes]:
    size = -(-len(data) // n)
    return [data[i : i + size] for i in range(0, len(data), size)]


@pytest.fixture
def alice(client, regular_user):
    login(client, "alice", "alice-password-1")
    client.headers["X-CSRF-Token"] = csrf_from(client.get("/").text)
    return client


def create(c, **kw):
    body = {"mime_type": "audio/webm;codecs=opus", "consent": True, "title": "t", **kw}
    return c.post("/api/recordings", json=body)


def upload_all(c, rec_id, parts, order=None):
    for i in order or range(len(parts)):
        r = c.put(f"/api/recordings/{rec_id}/chunks/{i}", content=parts[i])
        assert r.status_code == 200, r.text


def test_full_round_trip_out_of_order_with_duplicates(alice, webm, audio_dir):
    rec = create(alice).json()
    parts = split(webm, 5)
    upload_all(alice, rec["id"], parts, order=[0, 2, 1, 4, 3])
    dup = alice.put(f"/api/recordings/{rec['id']}/chunks/2", content=parts[2])
    assert dup.json()["result"] == "duplicate"

    done = alice.post(f"/api/recordings/{rec['id']}/finalize", json={"last_seq": 4}).json()
    assert done["status"] == "uploaded" and done["channels"] == 2
    assert done["duration_seconds"] == pytest.approx(3.0, abs=0.15)
    # Reassembled file is byte-identical to the original, chunk files are gone.
    path = next(audio_dir.rglob("original.webm"))
    assert path.read_bytes() == webm
    assert not list(audio_dir.rglob("*.part"))

    audio = alice.get(done["audio_url"])
    assert audio.status_code == 200 and audio.content == webm
    ranged = alice.get(done["audio_url"], headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100
    again = alice.post(f"/api/recordings/{rec['id']}/finalize", json={"last_seq": 4})
    assert again.json()["status"] == "uploaded"  # idempotent


def test_finalize_reports_missing_chunks(alice, webm):
    rec = create(alice).json()
    parts = split(webm, 4)
    upload_all(alice, rec["id"], parts, order=[0, 3])
    r = alice.post(f"/api/recordings/{rec['id']}/finalize", json={"last_seq": 3})
    assert r.status_code == 409 and r.json()["missing"] == [1, 2]
    upload_all(alice, rec["id"], parts, order=[1, 2])
    assert (
        alice.post(f"/api/recordings/{rec['id']}/finalize", json={"last_seq": 3}).status_code == 200
    )


def test_conflicting_chunk_refused(alice, webm):
    rec = create(alice).json()
    alice.put(f"/api/recordings/{rec['id']}/chunks/0", content=b"abc")
    r = alice.put(f"/api/recordings/{rec['id']}/chunks/0", content=b"xyz")
    assert r.status_code == 409 and r.json()["error"] == "conflict"


def test_chunks_refused_after_finalize(alice, webm):
    rec = create(alice).json()
    upload_all(alice, rec["id"], [webm])
    alice.post(f"/api/recordings/{rec['id']}/finalize", json={"last_seq": 0})
    r = alice.put(f"/api/recordings/{rec['id']}/chunks/1", content=b"late")
    assert r.status_code == 409 and r.json()["error"] == "closed"


def test_create_validation(alice):
    assert create(alice, consent=False).status_code == 400
    assert create(alice, mime_type="video/x-msvideo").status_code == 415
    for mime, ext in [("audio/ogg;codecs=opus", "ogg"), ("audio/mp4", "mp4")]:
        assert create(alice, mime_type=mime).status_code == 201, ext


def test_csrf_header_and_login_required(client, alice):
    del alice.headers["X-CSRF-Token"]
    assert create(alice).status_code == 403
    anon = client.__class__(client.app)
    r = anon.get("/api/recordings")
    assert r.status_code == 401 and r.json()["error"] == "login_required"


def test_other_users_cannot_touch_my_recording(alice, client, admin, webm):
    rec = create(alice).json()
    upload_all(alice, rec["id"], [webm])
    done = alice.post(f"/api/recordings/{rec['id']}/finalize", json={"last_seq": 0}).json()

    other = client.__class__(client.app)
    login(other, "admin", "admin-password-123")  # even an admin
    other.headers["X-CSRF-Token"] = csrf_from(other.get("/").text)
    assert other.get(done["audio_url"]).status_code == 404
    assert other.put(f"/api/recordings/{rec['id']}/chunks/1", content=b"x").status_code == 404
    assert other.delete(f"/api/recordings/{rec['id']}").status_code == 404
    assert other.get("/api/recordings").json() == []


def test_garbage_audio_marked_failed(alice):
    rec = create(alice).json()
    upload_all(alice, rec["id"], [b"definitely not audio" * 100])
    done = alice.post(f"/api/recordings/{rec['id']}/finalize", json={"last_seq": 0}).json()
    assert done["status"] == "failed" and done["error"] and done["audio_url"] is None


def test_stale_recording_becomes_interrupted_keeping_contiguous_prefix(alice, db, webm):
    rec = create(alice).json()
    parts = split(webm, 4)
    upload_all(alice, rec["id"], parts, order=[0, 1, 3])  # 2 never arrived
    row = db.get(Recording, rec["id"])
    row.last_chunk_at = utcnow() - timedelta(hours=1)
    db.commit()
    [listed] = alice.get("/api/recordings").json()  # listing closes stale ones
    assert listed["status"] in ("interrupted", "failed")
    db.expire_all()
    assert db.query(RecordingChunk).filter_by(recording_id=rec["id"]).count() == 0
    assert db.get(Recording, rec["id"]).size_bytes == len(parts[0]) + len(parts[1])


def test_chunk_and_quota_limits(alice, cfg, monkeypatch, webm):
    monkeypatch.setattr(cfg, "max_chunk_bytes", 100)
    rec = create(alice).json()
    r = alice.put(f"/api/recordings/{rec['id']}/chunks/0", content=b"x" * 101)
    assert r.status_code == 413
    monkeypatch.setattr(cfg, "max_chunk_bytes", 10_000_000)
    monkeypatch.setattr(cfg, "user_quota_mb", 1)
    r = alice.put(f"/api/recordings/{rec['id']}/chunks/0", content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413 and r.json()["error"] == "quota"


def test_max_recording_length(alice, db, cfg, monkeypatch):
    rec = create(alice).json()
    row = db.get(Recording, rec["id"])
    row.created_at = utcnow() - timedelta(seconds=cfg.max_recording_seconds + 300)
    db.commit()
    r = alice.put(f"/api/recordings/{rec['id']}/chunks/0", content=b"x")
    assert r.status_code == 409 and r.json()["error"] == "too_long"


@pytest.mark.parametrize(
    "ext, codec",
    [
        ("mp3", ["-c:a", "libmp3lame"]),
        ("wav", []),
        ("m4a", ["-c:a", "aac"]),
        ("ogg", ["-c:a", "libopus"]),
    ],
)
def test_upload_existing_files(alice, tmp_path_factory, ext, codec):
    path = media(tmp_path_factory, ext, *codec)
    with path.open("rb") as fh:
        r = alice.post(
            "/api/recordings/upload",
            files={"file": (f"meeting.{ext}", fh)},
            data={"consent": "true"},
        )
    assert r.status_code == 201, r.text
    assert r.json()["duration_seconds"] == pytest.approx(2.0, abs=0.2)
    assert r.json()["title"] == "meeting"


def test_upload_rejects_bad_files(alice):
    bad = alice.post(
        "/api/recordings/upload", files={"file": ("x.exe", b"MZ")}, data={"consent": "true"}
    )
    assert bad.status_code == 415
    junk = alice.post(
        "/api/recordings/upload", files={"file": ("x.mp3", b"junk" * 50)}, data={"consent": "true"}
    )
    assert junk.status_code == 422
    noconsent = alice.post("/api/recordings/upload", files={"file": ("x.mp3", b"junk")})
    assert noconsent.status_code == 400


def test_delete_then_purge(alice, db, cfg, audio_dir, webm):
    rec = create(alice).json()
    upload_all(alice, rec["id"], [webm])
    done = alice.post(f"/api/recordings/{rec['id']}/finalize", json={"last_seq": 0}).json()
    assert alice.delete(f"/api/recordings/{rec['id']}").status_code == 200
    assert alice.get("/api/recordings").json() == []
    assert alice.get(done["audio_url"]).status_code == 404
    assert list(audio_dir.rglob("original.webm"))  # still in the trash

    row = db.get(Recording, rec["id"])
    row.deleted_at = utcnow() - timedelta(days=cfg.trash_retention_days + 1)
    db.commit()
    assert recordings.purge_expired(db) == 1
    assert not list(audio_dir.rglob("original.webm"))
    db.expire_all()
    assert db.get(Recording, rec["id"]) is None


def test_home_lists_and_record_page_records(alice):
    home = alice.get("/").text
    assert 'id="list-app"' in home and "list.js" in home and 'id="upload-form"' in home
    page = alice.get("/record").text
    assert 'id="recorder-app"' in page and "recorder.js" in page and "data-chunk-seconds" in page


def test_pages_render_in_turkish(alice):
    alice.cookies.set("lang", "tr")
    for url in ("/", "/record"):
        assert "Kayıtlar" in alice.get(url).text


def test_login_logo_slot(client, tmp_path, monkeypatch):
    from app.config import get_config

    monkeypatch.setattr(get_config(), "branding_dir", str(tmp_path))
    assert client.get("/branding/logo").status_code == 404
    page = client.get("/login").text
    assert 'class="logo-slot"' in page and "/branding/logo" not in page
    (tmp_path / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    assert client.get("/branding/logo").status_code == 200
    assert 'src="/branding/logo"' in client.get("/login").text


def test_list_pages_and_search(alice):
    page = alice.get("/").text
    import re

    csrf = re.search(r'data-csrf="([^"]+)"', page).group(1)
    for i in range(30):
        r = alice.post(
            "/api/recordings",
            json={"mime_type": "audio/webm", "consent": True, "title": f"Meeting {i:02d}"},
            headers={"X-CSRF-Token": csrf},
        )
        assert r.status_code == 201
    first = alice.get("/api/recordings?limit=25")
    assert len(first.json()) == 25 and first.headers["x-total-count"] == "30"
    assert len(alice.get("/api/recordings?limit=25&offset=25").json()) == 5
    hit = alice.get("/api/recordings?limit=25&q=meeting 07")
    assert [r["title"] for r in hit.json()] == ["Meeting 07"]
    assert len(alice.get("/api/recordings").json()) == 30  # no limit: everything, as before
