from pathlib import Path
from types import SimpleNamespace

import pytest

from jellydub import pipeline as p
from jellydub.config import Config
from jellydub.models import Cue, JellyDubError, PlacedClip, SynthResult


@pytest.fixture
def stages(monkeypatch, tmp_path):
    events = []
    video, srt = tmp_path / 'movie[1].mkv', tmp_path / 'input.srt'
    video.write_bytes(b'video')
    srt.write_text('Vietnamese')
    cue = Cue(1, 1, 2, 'Xin chào')
    def output(name):
        def write(*args, **kwargs):
            events.append(name)
            path = args[-1]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'audio')
        return write
    monkeypatch.setattr(p, 'assess_subtitle', lambda *a, **kw: SimpleNamespace(score=0.95, issues=[], stats={}))
    monkeypatch.setattr(p, 'strip_junk', lambda cues: cues)
    monkeypatch.setattr(p, 'find_local_subtitles', lambda *a, **kw: [])
    monkeypatch.setattr(p, 'extract_embedded_subtitles', lambda *a, **kw: [])
    monkeypatch.setattr(p, 'download_subtitles', lambda *a, **kw: [])
    monkeypatch.setattr(p, 'require_ffmpeg', lambda: None)
    monkeypatch.setattr(p, 'duration', lambda v: 15.0)
    monkeypatch.setattr(p, 'parse_srt', lambda f: [cue])
    monkeypatch.setattr(p, 'write_srt', lambda cues, f: f.write_text('translated'))
    monkeypatch.setattr(p, 'output_paths', lambda v, d: {k: d / (v.stem + ext) for k, ext in
                                                       [('srt', '.vi.srt'), ('audio', '.AI-Dub.vi.mp3'), ('mkv', '.AI-Dub.vi.mkv')]})
    def provider(*args, **kwargs):
        events.append(('provider', kwargs))
        return SimpleNamespace(close=lambda: events.append('tts-close'))
    monkeypatch.setattr(p, 'get_provider', provider)
    monkeypatch.setattr(p, 'synthesize_cues', lambda *a, **kw: ([SynthResult(1, tmp_path / 'clip.wav', 1)], []))
    monkeypatch.setattr(p, 'align', lambda *a, **kw: [PlacedClip(1, tmp_path / 'clip.wav', 1, 1)])
    monkeypatch.setattr(p, 'render_timeline', output('render'))
    monkeypatch.setattr(p, 'extract_audio', lambda v, f, **kw: output('extract')(f))
    monkeypatch.setattr(p, 'mix', lambda mode, dub, f, **kw: output('mix-' + mode.value)(f))
    monkeypatch.setattr(p, 'export_mp3', output('export'))
    monkeypatch.setattr(p, 'mux_mkv', output('mux'))
    monkeypatch.setattr(p, 'check_audio', lambda *a: [])
    config = Config(source_lang='vi', out_dir=tmp_path / 'out', cache_dir=tmp_path / 'cache')
    return video, srt, config, events


def test_resume_and_out_dir(stages):
    video, srt, config, events = stages
    config.mkv = True
    outputs = p.dub(video, srt, config, report=lambda s: None)
    assert all(path.is_file() and path.parent == config.out_dir for path in outputs.values())
    assert video.read_bytes() == b'video' and srt.read_text() == 'Vietnamese'
    p.dub(video, srt, config, report=lambda s: None)
    assert events.count('render') == events.count('export') == events.count('mux') == 1
    srt.write_text('edited')
    p.dub(video, srt, config, report=lambda s: None)
    assert events.count('render') == 2


def test_translation_closes_before_tts(stages, monkeypatch):
    video, srt, config, events = stages
    config.source_lang = 'en'
    monkeypatch.setattr(p, 'get_translator', lambda **kw: SimpleNamespace(close=lambda: events.append('translate-close')))
    monkeypatch.setattr(p, 'translate_cues', lambda cues, *a, **kw: cues)
    p.dub(video, srt, config, report=lambda s: None)
    assert events.index('translate-close') < next(i for i, v in enumerate(events) if isinstance(v, tuple))


def test_failed_segments_close_and_stop(stages, monkeypatch):
    video, srt, config, events = stages
    monkeypatch.setattr(p, 'synthesize_cues', lambda *a, **kw: ([], [(Cue(1, 1, 2, 'x'), 'offline')]))
    messages = []
    with pytest.raises(JellyDubError, match='Re-run'):
        p.dub(video, srt, config, report=messages.append)
    assert 'tts-close' in events and 'render' not in events
    assert any('offline' in m for m in messages)


def test_replace_failure_and_opt_in_fallback(stages, monkeypatch):
    video, srt, config, events = stages
    config.mix = 'replace'
    def fail(*a, **kw):
        raise JellyDubError('missing model')
    monkeypatch.setattr(p, 'separate_background', fail)
    with pytest.raises(JellyDubError, match='--mix voiceover'):
        p.dub(video, srt, config, report=lambda s: None)
    config.fallback_voiceover = True
    p.dub(video, srt, config, report=lambda s: None)
    assert 'mix-voiceover' in events


def test_invalid_audio_not_published(stages, monkeypatch):
    video, srt, config, events = stages
    monkeypatch.setattr(p, 'check_audio', lambda *a: ['silent output'])
    with pytest.raises(JellyDubError, match='silent output'):
        p.dub(video, srt, config, report=lambda s: None)
    assert not list(config.out_dir.glob('*.mp3'))


def test_translation_exception_releases_model(stages, monkeypatch):
    video, srt, config, events = stages
    config.source_lang = 'en'
    monkeypatch.setattr(p, 'get_translator', lambda **kw: SimpleNamespace(close=lambda: events.append('translate-close')))
    def fail(*args, **kw):
        raise JellyDubError('translation failure')
    monkeypatch.setattr(p, 'translate_cues', fail)
    with pytest.raises(JellyDubError):
        p.dub(video, srt, config, report=lambda s: None)
    assert 'translate-close' in events
    assert not any(isinstance(v, tuple) for v in events)


def test_tail_overflow_never_truncates(stages, monkeypatch):
    video, srt, config, events = stages
    monkeypatch.setattr(p, 'align', lambda *a, **kw: [PlacedClip(1, Path('x'), 14, 2, warning='overflow')])
    with pytest.raises(JellyDubError, match='avoid truncation'):
        p.dub(video, srt, config, report=lambda s: None)
    assert 'render' not in events


def test_partial_stage_is_not_reused(tmp_path):
    path = tmp_path / 'timeline.wav'
    def fail(temporary):
        temporary.write_bytes(b'partial')
        raise JellyDubError('interrupted')
    with pytest.raises(JellyDubError):
        p._stage(path, fail)
    assert not path.exists() and not list(tmp_path.iterdir())


@pytest.mark.parametrize('name,text,expected', [
    ('movie.vi.srt', 'Hello', 'vi'), ('movie.vie.srt', 'Hello', 'vi'),
    ('movie.VI.SRT', 'Hello', 'vi'), ('movie.srt', 'Xin chào thế giới', 'vi'),
    ('movie.srt', 'Hello world', 'en'), ('movie.vietnamese.srt', 'Hello', 'en'),
])
def test_language_detection(tmp_path, monkeypatch, name, text, expected):
    monkeypatch.setattr(p, 'parse_srt', lambda f: [Cue(1, 0, 1, text)])
    assert p.detect_source_language(tmp_path / name)[0] == expected


def test_diacritics_threshold(tmp_path, monkeypatch):
    cues = [Cue(n, n, n + 1, 'Hello' if n else 'Chào') for n in range(10)]
    monkeypatch.setattr(p, 'parse_srt', lambda f: cues)
    assert p.detect_source_language(tmp_path / 'x.srt')[0] == 'en'
    for cue in cues[:4]:
        cue.text = 'Việt Nam'
    assert p.detect_source_language(tmp_path / 'x.srt')[0] == 'vi'


@pytest.mark.parametrize('language', ['vi', 'auto'])
def test_vietnamese_never_loads_translator(stages, monkeypatch, language):
    video, srt, config, events = stages
    config.source_lang = language
    def forbidden(**kw):
        pytest.fail('Vietnamese input must never load translator')
    monkeypatch.setattr(p, 'get_translator', forbidden)
    messages = []
    p.dub(video, srt, config, report=messages.append)
    assert (config.out_dir / (video.stem + '.vi.srt')).read_bytes() == srt.read_bytes()
    assert any('skipping translation' in m for m in messages)


def test_import_pipeline_does_not_import_translation():
    import subprocess
    import sys
    proc = subprocess.run([sys.executable, '-c',
        'import sys; import jellydub.pipeline; assert "jellydub.translate" not in sys.modules'],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_vietnamese_same_output_leaves_source(stages, monkeypatch):
    video, _, config, events = stages
    srt = config.out_dir / (video.stem + '.vi.srt')
    srt.parent.mkdir(parents=True)
    srt.write_bytes(b'original Vietnamese subtitles')
    before = srt.stat().st_mtime_ns
    config.source_lang = 'auto'
    p.dub(video, srt, config, report=lambda s: None)
    assert srt.read_bytes() == b'original Vietnamese subtitles'
    assert srt.stat().st_mtime_ns == before


@pytest.mark.parametrize('engine,workers', [('edge', 4), ('edge', 3), ('vieneu', 6)])
def test_workers_forwarded_to_synthesis(stages, monkeypatch, engine, workers):
    video, srt, config, events = stages
    config.tts = engine
    config.tts_workers = workers
    seen = []
    def synthesize(cues, provider, cache_dir, *, workers, on_progress):
        seen.append(workers)
        on_progress(1, 1)
        return [SynthResult(1, Path('clip.wav'), 1)], []
    monkeypatch.setattr(p, 'synthesize_cues', synthesize)
    p.dub(video, srt, config, report=lambda s: None)
    # The synthesis layer applies provider caps, not the pipeline.
    assert seen == [workers]
    assert 'tts-close' in events


def test_workers_preserve_existing_cache_key(stages):
    from dataclasses import asdict
    from jellydub.cache import cache_key
    video, srt, config, _ = stages
    old_settings = asdict(config)
    for option in ('tts_workers', 'subs', 'download', 'search_online', 'providers', 'subtitle_credentials_env'):
        old_settings.pop(option)
    old_key = cache_key(p._fingerprint(video), srt.read_bytes().hex(), old_settings, [])
    expected = config.cache_dir / f'{video.stem}-{old_key}'
    assert p._work_dir(video, srt, config) == expected
    config.tts_workers = 1
    assert p._work_dir(video, srt, config) == expected
    config.tts_workers = 8
    assert p._work_dir(video, srt, config) == expected


@pytest.mark.parametrize('command', ['translate', 'dub'])
@pytest.mark.parametrize('name,model', [('opus-mt', None), ('agy', None), ('agy', 'custom-model')])
def test_translator_selection_forwarded(stages, monkeypatch, command, name, model):
    video, srt, config, events = stages
    config.source_lang = 'en'
    config.translator = name
    config.translator_model = model
    seen = []
    def factory(**options):
        seen.append(options)
        return SimpleNamespace(close=lambda: events.append('translate-close'))
    monkeypatch.setattr(p, 'get_translator', factory)
    monkeypatch.setattr(p, 'translate_cues', lambda cues, *a, **kw: cues)
    getattr(p, command)(video, srt, config, report=lambda s: None)
    assert seen == [{'name': name, 'model': model, 'device': config.device}]
    assert events.count('translate-close') == 1


@pytest.mark.parametrize('command', ['translate', 'dub'])
@pytest.mark.parametrize('language', ['auto', 'vi'])
def test_agy_skipped_for_vietnamese(stages, monkeypatch, command, language):
    video, srt, config, _ = stages
    config.source_lang = language
    config.translator = 'agy'
    def forbidden(*args, **kwargs):
        pytest.fail('Vietnamese subtitles must skip every translation backend')
    monkeypatch.setattr(p, 'get_translator', forbidden)
    monkeypatch.setattr(p, 'translate_cues', forbidden)
    getattr(p, command)(video, srt, config, report=lambda s: None)
    assert (config.out_dir / (video.stem + '.vi.srt')).read_bytes() == srt.read_bytes()


def test_translator_selection_changes_work_cache(stages):
    video, srt, config, _ = stages
    opus_cache = p._work_dir(video, srt, config)
    config.translator = 'agy'
    agy_cache = p._work_dir(video, srt, config)
    config.translator_model = 'other-model'
    custom_cache = p._work_dir(video, srt, config)
    assert len({opus_cache, agy_cache, custom_cache}) == 3


@pytest.mark.parametrize('command', ['translate', 'dub'])
@pytest.mark.parametrize('language', ['en', 'vi', 'auto'])
def test_gemini_factory_and_vietnamese_skip(stages, monkeypatch, command, language):
    video, srt, config, events = stages
    config.source_lang = language
    config.translator = 'gemini'
    config.translator_model = 'test-model'
    seen = []
    def factory(**options):
        seen.append(options)
        return SimpleNamespace(close=lambda: events.append('gemini-close'))
    monkeypatch.setattr(p, 'get_translator', factory)
    monkeypatch.setattr(p, 'translate_cues', lambda cues, *a, **kw: cues)
    getattr(p, command)(video, srt, config, report=lambda message: None)
    if language == 'en':
        assert seen == [{'name': 'gemini', 'model': 'test-model', 'device': config.device}]
        assert events.count('gemini-close') == 1
    else:
        assert seen == []
        assert 'gemini-close' not in events


def test_gemini_credentials_not_used_in_pipeline_state(stages, monkeypatch):
    from dataclasses import asdict
    video, srt, config, _ = stages
    config.translator = 'gemini'
    seen = []
    messages = []
    config.source_lang = 'en'
    def factory(**options):
        seen.append(options)
        return SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(p, 'get_translator', factory)
    monkeypatch.setattr(p, 'translate_cues', lambda cues, *a, **kw: cues)
    # Synthetic sentinels only: never inspect or print any actual credentials.
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-key-one')
    first_cache = p._work_dir(video, srt, config)
    p.translate(video, srt, config, report=messages.append)
    monkeypatch.setenv('GEMINI_API_KEY', 'synthetic-key-two')
    assert p._work_dir(video, srt, config) == first_cache
    assert set(seen[0]) == {'name', 'model', 'device'}
    recorded = str((messages, seen, asdict(config)))
    assert 'synthetic-key-' not in recorded


@pytest.fixture
def subtitle_sources(stages, monkeypatch, tmp_path):
    video, explicit, config, events = stages
    vi = tmp_path / 'other-release.vi.srt'
    vi.write_text('Vietnamese source unchanged')
    en = tmp_path / 'movie.en.srt'
    en.write_text('English timing reference')
    vi_candidate = SimpleNamespace(path=vi, origin='local', language='vi', score=0.9, note='episode title match')
    en_candidate = SimpleNamespace(path=en, origin='sidecar', language='en', score=1.0, note='exact name')
    monkeypatch.setattr(p, 'find_local_subtitles', lambda v, lang: [vi_candidate] if lang == 'vi' else [en_candidate])
    monkeypatch.setattr(p, 'estimate_sync', lambda *a: SimpleNamespace(offset=-0.36, scale=1.0, matched_ratio=0.9, residual=0.04))
    monkeypatch.setattr(p, 'apply_sync', lambda cues, result: [Cue(c.index, c.start + result.offset, c.end + result.offset, c.text, c.speaker) for c in cues])
    monkeypatch.setattr(p, 'get_translator', lambda **kw: pytest.fail('existing Vietnamese must not be translated'))
    return video, config, vi_candidate, en_candidate, events


def test_automatic_local_vi_is_synced_without_translating(subtitle_sources, monkeypatch):
    video, config, vi, _, events = subtitle_sources
    before = vi.path.read_bytes(), vi.path.stat().st_mtime_ns
    monkeypatch.setattr(p, 'download_subtitles', lambda *a, **kw: [])
    messages = []
    outputs = p.dub(video, None, config, report=messages.append)
    assert outputs['srt'].read_text() == 'translated'  # fake write_srt serializes corrected cues
    assert (vi.path.read_bytes(), vi.path.stat().st_mtime_ns) == before
    assert any('offset -0.360s' in line for line in messages)
    assert any('skipping translation' in line for line in messages)


def test_bad_sync_candidate_rejected_then_next_used(subtitle_sources, monkeypatch, tmp_path):
    video, config, vi, _, _ = subtitle_sources
    config.search_online = True  # exercise ranking without the local fast-path probe
    bad = SimpleNamespace(path=tmp_path / 'bad.vi.srt', origin='local', language='vi', score=1.0, note='wrong release')
    bad.path.write_text('bad')
    monkeypatch.setattr(p, 'find_local_subtitles', lambda v, lang: [bad, vi] if lang == 'vi' else [])
    monkeypatch.setattr(p, '_english_reference', lambda *a: [Cue(1, 1, 2, 'reference')])
    count = []
    def estimate(*args):
        count.append(True)
        return SimpleNamespace(offset=-0.36, scale=1.0, matched_ratio=0.1 if len(count) == 1 else 0.9, residual=0.02)
    monkeypatch.setattr(p, 'estimate_sync', estimate)
    messages = []
    p.dub(video, None, config, report=messages.append)
    assert len(count) == 2 and any('Rejected' in line and 'matched_ratio=0.100' in line for line in messages)


@pytest.mark.parametrize('ratio,residual', [(0.49, 0.1), (0.9, 0.31)])
def test_reject_explicit_vi_with_bad_sync(subtitle_sources, monkeypatch, ratio, residual):
    video, config, vi, _, _ = subtitle_sources
    monkeypatch.setattr(p, 'estimate_sync', lambda *a: SimpleNamespace(offset=0., scale=1., matched_ratio=ratio, residual=residual))
    with pytest.raises(JellyDubError, match='failed timing checks'):
        p.dub(video, vi.path, config, report=lambda s: None)


def test_download_vi_precedes_translation(subtitle_sources, monkeypatch):
    import importlib.util
    video, config, vi, en, _ = subtitle_sources
    config.source_lang = 'auto'
    monkeypatch.setattr(p, 'find_local_subtitles', lambda v, lang: [en] if lang == 'en' else [])
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name: object())
    calls = []
    def download(v, lang, out, **kwargs):
        calls.append((lang, kwargs))
        return [vi]
    monkeypatch.setattr(p, 'download_subtitles', download)
    p.dub(video, None, config, report=lambda s: None)
    assert calls[0][0] == 'vi' and calls[0][1]['max_downloads'] == 2


@pytest.mark.parametrize('mode,download,extra', [('auto', False, True), ('auto', True, False), ('translate', True, True)])
def test_english_translation_fallback(subtitle_sources, monkeypatch, mode, download, extra):
    import importlib.util
    video, config, _, en, events = subtitle_sources
    config.subs, config.download = mode, download
    monkeypatch.setattr(p, 'find_local_subtitles', lambda v, lang: [en] if lang == 'en' else [])
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name: object() if extra else None)
    monkeypatch.setattr(p, 'download_subtitles', lambda *a, **kw: pytest.fail('downloads should be skipped'))
    monkeypatch.setattr(p, 'get_translator', lambda **kw: SimpleNamespace(close=lambda: events.append('translator-closed')))
    monkeypatch.setattr(p, 'translate_cues', lambda cues, *a, **kw: cues)
    p.dub(video, None, config, report=lambda s: None)
    assert 'translator-closed' in events


@pytest.mark.parametrize('mode', ['local', 'download'])
def test_strict_subtitle_modes_do_not_translate(stages, mode):
    video, _, config, _ = stages
    config.subs, config.download = mode, False
    with pytest.raises(JellyDubError, match='No acceptable Vietnamese'):
        p.dub(video, None, config, report=lambda s: None)


def test_subs_output_requires_force(subtitle_sources):
    video, config, vi, _, _ = subtitle_sources
    config.download = False
    config.out_dir.mkdir()
    target = config.out_dir / f'{video.stem}.vi.srt'
    target.write_text('different existing subtitle')
    with pytest.raises(JellyDubError, match='--force'):
        p.subs(video, config, report=lambda s: None)
    assert target.read_text() == 'different existing subtitle'
    assert p.subs(video, config, force=True, report=lambda s: None) == target
    assert target.read_text() == 'translated'
    stamp = target.stat().st_mtime_ns
    p.subs(video, config, report=lambda s: None)
    assert target.stat().st_mtime_ns == stamp
    assert vi.path.read_text() == 'Vietnamese source unchanged'


def test_subs_default_language_and_no_reference(stages, monkeypatch):
    video, srt, config, _ = stages
    candidate = SimpleNamespace(path=srt, origin='sidecar', language='vi', score=1.0, note='matching')
    monkeypatch.setattr(p, 'find_local_subtitles', lambda v, lang: [candidate] if lang == 'vi' else [])
    result = p.subs(video, config, report=lambda s: None)
    assert result.read_bytes() == srt.read_bytes()


def test_credentials_env_only_passed_to_provider_and_errors_redacted(stages, monkeypatch):
    import importlib.util
    video, _, config, _ = stages
    config.subtitle_credentials_env = {'username': 'TEST_SUBS_USER', 'password': 'TEST_SUBS_PASSWORD'}
    monkeypatch.setenv('TEST_SUBS_USER', 'synthetic-user')
    monkeypatch.setenv('TEST_SUBS_PASSWORD', 'synthetic-password')
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name: object())
    calls = []
    def download(*a, **kw):
        calls.append(kw['credentials'])
        raise JellyDubError('synthetic-password must not be logged')
    monkeypatch.setattr(p, 'download_subtitles', download)
    messages = []
    with pytest.raises(JellyDubError, match='No suitable subtitles'):
        p.dub(video, None, config, report=messages.append)
    assert calls == [{'username': 'synthetic-user', 'password': 'synthetic-password'}]
    assert all('synthetic-' not in m for m in messages)


def test_embedded_reference_preferred_over_sidecar(subtitle_sources, monkeypatch, tmp_path):
    video, config, _, en, _ = subtitle_sources
    embedded = SimpleNamespace(path=tmp_path / 'embedded.en.srt', origin='embedded', language='en', score=0.8, note='video stream')
    embedded.path.write_text('embedded')
    monkeypatch.setattr(p, 'extract_embedded_subtitles', lambda v, lang, out: [embedded] if lang == 'en' else [])
    read = []
    monkeypatch.setattr(p, 'parse_srt', lambda path: read.append(path) or [Cue(1, 0, 1, 'reference')])
    p._english_reference(video, p._source_dir(video, config), lambda s: None)
    assert set(read) == {embedded.path, en.path}


def test_explicit_english_has_priority(stages, monkeypatch):
    video, srt, config, events = stages
    config.source_lang = 'en'
    monkeypatch.setattr(p, 'find_local_subtitles', lambda *a: pytest.fail('explicit English is authoritative'))
    monkeypatch.setattr(p, 'get_translator', lambda **kw: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(p, 'translate_cues', lambda cues, *a, **kw: cues)
    p.dub(video, srt, config, report=lambda s: None)


def test_sync_threshold_boundary_is_accepted(subtitle_sources, monkeypatch):
    video, config, _, _, _ = subtitle_sources
    monkeypatch.setattr(p, 'estimate_sync', lambda *a: SimpleNamespace(offset=0.0, scale=1.0, matched_ratio=0.5, residual=0.3))
    assert p.subs(video, config, report=lambda s: None).is_file()


def test_missing_subtitles_clear_error_and_no_translation(stages, monkeypatch):
    video, _, config, _ = stages
    config.download = False
    monkeypatch.setattr(p, 'get_translator', lambda **kw: pytest.fail('no English input'))
    with pytest.raises(JellyDubError, match='Supply --srt'):
        p.dub(video, None, config, report=lambda s: None)


@pytest.mark.parametrize('local_quality,expected_origin', [(0.2, 'opensubtitlescom'), (0.95, 'local')])
def test_final_ranking_compares_local_and_download_quality(subtitle_sources, monkeypatch, tmp_path, local_quality, expected_origin):
    import importlib.util
    video, config, local, en, _ = subtitle_sources
    config.search_online = True  # this test compares local and downloaded rankings
    remote = SimpleNamespace(path=tmp_path / 'remote.vi.srt', origin='opensubtitlescom', language='vi', score=0.9,
                             prescore=0.95, quality=None, issues=[], note='trusted release',
                             metadata={'uploader': {'name': 'PublicAuthor'}, 'from_trusted': True,
                                       'download_count': 1200, 'ratings': 9.0})
    remote.path.write_text('remote')
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setattr(p, 'download_subtitles', lambda *a, **kw: [remote])
    monkeypatch.setattr(p, 'parse_srt', lambda path: [Cue(1, 1, 2, 'local' if path == local.path else 'remote')])
    monkeypatch.setattr(p, 'estimate_sync', lambda *a: SimpleNamespace(offset=0., scale=1., matched_ratio=1., residual=0.))
    monkeypatch.setattr(p, 'assess_subtitle', lambda cues, **kw: SimpleNamespace(
        score=local_quality if cues[0].text == 'local' else 0.9,
        issues=['encoding errors'] if cues[0].text == 'local' and local_quality < 0.5 else [], stats={}))
    messages = []
    chosen, language = p._resolve_subtitle(video, None, config, messages.append)
    assert chosen == (local.path if expected_origin == 'local' else remote.path)
    assert language == 'vi'
    assert any('PublicAuthor/trusted | 1200 | 9.0' in m for m in messages)
    assert any(m.startswith('Rank | Origin') for m in messages)
    assert local.quality == local_quality and remote.quality == 0.9
    if local_quality < 0.5:
        assert any('quality below 0.5' in m and m.startswith('Rejected') for m in messages)


def test_final_score_formula_and_prescore_ranking(stages, monkeypatch, tmp_path):
    _, _, config, _ = stages
    candidates = []
    for name, prescore in [('lower', 0.2), ('higher', 0.9)]:
        path = tmp_path / f'{name}.vi.srt'
        path.write_text(name)
        candidates.append(SimpleNamespace(path=path, origin='opensubtitlescom', language='vi', score=0.8,
                                           prescore=prescore, quality=None, issues=[], metadata={}, note=name))
    monkeypatch.setattr(p, 'assess_subtitle', lambda *a, **kw: SimpleNamespace(score=0.8, issues=[], stats={}))
    monkeypatch.setattr(p, 'estimate_sync', lambda *a: SimpleNamespace(offset=0., scale=1., matched_ratio=0.9, residual=0.03))
    monkeypatch.setattr(p, 'apply_sync', lambda cues, result: cues)
    messages = []
    selected = p._best_candidate(candidates, [Cue(1, 1, 2, 'reference')], tmp_path, messages.append)
    assert selected == candidates[1].path
    # 0.4*0.9 + 0.4*0.8 + 0.2*(0.9*(1-0.03/0.3)) = 0.842
    assert any('final=0.842' in m for m in messages)
    assert messages.index(next(m for m in messages if m.startswith('1 |'))) < messages.index(next(m for m in messages if m.startswith('2 |')))


def test_selected_junk_is_removed_without_changing_source(stages, monkeypatch, tmp_path):
    video, srt, config, _ = stages
    original = srt.read_bytes()
    dialogue = Cue(1, 1, 2, 'Xin chào')
    junk = Cue(2, 3, 4, 'www.subtitle-ad.example')
    monkeypatch.setattr(p, 'parse_srt', lambda path: [dialogue, junk])
    monkeypatch.setattr(p, 'strip_junk', lambda cues: [c for c in cues if c.index != 2])
    writes = []
    def write(cues, path):
        writes.append(cues)
        path.write_text('clean dialogue')
    monkeypatch.setattr(p, 'write_srt', write)
    candidate = SimpleNamespace(path=srt, origin='sidecar', language='vi', score=1., note='exact')
    monkeypatch.setattr(p, 'find_local_subtitles', lambda v, lang: [candidate] if lang == 'vi' else [])
    config.download = False
    output = p.subs(video, config, report=lambda s: None)
    assert writes == [[dialogue]]
    assert output.read_text() == 'clean dialogue'
    assert srt.read_bytes() == original


def test_junk_only_winner_falls_back_to_next_candidate(stages, monkeypatch, tmp_path):
    _, srt, _, _ = stages
    other = tmp_path / 'other.vi.srt'
    other.write_text('other')
    a = SimpleNamespace(path=srt, origin='local', language='vi', score=1., note='ad only')
    b = SimpleNamespace(path=other, origin='local', language='vi', score=0.8, note='dialogue')
    monkeypatch.setattr(p, 'parse_srt', lambda path: [Cue(1, 1, 2, 'junk' if path == srt else 'speech')])
    monkeypatch.setattr(p, 'strip_junk', lambda cues: [] if cues[0].text == 'junk' else cues)
    assert p._best_candidate([a, b], None, tmp_path, lambda s: None) == other


def test_forced_reference_excluded_and_fullest_track_preferred(stages, monkeypatch, tmp_path):
    video, _, config, _ = stages
    tracks = []
    counts = {}
    for name, origin, forced, count in [('forced', 'embedded', True, 500),
                                        ('short', 'embedded', False, 2),
                                        ('full-sdh', 'sidecar', False, 100)]:
        candidate = SimpleNamespace(path=tmp_path / f'{name}.en.srt', origin=origin, score=1.0,
                                    metadata={'forced': forced, 'cue_count': count})
        tracks.append(candidate)
        counts[candidate.path] = count
    monkeypatch.setattr(p, '_local_candidates', lambda *a: tracks)
    read = []
    def parse(path):
        read.append(path)
        return [Cue(i, i, i + 0.5, 'English') for i in range(counts[path])]
    monkeypatch.setattr(p, 'parse_srt', parse)
    messages = []
    reference = p._english_reference(video, tmp_path, messages.append)
    assert len(reference) == 100
    assert reference.alternatives[0][0] == tracks[2].path
    assert tracks[0].path not in read
    assert any('Skipping forced' in line for line in messages)


def test_sparse_reference_tries_next_before_rejecting_candidate(stages, monkeypatch, tmp_path):
    _, path, _, _ = stages
    candidate_cues = [Cue(i, i, i + 0.5, 'Tiếng Việt') for i in range(100)]
    sparse = [Cue(i, i, i + 0.5, 'English') for i in range(2)]
    full = [Cue(i, i, i + 0.5, 'English') for i in range(110)]
    # Defensive guard also works when a caller supplied sparse-first alternatives.
    reference = p._ReferenceCues([(Path('sparse.en.srt'), sparse), (Path('full.en.srt'), full)])
    monkeypatch.setattr(p, 'parse_srt', lambda path: candidate_cues)
    used = []
    def estimate(cues, timing):
        used.append(timing)
        return SimpleNamespace(offset=0., scale=1., matched_ratio=0.95, residual=0.01)
    monkeypatch.setattr(p, 'estimate_sync', estimate)
    monkeypatch.setattr(p, 'apply_sync', lambda cues, result: cues)
    quality_refs = []
    monkeypatch.setattr(p, 'assess_subtitle', lambda cues, **kw: quality_refs.append(kw['reference']) or SimpleNamespace(score=0.95, issues=[]))
    candidate = SimpleNamespace(path=path, origin='local', language='vi', score=1., note='full VI')
    messages = []
    assert p._best_candidate([candidate], reference, tmp_path, messages.append) == path
    assert used == [full] and quality_refs == [full]
    assert any('below 30%' in line for line in messages)


@pytest.mark.parametrize('command', ['dub', 'subs'])
@pytest.mark.parametrize('origin', ['local', 'sidecar', 'embedded'])
def test_good_local_vi_skips_online(subtitle_sources, monkeypatch, command, origin):
    video, config, local, _, _ = subtitle_sources
    local.origin = origin
    monkeypatch.setattr(p, 'estimate_sync', lambda *a: SimpleNamespace(offset=-0.001, scale=1., matched_ratio=0.9, residual=0.1))
    monkeypatch.setattr(p, 'assess_subtitle', lambda *a, **kw: SimpleNamespace(score=0.9, issues=[], stats={}))
    monkeypatch.setattr(p, '_download_candidates', lambda *a: pytest.fail('good local subtitle must prevent online search'))
    messages = []
    if command == 'dub':
        p.dub(video, None, config, report=messages.append)
    else:
        p.subs(video, config, report=messages.append)
    assert 'skipping online search: good local subtitle' in messages


@pytest.mark.parametrize('command', ['dub', 'subs'])
@pytest.mark.parametrize('case', ['forced-search', 'low-quality', 'low-match', 'high-residual', 'no-reference'])
def test_online_search_not_skipped_when_required(subtitle_sources, monkeypatch, command, case):
    video, config, _, _, _ = subtitle_sources
    config.search_online = case == 'forced-search'
    monkeypatch.setattr(p, 'assess_subtitle', lambda *a, **kw: SimpleNamespace(score=0.89 if case == 'low-quality' else 1., issues=[], stats={}))
    monkeypatch.setattr(p, 'estimate_sync', lambda *a: SimpleNamespace(offset=0., scale=1.,
        matched_ratio=0.89 if case == 'low-match' else 1., residual=0.11 if case == 'high-residual' else 0.001))
    if case == 'no-reference':
        monkeypatch.setattr(p, '_english_reference', lambda *a: None)
    calls = []
    monkeypatch.setattr(p, '_download_candidates', lambda *a: calls.append(True) or [])
    messages = []
    if command == 'dub':
        p.dub(video, None, config, report=messages.append)
    else:
        p.subs(video, config, report=messages.append)
    assert calls == [True]
    assert 'skipping online search: good local subtitle' not in messages


def test_search_online_does_not_override_no_download(subtitle_sources, monkeypatch):
    video, config, _, _, _ = subtitle_sources
    config.search_online, config.download = True, False
    monkeypatch.setattr(p, 'download_subtitles', lambda *a, **kw: pytest.fail('no-download remains authoritative'))
    p.subs(video, config, report=lambda s: None)
