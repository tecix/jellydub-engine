"""Live dub server: fake translator + fake TTS, real HTTP on an ephemeral port, real ffmpeg."""
import json
import math
import shutil
import struct
import threading
import time
import urllib.error
import urllib.request
import wave

import pytest

from jellydub.config import Config
from jellydub.live.server import DubServer
from jellydub.live.session import Engine, MAX_BACKLOG, MAX_TTS_FAILURES, Session
from jellydub.models import Cue
from jellydub.tts.base import TTSProvider

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


class FakeTranslator:
    def __init__(self):
        self.calls = []

    def cache_id(self):
        return "fake-live"

    def translate_batch(self, texts):
        self.calls.append(list(texts))
        return ["vi " + t for t in texts]

    def close(self):
        pass


class FakeTTS(TTSProvider):
    name = "fake"
    max_concurrency = 2

    def __init__(self, seconds=0.5, delay=0.0):
        self.seconds, self.delay = seconds, delay
        self.texts = []
        self.fail = False

    def cache_id(self):
        return "fake-tts"

    def synthesize(self, text, out_path, speaker=None):
        self.texts.append(text)
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("tts down")
        rate = 16000
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(int(rate * self.seconds)))
        with wave.open(str(out_path), "wb") as w:
            w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            w.writeframes(frames)


def make_srt(count=5, gap=3.0, length=2.0, text="Hello there"):
    rows = []
    for n in range(count):
        a, b = n * gap, n * gap + length
        stamp = lambda t: f"00:{int(t // 60):02d}:{t % 60:06.3f}".replace(".", ",")
        rows.append(f"{n + 1}\n{stamp(a)} --> {stamp(b)}\n{text} {n}\n")
    return "\n".join(rows)


@pytest.fixture
def live(tmp_path):
    made = []

    def start(tts=None, ttl=1800, **config):
        server = DubServer(Config(cache_dir=tmp_path / "cache", **config), translator=FakeTranslator(),
                           provider=tts or FakeTTS(), ttl=ttl)
        httpd = server.bind("127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        made.append(server)
        server.base = f"http://127.0.0.1:{httpd.server_address[1]}"
        return server

    yield start
    for server in made:
        server.httpd.shutdown()
        server.shutdown()


def call(server, method, path, body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(server.base + path, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read()
            return response.status, (payload if raw else (json.loads(payload) if payload else None)), response
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload), exc


def wait_for(check, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def create(server, srt=None, **extra):
    status, body, _ = call(server, "POST", "/v1/sessions", {"srt": srt or make_srt(), **extra})
    assert status == 201, body
    return body


def test_health(live):
    server = live()
    status, body, _ = call(server, "GET", "/v1/health")
    assert (status, body) == (200, {"ok": True, "version": "1", "translator": "opus-mt", "tts": "edge"})


def test_create_session_detects_language_and_cues(live):
    server = live()
    body = create(server)
    assert body["source_lang"] == "en" and body["cues"] == 5 and len(body["id"]) == 32
    vi = create(server, make_srt(2, text="Xin chào các bạn, tôi là người Việt"))
    assert vi["source_lang"] == "vi" and vi["cues"] == 2
    forced = create(server, source_lang="vi")
    assert forced["source_lang"] == "vi"


def test_cues_become_ready_with_translation_and_audio(live):
    server = live()
    sid = create(server)["id"]
    view = wait_for(lambda: (lambda v: v if v["state"] == "ready" else None)(
        call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=100")[1]))
    assert view["error"] is None and len(view["cues"]) == 5
    first = view["cues"][0]
    assert first["i"] == 0 and first["text"] == "vi Hello there 0" and first["start"] == 0 and first["end"] == 2
    assert first["audio"] == f"/v1/sessions/{sid}/audio/0.wav"
    assert view["ready_until"] == 14.0   # end of last cue: everything ready
    assert 0.4 < first["duration"] < 0.6


def test_cue_window_filters_by_start(live):
    server = live()
    sid = create(server)["id"]
    wait_for(lambda: call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=100")[1]["state"] == "ready")
    view = call(server, "GET", f"/v1/sessions/{sid}/cues?from=3&to=7")[1]
    assert [c["i"] for c in view["cues"]] == [1, 2]
    assert call(server, "GET", f"/v1/sessions/{sid}/cues")[1]["cues"][-1]["i"] == 4


def test_wav_is_mono_24k_s16le_with_content_length(live):
    server = live()
    sid = create(server)["id"]
    wait_for(lambda: call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=1")[1]["cues"])
    status, data, response = call(server, "GET", f"/v1/sessions/{sid}/audio/0.wav", raw=True)
    assert status == 200 and response.headers["Content-Length"] == str(len(data))
    assert response.headers["Content-Type"] == "audio/wav"
    path = server.sessions[sid].work / "0.wav"
    with wave.open(str(path), "rb") as clip:
        assert (clip.getnchannels(), clip.getsampwidth(), clip.getframerate(), clip.getcomptype()) == (1, 2, 24000, "NONE")
    assert data[:4] == b"RIFF"


def test_tempo_fitting_shortens_long_speech(live):
    # 3 s of speech into a 2 s slot (next cue 3 s later): capped at 1.35 -> ~2.2 s
    server = live(tts=FakeTTS(seconds=3.0), max_tempo=1.35)
    sid = create(server, make_srt(2, gap=2.0, length=2.0))["id"]
    view = wait_for(lambda: (lambda v: v if v["state"] == "ready" else None)(
        call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=10")[1]))
    assert 2.0 <= view["cues"][0]["duration"] <= 2.35


def test_ready_until_points_at_first_missing_cue(live):
    server = live(tts=FakeTTS(delay=0.3))
    sid = create(server, make_srt(30, gap=3.0))["id"]
    view = call(server, "GET", f"/v1/sessions/{sid}/cues?from=10&to=100")[1]
    assert view["state"] == "working"
    assert view["ready_until"] >= 10 and view["ready_until"] % 3 == 0
    wait_for(lambda: call(server, "GET", f"/v1/sessions/{sid}/cues?from=10&to=100")[1]["cues"])
    view = call(server, "GET", f"/v1/sessions/{sid}/cues?from=10&to=100")[1]
    first_missing = view["ready_until"]
    assert all(c["start"] < first_missing for c in view["cues"][:1])


def test_seek_reprioritises_worker(live):
    tts = FakeTTS(delay=0.15)
    server = live(tts=tts)
    sid = create(server, make_srt(200, gap=3.0))["id"]   # 600 s of cues
    call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=30")
    wait_for(lambda: call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=30")[1]["cues"])
    call(server, "GET", f"/v1/sessions/{sid}/cues?from=450&to=480")
    # cues around 450 s become ready long before the worker would reach them sequentially
    view = wait_for(lambda: (lambda v: v if v["cues"] else None)(
        call(server, "GET", f"/v1/sessions/{sid}/cues?from=450&to=480")[1]), timeout=10)
    assert view["cues"][0]["start"] >= 450
    session = server.sessions[sid]
    assert max(session.clips) >= 140 and len(session.clips) < 100


@pytest.mark.parametrize("playhead, first", [(0, 0), (300, 100)])
def test_first_cue_poll_starts_translation_at_requested_playhead(live, monkeypatch, playhead, first):
    entered = threading.Event()
    translated = []

    def translate(session, pos):
        translated.append(pos)
        entered.set()

    monkeypatch.setattr(Session, "_translate", translate)
    server = live()
    sid = create(server, make_srt(120))["id"]
    session = server.sessions[sid]
    with session.cond:
        # Run a scheduling pass explicitly so this assertion does not depend on
        # whether the background thread got CPU time between HTTP requests.
        session._schedule()
        assert not session.translating
        assert translated == []
    assert call(server, "GET", f"/v1/sessions/{sid}/cues?from={playhead}&to={playhead + 30}")[0] == 200
    assert entered.wait(5)
    assert translated == [first]


def test_vietnamese_skips_translation(live):
    server = live()
    sid = create(server, make_srt(2, text="Xin chào các bạn"))["id"]
    view = wait_for(lambda: (lambda v: v if v["state"] == "ready" else None)(
        call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=100")[1]))
    assert view["cues"][0]["text"] == "Xin chào các bạn 0"
    assert server._translator.calls == []


def test_tts_failure_reports_error(live):
    tts = FakeTTS()
    tts.fail = True
    server = live(tts=tts)
    sid = create(server, make_srt(12))["id"]
    view = wait_for(lambda: (lambda v: v if v["state"] == "error" else None)(
        call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=100")[1]))
    assert "tts down" in view["error"]


def test_delete_and_404s(live):
    server = live()
    sid = create(server)["id"]
    work = server.sessions[sid].work
    assert call(server, "DELETE", f"/v1/sessions/{sid}")[0] == 204
    assert not work.exists()
    for method, path in (("GET", f"/v1/sessions/{sid}/cues"), ("GET", f"/v1/sessions/{sid}/audio/0.wav"),
                         ("DELETE", f"/v1/sessions/{sid}"), ("GET", "/v1/nope"), ("GET", "/v1/sessions/zz/cues")):
        status, body, _ = call(server, method, path)
        assert status == 404 and "error" in body
    other = create(server)["id"]
    assert call(server, "GET", f"/v1/sessions/{other}/audio/99.wav")[0] == 404


def test_bad_requests(live):
    server = live()
    assert call(server, "POST", "/v1/sessions", {"nope": 1})[0] == 400
    assert call(server, "POST", "/v1/sessions", {"srt": "not an srt"})[0] == 400
    assert call(server, "POST", "/v1/sessions", {"srt": make_srt(), "source_lang": "fr"})[0] == 400
    sid = create(server)["id"]
    assert call(server, "GET", f"/v1/sessions/{sid}/cues?from=abc")[0] == 400


def test_sessions_expire_after_idle(live):
    server = live(ttl=0.3)
    sid = create(server)["id"]
    time.sleep(0.15)
    assert call(server, "GET", f"/v1/sessions/{sid}/cues")[0] == 200   # request refreshes the timer
    wait_for(lambda: sid not in server.sessions, timeout=5)
    assert call(server, "GET", f"/v1/sessions/{sid}/cues")[0] == 404


def test_tts_clips_cached_across_sessions(live):
    tts = FakeTTS()
    server = live(tts=tts)
    for _ in range(2):
        sid = create(server)["id"]
        wait_for(lambda: call(server, "GET", f"/v1/sessions/{sid}/cues?from=0&to=100")[1]["state"] == "ready")
    assert len(tts.texts) == 5


def test_advertised_addresses_are_only_the_default_route_interface():
    from jellydub.live.server import _usable_addresses
    adapters = [("lo", ["127.0.0.1"]), ("tailscale0", ["100.106.124.85"]), ("virbr0", ["192.168.122.1"]),
                ("docker0", ["172.17.0.1"]), ("br-1a2b", ["172.20.0.1"]), ("veth9", ["10.1.1.1"]),
                ("wlan1", ["192.168.122.9"]), ("wlp3s0", ["192.168.2.17", "169.254.1.2"])]
    assert _usable_addresses(adapters, "wlp3s0") == ["192.168.2.17"]
    assert _usable_addresses(adapters, None) == ["100.106.124.85", "192.168.2.17"]


@pytest.fixture
def manual_session(tmp_path, monkeypatch):
    """Exercise worker transitions without HTTP, ffmpeg, or scheduler timing."""
    monkeypatch.setattr(Session, "_run", lambda self: None)
    engine = Engine(object(), lambda cues: cues, tmp_path / "tts")
    cues = [Cue(i, i * 3.0, i * 3.0 + 2.0, f"Hello {i}") for i in range(120)]
    session = Session("manual", cues, "en", engine, tmp_path / "session")
    yield session
    session.close()


def test_direct_session_still_starts_without_playhead_hint(manual_session, monkeypatch):
    submitted = []
    monkeypatch.setattr(manual_session.tr_pool, "submit", lambda task, first: submitted.append(first))
    with manual_session.cond:
        manual_session._schedule()
    assert submitted == [0]


@pytest.mark.parametrize("playhead, expected", [(0.0, []), (300.0, [100])])
def test_translation_backlog_respects_current_playhead(manual_session, monkeypatch, playhead, expected):
    session = manual_session
    session.text.update({i: f"vi Hello {i}" for i in range(MAX_BACKLOG)})
    # All speech workers are busy: translation near a seek must not wait for the
    # old backlog to drain, while the unchanged playhead still applies its cap.
    session.inflight.add(0)
    submitted = []
    monkeypatch.setattr(session.tr_pool, "submit", lambda task, first: submitted.append(first))
    session.set_playhead(playhead)
    with session.cond:
        session._schedule()
    assert submitted == expected


@pytest.mark.parametrize("target_callback", [False, True])
def test_translation_callback_preserves_targets_and_existing_context(manual_session, target_callback):
    session = manual_session
    session.text[3] = "existing context"
    calls = []

    def translated(cues):
        return [Cue(c.index, c.start, c.end, "vi " + c.text) for c in cues]

    def translate(cues):
        calls.append([cue.index for cue in cues])
        return translated(cues)

    def translate_targets(cues, first, end, known_text):
        assert cues is session.cues
        assert (first, end) == (4, 10)
        assert known_text == {3: "existing context"}
        assert known_text is not session.text
        calls.append([cue.index for cue in cues[first:end]])
        return translated(cues[first:end])

    session.engine.translate = translate
    if target_callback:
        session.engine.translate_targets = translate_targets
    session._translate(4)
    assert calls == [list(range(4 if target_callback else 1, 10))]
    assert session.text == {3: "existing context", **{i: f"vi Hello {i}" for i in range(4, 10)}}


@pytest.mark.parametrize("fail", [False, True])
def test_translation_completion_after_close_is_discarded(manual_session, fail):
    session = manual_session
    entered, release = threading.Event(), threading.Event()

    def translate(cues):
        entered.set()
        assert release.wait(5)
        if fail:
            raise RuntimeError("late translation failure")
        return [Cue(c.index, c.start, c.end, "vi " + c.text) for c in cues]

    session.engine.translate = translate
    session.translating = True
    future = session.tr_pool.submit(session._translate, 0)
    try:
        assert entered.wait(5)
        session.close()
    finally:
        release.set()
        future.result(timeout=5)
    assert session.text == {}
    assert session.error is None
    assert not session.translating


@pytest.mark.parametrize("fail", [False, True])
def test_speech_completion_after_close_is_discarded(manual_session, monkeypatch, fail):
    session = manual_session
    entered, release = threading.Event(), threading.Event()

    def synthesize(pos):
        entered.set()
        assert release.wait(5)
        # Alignment may recreate the directory after close removed it.
        session.work.mkdir(parents=True, exist_ok=True)
        path = session.work / f"{pos}.wav"
        path.write_bytes(b"late audio")
        if fail:
            raise RuntimeError("late speech failure")
        return path, 0.5

    monkeypatch.setattr(session, "_synthesize", synthesize)
    session.inflight.add(0)
    session.failures = MAX_TTS_FAILURES - 1
    future = session.tts_pool.submit(session._speak, 0)
    try:
        assert entered.wait(5)
        session.close()
    finally:
        release.set()
        future.result(timeout=5)
    assert session.clips == {}
    assert session.skipped == set()
    assert session.successes == 0
    assert session.failures == MAX_TTS_FAILURES - 1
    assert session.error is None
    assert session.inflight == set()
    assert not session.work.exists()
