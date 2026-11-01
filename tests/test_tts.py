import sys
from types import SimpleNamespace

import pytest

from jellydub.ffmpeg import probe, run_ffmpeg
from jellydub.models import Cue
from jellydub.tts import get_provider
from jellydub.tts.base import TTSError, TTSProvider
from jellydub.tts.edge import EdgeTTS
from jellydub.tts.synthesize import synthesize_cues
from jellydub.tts.vieneu import VieNeuTTS


class Fake(TTSProvider):
    def __init__(self, errors=()):
        self.calls = 0
        self.errors = list(errors)

    def cache_id(self):
        return "fake"

    def synthesize(self, text, out_path, speaker=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=0.15", str(out_path)])


def test_cache_retry_failure_and_progress(tmp_path, monkeypatch):
    monkeypatch.setattr("jellydub.tts.synthesize.time.sleep", lambda _: None)
    provider = Fake([TTSError("retry", transient=True)])
    cues = [Cue(1, 0, 1, "hello"), Cue(2, 1, 2, " ")]
    progress = []
    results, failures = synthesize_cues(cues, provider, tmp_path, on_progress=lambda *p: progress.append(p))
    assert len(results) == 1 and not failures and provider.calls == 2
    assert progress == [(1, 2), (2, 2)]
    stream = probe(results[0].path)["streams"][0]
    assert (stream["sample_rate"], stream["channels"], stream["codec_name"]) == ("48000", 1, "pcm_s16le")
    assert synthesize_cues(cues, provider, tmp_path)[0] == results
    assert provider.calls == 2
    provider = Fake([TTSError("permanent")])
    results, failures = synthesize_cues([Cue(3, 0, 1, "other"), Cue(4, 0, 1, "works")], provider, tmp_path)
    assert len(failures) == len(results) == 1
    assert failures[0][0].index == 3


def test_retry_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setattr("jellydub.tts.synthesize.time.sleep", lambda _: None)
    provider = Fake([TTSError("offline", True)] * 3)
    results, failures = synthesize_cues([Cue(1, 0, 1, "hello")], provider, tmp_path, retries=2)
    assert not results and len(failures) == 1 and provider.calls == 3
    assert not list(tmp_path.glob("*.part*"))


def test_edge_options_and_error_mapping(tmp_path, monkeypatch):
    calls = []
    class Communicate:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
        async def save(self, path):
            raise TimeoutError("offline")
    monkeypatch.setitem(sys.modules, "edge_tts", SimpleNamespace(Communicate=Communicate))
    provider = EdgeTTS(rate="+10%")
    with pytest.raises(TTSError) as err:
        provider.synthesize("test", tmp_path / "out.wav")
    assert err.value.transient
    assert calls[0][1]["rate"] == "+10%"
    assert EdgeTTS().cache_id() != provider.cache_id()
    assert isinstance(get_provider("edge"), EdgeTTS)


def test_vieneu_lazy_profile_cache_and_close(tmp_path, monkeypatch):
    ref = tmp_path / "ref.wav"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=duration=3.1", str(ref)])
    instances = []
    class Model:
        def __init__(self, **kwargs):
            instances.append(self)
            self.added = 0
            self.closed = False
        def add_voice(self, key, path):
            self.key = key
            self.added += 1
        def save_voices(self, path):
            import json
            from pathlib import Path
            Path(path).write_text(json.dumps({"presets": {self.key: {"speaker_emb": [1], "codes": [2]}}}))
        def get_preset_voice(self, key):
            return {"speaker_emb": [1], "codes": [2]}
        def infer(self, text, voice):
            self.voice = voice
            return "audio"
        def save(self, audio, path):
            pass
        def close(self):
            self.closed = True
    monkeypatch.setitem(sys.modules, "vieneu", SimpleNamespace(Vieneu=Model))
    provider = VieNeuTTS(speaker_refs={"actor": ref}, cache_dir=tmp_path / "profiles")
    assert not instances
    provider.synthesize("hello", tmp_path / "out.wav", speaker="actor")
    provider.synthesize("again", tmp_path / "out.wav", speaker="actor")
    assert len(instances) == 1 and instances[0].added == 1
    assert list((tmp_path / "profiles").glob("*.json"))
    with pytest.warns(UserWarning, match="No reference"):
        provider.synthesize("fallback", tmp_path / "out.wav", speaker="missing")
    assert instances[0].voice == "Hải Đăng"
    provider.close()
    assert instances[0].closed


def test_vieneu_invalid_reference(tmp_path):
    provider = VieNeuTTS(reference=tmp_path / "missing.wav")
    with pytest.warns(UserWarning, match="3–8"):
        assert provider._voice(object(), None) == "Hải Đăng"


def test_corrupt_cache_regenerated_and_silence_rejected(tmp_path):
    from jellydub.cache import cache_key
    target = tmp_path / (cache_key("fake", "hello", None) + ".wav")
    target.write_bytes(b"interrupted")
    provider = Fake()
    results, failures = synthesize_cues([Cue(1, 0, 1, "hello")], provider, tmp_path)
    assert results and not failures and provider.calls == 1
    class Silent(Fake):
        def synthesize(self, text, out_path, speaker=None):
            run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "0.1", str(out_path)])
    results, failures = synthesize_cues([Cue(2, 0, 1, "silence")], Silent(), tmp_path, retries=0)
    assert not results and len(failures) == 1
