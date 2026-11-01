"""Live LLM translation uses stable source context without retranslating it."""
import json

from jellydub.config import Config
from jellydub.live.server import DubServer
from jellydub.models import Cue
from jellydub.translate import _ChunkedTranslator, translate_cues


class ContextTranslator(_ChunkedTranslator):
    def __init__(self):
        super().__init__("test-live-context")
        self.calls = []

    def _request(self, prompt, schema):
        data = json.loads(prompt[prompt.index("{"):])
        self.calls.append(data)
        return ["vi " + cue["text"] for cue in data["cues"]]


def source_cues():
    return [Cue(i, i * 3, i * 3 + 2, f"Line {i}.") for i in range(24)]


def test_live_chunks_translate_only_targets_and_reuse_context(tmp_path):
    translator = ContextTranslator()
    server = DubServer(Config(cache_dir=tmp_path), translator=translator)
    cues = source_cues()
    known = {}
    for first in (0, 6, 12):
        translated = server._translate_targets(cues, first, first + 6, known)
        assert [c.index for c in translated] == list(range(first, first + 6))
        known.update({c.index: c.text for c in translated})

    assert [[c["text"] for c in call["cues"]] for call in translator.calls] == [
        [f"Line {i}." for i in range(first, first + 6)] for first in (0, 6, 12)
    ]
    assert translator.calls[1]["context"] == [
        {"source": f"Line {i}.", "translated": f"vi Line {i}."} for i in range(1, 6)
    ]
    # A seek across old chunk boundaries, or another session, reuses the same keys.
    again = server._translate_targets(cues, 4, 10, {})
    assert len(translator.calls) == 3
    assert [c.text for c in again] == [f"vi Line {i}." for i in range(4, 10)]


def test_live_seek_uses_cached_context_without_translating_unknown_context(tmp_path):
    translator = ContextTranslator()
    server = DubServer(Config(cache_dir=tmp_path), translator=translator)
    cues = source_cues()
    server._translate_targets(cues, 0, 6, {})
    server._translate_targets(cues, 6, 8, {})
    assert len(translator.calls[1]["context"]) == 5
    server._translate_targets(cues, 18, 24, {})
    assert translator.calls[-1]["context"] == []
    assert [c["text"] for c in translator.calls[-1]["cues"]] == [f"Line {i}." for i in range(18, 24)]


def test_live_reuses_full_subtitle_translation_cache(tmp_path):
    translator = ContextTranslator()
    server = DubServer(Config(cache_dir=tmp_path), translator=translator)
    cues = source_cues()
    translate_cues(cues, translator, server.root / "translations")
    translator.calls.clear()
    out = server._translate_targets(cues, 8, 14, {})
    assert translator.calls == []
    assert [c.text for c in out] == [f"vi Line {i}." for i in range(8, 14)]


def test_plain_translator_keeps_context_fallback(tmp_path):
    class PlainTranslator:
        def cache_id(self):
            return "plain-live"

        def translate_batch(self, texts):
            return ["vi " + text for text in texts]

    server = DubServer(Config(cache_dir=tmp_path), translator=PlainTranslator())
    out = server._translate_targets(source_cues(), 6, 12, {})
    assert [c.index for c in out] == list(range(6, 12))
    assert [c.text for c in out] == [f"vi Line {i}." for i in range(6, 12)]
