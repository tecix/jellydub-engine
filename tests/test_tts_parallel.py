"""Parallel orchestration tests: fake providers, isolated caches, no service calls."""
import threading

import pytest

from jellydub.models import Cue
from jellydub.tts.base import TTSError, TTSProvider
from jellydub.tts.edge import EdgeTTS
from jellydub.tts.vieneu import VieNeuTTS
from jellydub.tts.synthesize import _AdaptiveLimiter, synthesize_cues


@pytest.fixture
def fast_audio(monkeypatch):
    import jellydub.tts.synthesize as module
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(module, "_valid_duration", lambda path: 0.1 if path.exists() else None)
    monkeypatch.setattr(module, "run_ffmpeg", lambda args: __import__("pathlib").Path(args[-1]).write_bytes(b"normalized"))
    return module


class ParallelFake(TTSProvider):
    max_concurrency = 3

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = []
        self.active = self.peak = 0
        self.barrier = threading.Barrier(2)

    def cache_id(self):
        return "parallel-fake"

    def synthesize(self, text, out_path, speaker=None):
        with self.lock:
            self.calls.append((text, speaker))
            self.active += 1
            self.peak = max(self.peak, self.active)
            call_number = len(self.calls)
        try:
            if call_number <= 2:
                self.barrier.wait(timeout=3)
            threading.Event().wait(0.02 if text == "first" else 0.005)
            out_path.write_bytes(b"speech")
        finally:
            with self.lock:
                self.active -= 1


@pytest.mark.parametrize("workers", [2, 8])
def test_parallel_cap_order_dedupe_and_progress(tmp_path, fast_audio, workers):
    provider = ParallelFake()
    cues = [Cue(i, 0, 1, text, speaker) for i, (text, speaker) in enumerate([
        ("first", None), ("second", None), ("first", None),
        ("third", None), ("first", "other"), ("", None)], 1)]
    progress = []
    caller = threading.get_ident()
    def report(done, total):
        assert threading.get_ident() == caller
        progress.append((done, total))
    results, failures = synthesize_cues(cues, provider, tmp_path, workers=workers, on_progress=report)
    assert not failures
    assert 1 < provider.peak <= min(workers, 3)
    assert len(provider.calls) == 4
    assert [result.cue_index for result in results] == [1, 2, 3, 4, 5]
    assert results[0].path == results[2].path
    assert results[0].path != results[4].path
    assert progress == [(i, 6) for i in range(1, 7)]
    synthesize_cues(cues, provider, tmp_path, workers=8)
    assert len(provider.calls) == 4


def test_limiter_halves_recovers_and_bounds():
    limiter = _AdaptiveLimiter(8)
    limiter.acquire()
    limiter.release(transient=True)
    assert limiter.allowed == 4
    for _ in range(19):
        limiter.acquire()
        limiter.release(success=True)
    assert limiter.allowed == 4
    limiter.acquire()
    limiter.release(success=True)
    assert limiter.allowed == 5
    for _ in range(4):
        limiter.acquire()
        limiter.release(transient=True)
    assert limiter.allowed == 1
    for _ in range(200):
        limiter.acquire()
        limiter.release(success=True)
    assert limiter.allowed == 8


def test_transient_failure_reduces_shared_limit_and_dedupes_failures(tmp_path, fast_audio, monkeypatch):
    changes = []
    original = fast_audio._AdaptiveLimiter.release
    def observe(self, **kwargs):
        original(self, **kwargs)
        changes.append((kwargs, self.allowed))
    monkeypatch.setattr(fast_audio._AdaptiveLimiter, "release", observe)
    class Failing(TTSProvider):
        max_concurrency = 4
        calls = 0
        def cache_id(self):
            return "failing"
        def synthesize(self, text, out_path, speaker=None):
            self.calls += 1
            raise TTSError("throttled", transient=True)
    provider = Failing()
    cues = [Cue(i, 0, 1, "same") for i in range(3)]
    results, failures = synthesize_cues(cues, provider, tmp_path, workers=8, retries=1)
    assert not results and [cue.index for cue, _ in failures] == [0, 1, 2]
    assert provider.calls == 2
    assert [allowed for _, allowed in changes] == [2, 1]


def test_provider_caps_and_worker_floor(tmp_path, fast_audio):
    assert EdgeTTS.max_concurrency == 8
    assert VieNeuTTS.max_concurrency == 1
    class Serial(TTSProvider):
        def cache_id(self):
            return "serial"
        def synthesize(self, text, out_path, speaker=None):
            out_path.write_bytes(b"speech")
    assert len(synthesize_cues([Cue(1, 0, 1, "line")], Serial(), tmp_path, workers=0)[0]) == 1


@pytest.mark.parametrize("text", [
    "", " \n\t", "*", "***", "♪ ♫ # ~ - – — ... !?", "[Nhạc]",
    "(music)", "[MUSIC PLAYING]", "[Tiếng cười]\n(music)",
    "* [Nhạc] *", "[music] (applause)", "♪\n[Nhạc]\n*",
])
def test_non_speech_skipped_without_requests(tmp_path, fast_audio, text):
    class NeverCalled(TTSProvider):
        max_concurrency = 8
        def cache_id(self):
            return "unused"
        def synthesize(self, *args, **kwargs):
            pytest.fail("non-speech must not reach TTS")
    progress = []
    cues = [Cue(i, 0, 1, text) for i in range(13)]
    results, failures = synthesize_cues(
        cues, NeverCalled(), tmp_path, workers=4,
        on_progress=lambda *args: progress.append(args))
    assert not results and not failures
    assert progress == [(i, 13) for i in range(1, 14)]
    assert not list(tmp_path.iterdir())


def test_mixed_speech_preserves_text_cache_keys_and_order(tmp_path, fast_audio):
    from jellydub.cache import cache_key
    class Recording(TTSProvider):
        max_concurrency = 4
        def __init__(self):
            self.calls = []
        def cache_id(self):
            return "unchanged"
        def synthesize(self, text, out_path, speaker=None):
            self.calls.append((text, speaker))
            out_path.write_bytes(b"speech")
    texts = ["*", "♪ Xin chào! ♪", "[Nhạc]\nTôi ở đây.", "123", "(music)",
             "Hello [quietly]", "[MUSIC PLAYING] Hello", "đ", "*"]
    cues = [Cue(i, 0, 1, text, "actor") for i, text in enumerate(texts)]
    provider = Recording()
    results, failures = synthesize_cues(cues, provider, tmp_path, workers=4)
    expected = [1, 2, 3, 5, 6, 7]
    assert not failures
    assert [result.cue_index for result in results] == expected
    assert sorted(provider.calls) == sorted((texts[i], "actor") for i in expected)
    for result in results:
        assert result.path.name == cache_key("unchanged", texts[result.cue_index], "actor") + ".wav"
    synthesize_cues(cues, provider, tmp_path, workers=4)
    assert len(provider.calls) == len(expected)
