"""M3 chain with the fake WhisperX and fake LLM (dev/fakes) served in-process:
upload -> auto-transcribe -> transcript -> insights, plus failure paths."""

import importlib.util
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import services
from app.models import Insight, Job, Recording, Speaker, Transcript
from app.settings_store import ServicesSettings, get_services, save_services
from app.worker import run_once
from tests.conftest import csrf_from, login

FAKES = Path(__file__).resolve().parents[2] / "dev" / "fakes"
FOUR_CH = Path(__file__).resolve().parents[2] / "dev" / "test-audio" / "four-channel-tones.wav"


def load(name):
    spec = importlib.util.spec_from_file_location(name, FAKES / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fakes(monkeypatch, secrets_dir):
    """Route the app's httpx calls to the fake services by host name."""
    fw, fl = load("fake_whisperx"), load("fake_llm")
    fl.KEY = "sk-test"
    (secrets_dir / "llm").mkdir()
    (secrets_dir / "llm" / "llm_api_key").write_text("sk-test")
    clients = {"whisperx": TestClient(fw.app), "llm": TestClient(fl.app)}

    def route(method):
        def call(url, **kw):
            u = httpx.URL(url)
            if u.host not in clients:
                raise httpx.ConnectError(f"no route to {u.host}")
            kw.pop("verify", None)
            kw.pop("timeout", None)
            path = u.raw_path.decode()
            return getattr(clients[u.host], method)(path, **kw)

        return call

    monkeypatch.setattr(services.httpx, "post", route("post"))
    monkeypatch.setattr(services.httpx, "get", route("get"))
    return fw, fl


@pytest.fixture
def configured(db, fakes):
    save_services(
        db,
        ServicesSettings(
            asr_url="http://whisperx:9000",
            llm_base_url="http://llm:8000/v1",
            llm_model="fake-model",
            asr_language="tr",
        ),
    )
    return fakes


@pytest.fixture
def alice(client, regular_user):
    login(client, "alice", "alice-password-1")
    client.headers["X-CSRF-Token"] = csrf_from(client.get("/").text)
    return client


def upload(c, path=FOUR_CH, name="meeting.wav"):
    with open(path, "rb") as fh:
        r = c.post("/api/recordings/upload", files={"file": (name, fh)}, data={"consent": "true"})
    assert r.status_code == 201, r.text
    return r.json()


def drain(db, queue):
    while run_once(db, queue, "test-worker"):
        pass


def test_upload_auto_transcribes_with_prepared_audio(alice, db, configured):
    fw, _ = configured
    rec = upload(alice)
    assert db.query(Job).filter_by(type="transcribe").count() == 1
    drain(db, "gpu")
    # The (strict) fake got 16 kHz mono FLAC, not the 4-channel original.
    assert fw.LAST["rate"] == 16000 and fw.LAST["channels"] == 1
    assert fw.LAST["filename"].endswith(".flac") and fw.LAST["language"] == "tr"
    assert fw.LAST["diarize"] is True

    data = alice.get(f"/api/recordings/{rec['id']}/transcript").json()["transcript"]
    assert data["status"] == "done" and data["language"] == "tr"
    assert len(data["segments"]) == 6
    assert {s["label"] for s in data["speakers"]} == {"SPEAKER_00", "SPEAKER_01"}
    assert any(s["speaker"] is None for s in data["segments"])  # gaps survive


def test_auto_transcribe_off_then_button(alice, db, configured):
    save_services(db, ServicesSettings(asr_url="http://whisperx:9000", auto_transcribe=False))
    rec = upload(alice)
    assert db.query(Job).filter_by(type="transcribe").count() == 0
    r = alice.post(
        f"/api/recordings/{rec['id']}/transcribe", json={"language": "en", "channel": "3"}
    )
    assert r.status_code == 202
    drain(db, "gpu")
    fw, _ = configured
    assert fw.LAST["language"] == "en"
    tr = db.query(Transcript).one()
    assert tr.status == "done" and tr.channel == "3" and tr.language == "en"


def test_bad_channel_is_reported_not_crashed(alice, db, configured):
    rec = upload(alice)
    drain(db, "gpu")
    alice.post(f"/api/recordings/{rec['id']}/transcribe", json={"channel": "9"})
    drain(db, "gpu")
    tr = db.query(Transcript).one()
    assert tr.status == "failed" and "no_such_channel" in tr.error


def test_asr_unreachable_is_reported(alice, db, configured):
    save_services(db, ServicesSettings(asr_url="http://nowhere:9000"))
    upload(alice)
    drain(db, "gpu")
    tr = db.query(Transcript).one()
    assert tr.status == "failed" and "asr_unreachable" in tr.error


def ready(alice, db):
    rec = upload(alice)
    drain(db, "gpu")
    return rec


@pytest.mark.parametrize("kind", ["summary", "action_items", "decisions"])
def test_text_insights(alice, db, configured, kind):
    rec = ready(alice, db)
    r = alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": kind})
    assert r.status_code == 202
    drain(db, "llm")
    [ins] = alice.get(f"/api/recordings/{rec['id']}/insights").json()
    assert ins["status"] == "done" and ins["answer"] and ins["model"] == "fake-model"


def test_subject_renames_recording(alice, db, configured):
    rec = ready(alice, db)
    alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": "subject"})
    drain(db, "llm")
    db.expire_all()
    assert db.get(Recording, rec["id"]).title == "Ofis taşınma planlaması"


def test_speakers_suggestions_and_apply(alice, db, configured):
    rec = ready(alice, db)
    alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": "speakers"})
    drain(db, "llm")
    [ins] = alice.get(f"/api/recordings/{rec['id']}/insights").json()
    names = {s["label"]: s["name"] for s in ins["data"]["suggestions"]}
    assert names == {"SPEAKER_00": "Ayşe", "SPEAKER_01": "Mehmet"}
    assert alice.post(f"/api/insights/{ins['id']}/apply-speakers").json()["applied"] == 2
    db.expire_all()
    assert {s.label: s.name for s in db.query(Speaker)} == names
    # Names are used in later prompts and survive re-transcription.
    alice.post(f"/api/recordings/{rec['id']}/transcribe", json={})
    drain(db, "gpu")
    db.expire_all()
    assert {s.label: s.name for s in db.query(Speaker)} == names


def test_free_question(alice, db, configured):
    rec = ready(alice, db)
    r = alice.post(
        f"/api/recordings/{rec['id']}/insights",
        json={"kind": "question", "question": "When is the lease signed?"},
    )
    assert r.status_code == 202
    drain(db, "llm")
    [ins] = alice.get(f"/api/recordings/{rec['id']}/insights").json()
    assert ins["answer"] == "FAKE ANSWER to: When is the lease signed?"
    assert (
        alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": "question"}).status_code
        == 400
    )


def test_long_transcript_is_done_in_parts(alice, db, configured):
    _, fl = configured
    save_services(
        db,
        ServicesSettings(
            asr_url="http://whisperx:9000",
            llm_base_url="http://llm:8000/v1",
            llm_model="fake-model",
            llm_max_chars=2000,
        ),
    )
    rec = ready(alice, db)
    # Make the transcript long: 40 copies of each segment.
    tr = db.query(Transcript).one()
    from app.models import Segment

    for i in range(240):
        db.add(
            Segment(
                transcript_id=tr.id,
                idx=100 + i,
                start=float(i),
                end=float(i) + 1,
                speaker="SPEAKER_00",
                text="filler sentence about the office move " * 2,
            )
        )
    db.commit()
    fl.CALLS.clear()
    alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": "summary"})
    drain(db, "llm")
    assert len(fl.CALLS) >= 3  # notes per part + the final summary
    assert all(c["chars"] < 4000 for c in fl.CALLS)
    [ins] = alice.get(f"/api/recordings/{rec['id']}/insights").json()
    assert ins["status"] == "done"


def test_llm_key_is_sent_and_wrong_key_fails(alice, db, configured, secrets_dir):
    rec = ready(alice, db)
    (secrets_dir / "llm" / "llm_api_key").write_text("wrong")
    alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": "summary"})
    drain(db, "llm")
    ins = db.query(Insight).one()
    assert ins.status == "failed" and "llm_http" in ins.error and "401" in ins.error


def test_insight_needs_finished_transcript(alice, db, configured):
    save_services(db, ServicesSettings(asr_url="http://whisperx:9000", auto_transcribe=False))
    rec = upload(alice)
    r = alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": "summary"})
    assert r.status_code == 409


def test_others_cannot_touch_transcript_or_insights(alice, client, admin, db, configured):
    rec = ready(alice, db)
    alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": "summary"})
    drain(db, "llm")
    ins_id = db.query(Insight).one().id
    other = client.__class__(client.app)
    login(other, "admin", "admin-password-123")
    other.headers["X-CSRF-Token"] = csrf_from(other.get("/").text)
    assert other.get(f"/api/recordings/{rec['id']}/transcript").status_code == 404
    assert other.get(f"/api/recordings/{rec['id']}/insights").status_code == 404
    assert (
        other.post(f"/api/recordings/{rec['id']}/insights", json={"kind": "summary"}).status_code
        == 404
    )
    assert other.delete(f"/api/insights/{ins_id}").status_code == 404
    assert other.get(f"/recordings/{rec['id']}").status_code == 404


def test_recording_page_renders(alice, db, configured):
    rec = ready(alice, db)
    page = alice.get(f"/recordings/{rec['id']}").text
    assert 'id="recording-app"' in page and 'data-kind="speakers"' in page
    assert "channel 4 only" in page  # channel choices from the file's 4 channels


def test_verify_buttons_against_fakes(db, configured):
    s = ServicesSettings(
        asr_url="http://whisperx:9000", llm_base_url="http://llm:8000/v1", llm_model="fake-model"
    )
    asr = {st.label: st for st in services.verify_asr(s)}
    assert asr["post_asr"].ok and asr["speaker_params"].ok
    llm = {st.label: st for st in services.verify_llm(s)}
    assert llm["models_offered"].ok and "fake-model" in llm["models_offered"].detail
    assert llm["test_question"].ok


def test_mono_opus_browser_recording_also_works(alice, db, configured, tmp_path):
    src = tmp_path / "browser.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=700:duration=3",
            "-ac",
            "2",
            "-c:a",
            "libopus",
            str(src),
        ],
        check=True,
    )
    upload(alice, src, "browser.webm")
    drain(db, "gpu")
    assert db.query(Transcript).one().status == "done"


def test_admin_services_page_save_verify_and_prompts(client, admin, db, configured):
    from app.settings_store import get_prompts, get_services

    login(client, "admin", "admin-password-123")
    assert "Insight prompts" in client.get("/admin/prompts").text
    page = client.get("/admin/services").text
    assert "sk-test" not in page and "fingerprint" in page
    r = client.post(
        "/admin/services",
        data={
            "csrf": csrf_from(page),
            "asr_url": "http://whisperx:9000/",
            "auto_transcribe": "on",
            "asr_language": "TR",
            "asr_diarize": "on",
            "asr_max_speakers": "4",
            "default_channel": "2",
            "llm_base_url": "http://llm:8000/v1",
            "llm_model": "fake-model",
            "llm_max_chars": "5000",
        },
        follow_redirects=False,
    )
    assert r.headers["location"] == "/admin/services?saved=1"
    s = get_services(db)
    assert (s.asr_url, s.asr_language, s.default_channel, s.asr_max_speakers) == (
        "http://whisperx:9000",
        "tr",
        "2",
        4,
    )
    token = csrf_from(client.get("/admin/services").text)
    bad = client.post("/admin/services", data={"csrf": token, "asr_url": "whisperx:9000"})
    assert "must start with http" in bad.text
    assert "OK" in client.post("/admin/services/verify-asr", data={"csrf": token}).text
    llm = client.post("/admin/services/verify-llm", data={"csrf": token}).text
    assert "fake-model" in llm

    custom = "Summarise in {language} in one line.\n{transcript}"
    client.post("/admin/services/prompts", data={"csrf": token, "prompt_summary": custom})
    db.expire_all()
    assert get_prompts(db)["summary"] == custom
    broken = client.post(
        "/admin/services/prompts", data={"csrf": token, "prompt_summary": "Hi {name}"}
    )
    assert "unknown {placeholder}" in broken.text
    client.post("/admin/services/prompts", data={"csrf": token, "reset": "all"})
    db.expire_all()
    from app.insights import DEFAULT_PROMPTS

    assert get_prompts(db) == DEFAULT_PROMPTS


def test_services_page_admin_only(client, regular_user):
    login(client, "alice", "alice-password-1")
    assert client.get("/admin/services").status_code == 403


@pytest.mark.parametrize(
    "kind, expect",
    [
        ("summary", "FAKE SUMMARY"),
        ("decisions", "Sign the office lease"),
        ("action_items", "moving checklist"),
        ("subject", "Ofis"),
    ],
)
def test_each_button_gets_its_own_answer(alice, db, configured, kind, expect):
    rec = ready(alice, db)
    alice.post(f"/api/recordings/{rec['id']}/insights", json={"kind": kind})
    drain(db, "llm")
    [ins] = alice.get(f"/api/recordings/{rec['id']}/insights").json()
    assert expect in ins["answer"]


@pytest.mark.parametrize(
    ("heard", "banned", "sent", "kept"),
    [
        ("az", ["az"], "tr", "tr"),  # banned guess -> the fallback is sent
        ("en", ["az"], "en", "en"),  # allowed guess -> sent as-is
        ("az", [], None, "az"),  # nothing banned -> the service detects on its own
    ],
)
def test_banned_languages(alice, db, configured, heard, banned, sent, kept):
    fw, _ = configured
    fw.DETECT = heard
    s = ServicesSettings(
        asr_url="http://whisperx:9000",
        asr_language="auto",
        asr_banned_languages=banned,
        asr_fallback_language="tr" if banned else "",
    )
    save_services(db, s)
    upload(alice)
    drain(db, "gpu")
    tr = db.query(Transcript).one()
    assert tr.status == "done", tr.error
    assert fw.LAST["language"] == sent and tr.language == kept
    fw.DETECT = "en"


def test_banned_languages_admin_checks(client, admin, db, configured):
    login(client, "admin", "admin-password-123")
    token = csrf_from(client.get("/admin/services").text)
    base = {"csrf": token, "asr_url": "http://whisperx:9000"}
    bad = client.post("/admin/services", data={**base, "asr_banned_languages": "az"})
    assert "instead of the banned" in bad.text
    same = client.post(
        "/admin/services",
        data={**base, "asr_banned_languages": "az", "asr_fallback_language": "az"},
    )
    assert "be one of the banned" in same.text
    ok = client.post(
        "/admin/services",
        data={**base, "asr_banned_languages": "AZ, kk", "asr_fallback_language": "tr"},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    from app.settings_store import get_services

    db.expire_all()
    s = get_services(db)
    assert (s.asr_banned_languages, s.asr_fallback_language) == (["az", "kk"], "tr")


# --- Ask: chats with follow-ups over one or more meetings -----------------------------


def transcribed(alice, db, n=1):
    ids = [upload(alice)["id"] for _ in range(n)]
    drain(db, "gpu")
    return ids


def test_chat_with_follow_up_over_two_meetings(alice, db, configured):
    a, b = transcribed(alice, db, 2)
    src = alice.get("/api/chats/sources").json()
    assert {m["id"] for m in src["meetings"]} == {a, b} and all(
        m["chars"] > 0 for m in src["meetings"]
    )
    r = alice.post("/api/chats", json={"recording_ids": [a, b], "question": "Ne zaman?"})
    assert r.status_code == 201, r.text
    chat = r.json()
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
    # A second question while the first is still waiting is refused.
    busy = alice.post(f"/api/chats/{chat['id']}/messages", json={"question": "Ve?"})
    assert busy.status_code == 409 and busy.json()["error"] == "chat_busy"
    drain(db, "llm")
    first = alice.get(f"/api/chats/{chat['id']}").json()["messages"][-1]
    assert first["status"] == "done"
    assert first["content"] == "FAKE CHAT ANSWER to: Ne zaman? (meetings: 2, earlier turns: 0)"
    alice.post(f"/api/chats/{chat['id']}/messages", json={"question": "Kim arayacak?"})
    drain(db, "llm")
    msgs = alice.get(f"/api/chats/{chat['id']}").json()["messages"]
    assert len(msgs) == 4 and "earlier turns: 2" in msgs[-1]["content"]
    assert [c["id"] for c in alice.get("/api/chats").json()] == [chat["id"]]


def test_chat_respects_the_context_limit(alice, db, configured):
    (a,) = transcribed(alice, db)
    size = alice.get("/api/chats/sources").json()["meetings"][0]["chars"]
    s = get_services(db)
    s.chat_max_chars = size + 5
    save_services(db, s)
    too_big = alice.post(
        "/api/chats", json={"recording_ids": [a], "question": "a question too long"}
    )
    assert too_big.status_code == 413 and too_big.json()["error"] == "chat_too_large"
    assert too_big.json()["limit"] == size + 5
    assert (
        alice.post("/api/chats", json={"recording_ids": [a], "question": "ok"}).status_code == 201
    )


def test_chat_drops_oldest_turns_first(alice, db, configured):
    from app import chats
    from app.models import Chat, ChatMessage

    (a,) = transcribed(alice, db)
    chat_id = alice.post("/api/chats", json={"recording_ids": [a], "question": "q1"}).json()["id"]
    drain(db, "llm")
    for q in ("q2", "q3"):
        alice.post(f"/api/chats/{chat_id}/messages", json={"question": q})
        drain(db, "llm")
    chat = db.get(Chat, chat_id)
    last = db.query(ChatMessage).filter_by(chat_id=chat_id).order_by(ChatMessage.id.desc()).first()
    s = get_services(db)
    full = chats.build_messages(db, chat, last, s)
    assert [m["role"] for m in full] == ["system", "user", "assistant", "user", "assistant", "user"]
    # Room for the meetings, the question and one earlier answer: the earlier
    # *pair* is dropped whole, so history never starts with an answer.
    s.chat_max_chars = len(full[0]["content"]) + len("q3") + len(full[-2]["content"]) + 1
    tight = chats.build_messages(db, chat, last, s)
    assert [m["role"] for m in tight] == ["system", "user"]


def test_chats_are_private_and_need_finished_transcripts(alice, client, db, configured):
    (a,) = transcribed(alice, db)
    chat_id = alice.post("/api/chats", json={"recording_ids": [a], "question": "x"}).json()["id"]
    r = alice.post("/api/chats", json={"recording_ids": ["no-such-id"], "question": "x"})
    assert r.json()["error"] == "chat_not_ready"
    from app.models import User
    from app.security import hash_password

    db.add(User(username="bob", password_hash=hash_password("bob-password-12")))
    db.commit()
    alice.post("/logout", data={"csrf": csrf_from(alice.get("/").text)})
    login(alice, "bob", "bob-password-12")
    alice.headers["X-CSRF-Token"] = csrf_from(alice.get("/").text)
    assert alice.get(f"/api/chats/{chat_id}").status_code == 404
    assert alice.get(f"/ask/{chat_id}").status_code == 404
    assert (
        alice.post("/api/chats", json={"recording_ids": [a], "question": "x"}).json()["error"]
        == "chat_not_ready"
    )
