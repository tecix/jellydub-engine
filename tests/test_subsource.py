from pathlib import Path
from types import SimpleNamespace

import pytest

from jellydub.models import Cue, JellyDubError
from jellydub.subsource import (download_subtitles, extract_embedded_subtitles,
                               find_local_subtitles, looks_vietnamese)


VI = '1\n00:00:01,000 --> 00:00:02,000\nChúng ta cùng khám phá vũ trụ.\n'
EN = '1\n00:00:01,000 --> 00:00:02,000\nWe explore the universe.\n'


def put(path, text=VI):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def test_vietnamese_threshold_and_unicode():
    import unicodedata
    assert not looks_vietnamese([])
    assert not looks_vietnamese([Cue(1, 0, 1, '')])
    assert looks_vietnamese([Cue(1, 0, 1, unicodedata.normalize('NFD', 'chúng ta'))])
    assert not looks_vietnamese([Cue(i, 0, 1, 'đây' if i < 3 else 'Hi') for i in range(10)])
    assert looks_vietnamese([Cue(i, 0, 1, 'đây' if i < 4 else 'Hi') for i in range(10)])


def test_local_cosmos_sidecars_and_wrong_episodes(tmp_path):
    video = put(tmp_path / 'Cosmos.Possible.Worlds.S02E04.Vavilov[release].mkv', 'video')
    exact = put(video.with_name(video.stem + '.vie.srt'))
    alternate = put(tmp_path / 'Subs' / 'Cosmos Possible Worlds S01E04 Vavilov.vi.srt')
    weak = put(tmp_path / 'subtitle' / 'Cosmos Possible Worlds S01E04.vi.srt')
    put(tmp_path / 'subtitles' / 'Cosmos Possible Worlds S02E05.Other.vi.srt')
    put(tmp_path / 'Elsewhere.S02E04.Vavilov.vi.srt')
    put(tmp_path / 'broken.vi.srt', 'broken')
    found = find_local_subtitles(video, 'vi')
    assert found[0].path == exact and found[0].origin == 'sidecar' and found[0].score == 1
    assert found[1].path == alternate
    assert 'Vavilov' in found[1].note
    assert {c.path for c in found} == {exact, alternate, weak}
    assert video.read_text() == 'video' and alternate.read_text() == VI


def test_local_search_dirs_language_and_fallback(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'guessit', None)
    video = put(tmp_path / 'video' / 'Cosmos.Possible.Worlds.S02E04.Vavilov.mkv', 'video')
    other = tmp_path / 'other'
    vi = put(other / 'subtitles' / 'Cosmos Possible Worlds S01E04 Vavilov.srt')
    en = put(video.with_name(video.stem + '.eng.srt'), EN)
    assert find_local_subtitles(video, 'vie', [other])[0].path == vi
    assert find_local_subtitles(video, 'en', [other])[0].path == en
    # Explicit .en tagged text that is actually Vietnamese must not trigger translation.
    put(en, VI)
    assert find_local_subtitles(video, 'en') == []
    assert find_local_subtitles(video, 'vi')[0].path == en


def test_never_return_video(tmp_path):
    video = put(tmp_path / 'Movie.vi.srt')
    assert find_local_subtitles(video, 'vi') == []


def test_extract_embedded_and_cache(tmp_path, monkeypatch, caplog):
    video = put(tmp_path / 'movie[1].mkv', 'video')
    streams = [
        {'index': 0, 'codec_type': 'video', 'codec_name': 'h264'},
        {'index': 3, 'codec_type': 'subtitle', 'codec_name': 'ass', 'tags': {'language': 'vie'}},
        {'index': 4, 'codec_type': 'subtitle', 'codec_name': 'hdmv_pgs_subtitle', 'tags': {'language': 'vie'}},
        {'index': 5, 'codec_type': 'subtitle', 'codec_name': 'subrip', 'tags': {'language': 'eng'}},
    ]
    monkeypatch.setattr('jellydub.subsource.probe', lambda path: {'streams': streams})
    calls = []
    def ffmpeg(args):
        calls.append(args)
        assert args[:4] == ['-i', str(video), '-map', '0:3']
        put(Path(args[-1]))
    monkeypatch.setattr('jellydub.subsource.run_ffmpeg', ffmpeg)
    found = extract_embedded_subtitles(video, 'vi', tmp_path / 'out')
    assert len(found) == 1 and found[0].origin == 'embedded'
    assert found[0].path.read_text() == VI
    assert 'bitmap/unsupported' in caplog.text
    assert extract_embedded_subtitles(video, 'vi', tmp_path / 'out') == found
    assert len(calls) == 1
    assert video.read_text() == 'video'


def test_extract_failure_isolated(tmp_path, monkeypatch, caplog):
    video = put(tmp_path / 'movie.mkv', 'video')
    monkeypatch.setattr('jellydub.subsource.probe', lambda path: {'streams': [
        {'index': 2, 'codec_type': 'subtitle', 'codec_name': 'subrip', 'tags': {'language': 'vi'}}]})
    def fail(args):
        raise JellyDubError('failed')
    monkeypatch.setattr('jellydub.subsource.run_ffmpeg', fail)
    assert extract_embedded_subtitles(video, 'vi', tmp_path / 'out') == []
    assert 'Could not extract' in caplog.text


@pytest.fixture
def fake_subliminal(monkeypatch):
    import sys
    import types
    calls = SimpleNamespace(queries=[], downloads=[], configs=[], scans=0, fail=False)
    language = 'vi'
    def scan_video(path):
        calls.scans += 1
        return SimpleNamespace(name=path, series='Cosmos: Possible Worlds', season=2, episode=4,
                               imdb_id='original-id', series_imdb_id='series-id')
    class Subtitle:
        provider_name = 'opensubtitlescom'
        language = 'vi'
        movie_kind = 'episode'
        series_title = 'Cosmos: Possible Worlds'
        series_season = 2
        series_episode = 4
        movie_title = 'Vavilov'
        def __init__(self, id, score):
            self.id, self.score, self.text = id, score, VI
    low, high, second = Subtitle('low', 10), Subtitle('high', 90), Subtitle('second', 80)
    class Pool:
        def __init__(self, **kwargs):
            calls.configs.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def list_subtitles(self, video, languages):
            import logging
            logging.warning('secret-user secret-password secret-key')
            calls.queries.append((video.series, video.season, video.imdb_id))
            if calls.fail:
                raise RuntimeError('secret-password secret-key')
            return [high, second, high] if video.series == 'Cosmos' and video.season == 1 else [low]
        def download_subtitle(self, subtitle):
            calls.downloads.append(subtitle.id)
            return True
    module = types.ModuleType('subliminal')
    calls.cache_configs = []
    class Region:
        is_configured = False
        def configure(self, backend):
            calls.cache_configs.append(backend)
            self.is_configured = True
    module.region = Region()
    module.scan_video, module.ProviderPool = scan_video, Pool
    module.compute_score = lambda subtitle, video: subtitle.score
    score = types.ModuleType('subliminal.score')
    score.get_scores = lambda video: {'hash': 100}
    babelfish = types.ModuleType('babelfish')
    babelfish.Language = SimpleNamespace(fromalpha2=lambda lang: lang)
    monkeypatch.setitem(sys.modules, 'subliminal', module)
    monkeypatch.setitem(sys.modules, 'subliminal.score', score)
    monkeypatch.setitem(sys.modules, 'babelfish', babelfish)
    monkeypatch.setattr('jellydub.subsource._metadata_pool_class', lambda: Pool)
    calls.subtitles = [low, high, second]
    return calls


def test_download_alternates_ranking_quota_cache(tmp_path, fake_subliminal, monkeypatch, caplog):
    video = put(tmp_path / 'Cosmos.Possible.Worlds.S02E04.Vavilov[release].mkv', 'video')
    monkeypatch.setenv('OPENSUBTITLES_USERNAME', 'secret-user')
    monkeypatch.setenv('OPENSUBTITLES_PASSWORD', 'secret-password')
    monkeypatch.setenv('OPENSUBTITLES_APIKEY', 'secret-key')
    before = __import__('logging').root.manager.disable
    found = download_subtitles(video, 'vi', tmp_path / 'out')
    assert [c.score for c in found] == [.9, .8]
    assert fake_subliminal.downloads == ['high', 'second']
    assert any(series == 'Cosmos' and season == 1 and imdb is None for series, season, imdb in fake_subliminal.queries)
    assert all(c.path.read_text().startswith('1\n') for c in found)
    assert fake_subliminal.configs[0]['provider_configs']['opensubtitlescom'] == {
        'username': 'secret-user', 'password': 'secret-password', 'apikey': 'secret-key'}
    assert download_subtitles(video, 'vi', tmp_path / 'out') == found
    assert fake_subliminal.scans == 1
    assert __import__('logging').root.manager.disable == before
    assert not any(secret in caplog.text for secret in ['secret-user', 'secret-password', 'secret-key'])
    assert all('secret-' not in p.read_text() for p in (tmp_path / 'out').iterdir())


def test_download_failure_and_credentials_override(tmp_path, fake_subliminal, caplog):
    video = put(tmp_path / 'movie.mkv', 'video')
    fake_subliminal.fail = True
    assert download_subtitles(video, 'vi', tmp_path / 'out', credentials={'username': 'explicit-user'}) == []
    assert fake_subliminal.configs[0]['provider_configs']['opensubtitlescom']['username'] == 'explicit-user'
    assert 'secret-password' not in caplog.text
    assert 'No downloadable subtitles' in caplog.text
    assert fake_subliminal.downloads == []


def test_download_missing_extra_and_zero_limit(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'subliminal', None)
    video = put(tmp_path / 'movie.mkv', 'video')
    assert download_subtitles(video, 'vi', tmp_path / 'out', max_downloads=0) == []
    with pytest.raises(JellyDubError, match='opensubtitles extra'):
        download_subtitles(video, 'vi', tmp_path / 'out')


def test_local_untagged_english(tmp_path):
    video = put(tmp_path / 'Movie.mkv', 'video')
    english = put(tmp_path / 'Movie.srt', EN)
    found = find_local_subtitles(video, 'en')
    assert len(found) == 1 and found[0].path == english


def test_download_failed_attempts_still_count_towards_quota(tmp_path, fake_subliminal, monkeypatch):
    import sys
    pool = sys.modules['subliminal'].ProviderPool
    def fail(self, subtitle):
        fake_subliminal.downloads.append(subtitle.id)
        raise RuntimeError('secret-password')
    monkeypatch.setattr(pool, 'download_subtitle', fail)
    video = put(tmp_path / 'Cosmos.Possible.Worlds.S02E04.Vavilov.mkv', 'video')
    assert download_subtitles(video, 'vi', tmp_path / 'out', max_downloads=2) == []
    assert fake_subliminal.downloads == ['high', 'second']


def test_corrupt_download_cache_is_regenerated(tmp_path, fake_subliminal):
    video = put(tmp_path / 'Cosmos.Possible.Worlds.S02E04.Vavilov.mkv', 'video')
    found = download_subtitles(video, 'vi', tmp_path / 'out')
    found[0].path.write_text('broken')
    regenerated = download_subtitles(video, 'vi', tmp_path / 'out')
    assert regenerated == found and regenerated[0].path.read_text().startswith('1\n')
    assert fake_subliminal.scans == 2


def test_candidate_contract_default_containers():
    from jellydub.subsource import SubtitleCandidate
    first = SubtitleCandidate(Path('one.srt'), 'local', 'vi', .9, 'old positional constructor')
    second = SubtitleCandidate(Path('two.srt'), 'local', 'vi', .8)
    first.metadata['test'] = True
    first.issues.append('review')
    assert second.metadata == {} and second.issues == []
    assert second.prescore == 0 and second.quality is None


def test_prescore_weights_penalties_and_rating_prior():
    from jellydub.subsource import _prescore
    video = Path('Movie.2024.1080p.WEB-DL-GROUP.mkv')
    exact, note = _prescore({'moviehash_match': True, 'fps': 24}, .9, video, 24)
    popular, _ = _prescore({'download_count': 1000000, 'ratings': 10, 'votes': 1000}, .1, video)
    assert exact > popular and 'movie hash match' in note and 'matching fps' in note
    base, _ = _prescore({}, .8, video)
    trusted, note = _prescore({'from_trusted': '1'}, .8, video)
    rank, _ = _prescore({'uploader': {'rank': 'Gold member', 'name': 'Alice'}}, .8, video)
    assert trusted > base and rank == trusted and 'trusted uploader' in note
    one_vote, _ = _prescore({'ratings': 10, 'votes': 1}, .8, video)
    many_votes, _ = _prescore({'ratings': 10, 'votes': 100}, .8, video)
    assert base < one_vote < many_votes
    sdh, _ = _prescore({'hearing_impaired': True}, .8, video)
    assert sdh == pytest.approx(base - .03)
    for flag in ('machine_translated', 'ai_translated'):
        assert _prescore({flag: 'true', 'moviehash_match': True}, 1, video)[0] == 0
    similar, _ = _prescore({'release': video.stem, 'fps': 24}, .8, video, 24)
    unrelated, _ = _prescore({'release': 'Other.480p.DVD-DIFF', 'fps': 25}, .8, video, 24)
    assert similar > unrelated
    assert 0 <= _prescore({'ratings': 'nan', 'download_count': 'broken', 'fps': None}, .8, video)[0] <= 1


def test_metadata_ranking_before_download_and_cache(tmp_path, fake_subliminal):
    video = put(tmp_path / 'Cosmos.Possible.Worlds.S02E04.Vavilov.mkv', 'video')
    low, high, second = fake_subliminal.subtitles
    # The lowest old provider match wins due to exact match and trusted uploader.
    low.raw_attributes = {'moviehash_match': True, 'from_trusted': True,
                          'uploader': {'name': 'Alice', 'rank': 'translator'},
                          'ratings': 9, 'votes': 100, 'download_count': 2000,
                          'new_download_count': 10, 'upload_date': '2026-01-01',
                          'password': 'secret-do-not-persist'}
    high.raw_attributes = {'ai_translated': True, 'download_count': 1000000}
    second.raw_attributes = {'hearing_impaired': True}
    found = download_subtitles(video, 'vi', tmp_path / 'out', max_downloads=2)
    assert fake_subliminal.downloads == ['low', 'second']
    assert found[0].prescore > found[1].prescore
    assert found[0].metadata['uploader'] == {'name': 'Alice', 'rank': 'translator'}
    assert found[0].metadata['new_download_count'] == 10
    assert found[0].quality is None and found[0].issues == []
    assert 'trusted uploader' in found[0].note
    assert download_subtitles(video, 'vi', tmp_path / 'out') == found
    assert fake_subliminal.scans == 1
    assert all('secret-do-not-persist' not in p.read_text() for p in (tmp_path / 'out').iterdir())


def test_opensubtitles_subclasses_keep_raw_attributes():
    pytest.importorskip('subliminal.providers.opensubtitlescom')
    from subliminal import ProviderPool
    from subliminal.providers.opensubtitlescom import OpenSubtitlesComProvider, OpenSubtitlesComSubtitle
    from jellydub.subsource import _metadata_pool_class, _provider_facts
    pool_type = _metadata_pool_class()
    assert issubclass(pool_type, ProviderPool)
    pool = pool_type(providers=['opensubtitlescom'])
    provider = pool['opensubtitlescom']  # Creates a local Session only, no network request.
    try:
        assert isinstance(provider, OpenSubtitlesComProvider)
        assert type(provider) is not OpenSubtitlesComProvider
        assert provider.subtitle_class is not OpenSubtitlesComSubtitle
        assert OpenSubtitlesComProvider.subtitle_class is OpenSubtitlesComSubtitle
        response = {'id': '123', 'attributes': {
            'language': 'vi', 'hearing_impaired': 0, 'foreign_parts_only': 0,
            'release': 'Cosmos.1080p.WEB-DL-GROUP', 'moviehash_match': True,
            'download_count': 300, 'new_download_count': 4, 'machine_translated': 0,
            'ai_translated': False, 'fps': 23.976, 'from_trusted': True,
            'uploader': {'rank': 'trusted', 'name': 'Alice'}, 'ratings': 9.5, 'votes': 20,
            'upload_date': '2026-01-01', 'feature_details': {'feature_type': 'Episode'},
            'files': [{'file_id': 123, 'file_name': 'subtitle.srt'}],
        }}
        subtitle = provider.subtitle_class.from_response(response)
        assert subtitle.raw_attributes == response['attributes']
        response['attributes']['uploader']['name'] = 'Changed'
        facts = _provider_facts(subtitle)
        assert facts['uploader']['name'] == 'Alice'
        assert facts['from_trusted'] is True and facts['new_download_count'] == 4
        assert facts['ratings'] == 9.5 and facts['upload_date'] == '2026-01-01'
    finally:
        provider.terminate()


def test_query_variants_do_not_mutate_original_external_ids():
    pytest.importorskip('subliminal')
    from subliminal.video import Episode
    from jellydub.subsource import _queries
    video = Episode('Cosmos.Possible.Worlds.S02E04.mkv', 'Cosmos: Possible Worlds', 2, 4)
    video.imdb_id = 'tt123'
    video.series_imdb_id = 'tt456'
    before = dict(video.external_ids)
    queries = _queries(video)
    assert video.external_ids == before
    assert queries[0] is video
    assert all(q.external_ids is not video.external_ids for q in queries[1:])
    assert all(q.imdb_id is None for q in queries[1:])


def test_cosmos_forced_english_is_not_sync_reference(tmp_path, monkeypatch, caplog):
    import logging
    video = put(tmp_path / 'Cosmos.Possible.Worlds.S02E05.mkv', 'video')
    streams = [
        {'index': 2, 'codec_type': 'subtitle', 'codec_name': 'subrip',
         'tags': {'language': 'eng', 'title': 'Forced'},
         'disposition': {'forced': 1, 'hearing_impaired': 0, 'default': 1}},
        {'index': 3, 'codec_type': 'subtitle', 'codec_name': 'subrip',
         'tags': {'language': 'eng', 'title': 'SDH'},
         'disposition': {'forced': 0, 'hearing_impaired': 1, 'default': 0}},
    ]
    monkeypatch.setattr('jellydub.subsource.probe', lambda path: {'streams': streams})
    full = EN + '\n2\n00:00:04,000 --> 00:00:05,000\nThe full dialogue continues.\n'
    calls = []
    def ffmpeg(args):
        calls.append(args)
        assert args[3] == '0:3'
        put(Path(args[-1]), full)
    monkeypatch.setattr('jellydub.subsource.run_ffmpeg', ffmpeg)
    with caplog.at_level(logging.INFO):
        found = extract_embedded_subtitles(video, 'en', tmp_path / 'out')
    assert len(found) == 1
    assert found[0].metadata == {'stream_index': 3, 'codec': 'subrip', 'title': 'SDH',
                                 'disposition': {'forced': 0, 'hearing_impaired': 1, 'default': 0},
                                 'cue_count': 2}
    assert 'SDH' in found[0].note and '2 cues' in found[0].note
    assert 'forced=0' in found[0].note and 'hearing_impaired=1' in found[0].note
    assert 'Skipping forced subtitle stream 2' in caplog.text
    assert extract_embedded_subtitles(video, 'en', tmp_path / 'out') == found
    assert len(calls) == 1


def test_title_only_forced_stream_skipped(tmp_path, monkeypatch):
    video = put(tmp_path / 'movie.mkv', 'video')
    monkeypatch.setattr('jellydub.subsource.probe', lambda path: {'streams': [
        {'index': 2, 'codec_type': 'subtitle', 'codec_name': 'subrip',
         'tags': {'language': 'eng', 'title': 'English (Forced)'}}]})
    monkeypatch.setattr('jellydub.subsource.run_ffmpeg', lambda args: pytest.fail('forced stream extracted'))
    assert extract_embedded_subtitles(video, 'en', tmp_path / 'out') == []


def test_full_embedded_streams_rank_by_coverage_then_default(tmp_path, monkeypatch):
    video = put(tmp_path / 'movie.mkv', 'video')
    monkeypatch.setattr('jellydub.subsource.probe', lambda path: {'streams': [
        {'index': i, 'codec_type': 'subtitle', 'codec_name': 'subrip',
         'tags': {'language': 'eng', 'title': 'SDH' if i == 4 else 'English'},
         'disposition': {'default': int(i == 3), 'hearing_impaired': int(i == 4)}} for i in (2, 4, 3)]})
    def ffmpeg(args):
        content = EN
        if args[3] != '0:2':
            content += '\n2\n00:00:04,000 --> 00:00:05,000\nMore speech.\n'
        put(Path(args[-1]), content)
    monkeypatch.setattr('jellydub.subsource.run_ffmpeg', ffmpeg)
    found = extract_embedded_subtitles(video, 'en', tmp_path / 'out')
    assert [c.metadata['stream_index'] for c in found] == [3, 4, 2]
    assert found[0].score > found[1].score


def test_subliminal_cache_configured_once_and_preserves_existing(tmp_path, fake_subliminal):
    import sys
    from jellydub.subsource import _configure_subliminal_cache
    region = sys.modules['subliminal'].region
    assert not region.is_configured
    _configure_subliminal_cache()
    _configure_subliminal_cache()
    assert fake_subliminal.cache_configs == ['dogpile.cache.memory']
    fake_subliminal.cache_configs.clear()
    # A caller's already-configured backend must never be replaced.
    region.is_configured = True
    _configure_subliminal_cache()
    assert fake_subliminal.cache_configs == []


def test_download_initializes_region_before_provider(tmp_path, fake_subliminal, monkeypatch):
    import sys
    module = sys.modules['subliminal']
    original = module.ProviderPool.download_subtitle
    def download(self, subtitle):
        assert module.region.is_configured
        return original(self, subtitle)
    monkeypatch.setattr(module.ProviderPool, 'download_subtitle', download)
    video = put(tmp_path / 'movie.mkv', 'video')
    assert download_subtitles(video, 'vi', tmp_path / 'out')
    assert fake_subliminal.cache_configs == ['dogpile.cache.memory']


@pytest.mark.parametrize(('kind', 'expected'), [
    ('auth', 'OpenSubtitles.com downloads need a free account: set OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD'),
    ('quota', 'daily download quota reached'),
    ('429', 'daily download quota reached'),
    ('network', 'network error; check connectivity and retry'),
    ('unavailable', 'provider unavailable; try another provider or retry later'),
])
def test_download_specific_failures_and_provider_fallback(tmp_path, fake_subliminal, monkeypatch, caplog, kind, expected):
    import sys
    module = sys.modules['subliminal']
    pool = module.ProviderPool
    # Mirror the real Interstellar search: eight unique VI matches on OpenSubtitles.
    sample = type(fake_subliminal.subtitles[0])
    os_subs = [sample(str(i), 95 - i) for i in range(8)]
    alternative = sample('fallback', 50)
    alternative.provider_name = 'podnapisi'
    def search(self, video, languages):
        return os_subs + [alternative]
    class AuthenticationError(Exception):
        pass
    class DownloadLimitReached(Exception):
        pass
    class ProviderError(Exception):
        pass
    def download(self, subtitle):
        fake_subliminal.downloads.append((subtitle.provider_name, subtitle.id))
        if subtitle.provider_name == 'opensubtitlescom':
            message = 'secret-user secret-password secret-key'
            if kind == 'auth':
                raise AuthenticationError('Cannot authenticate with username and password ' + message)
            if kind == 'quota':
                raise DownloadLimitReached(message)
            if kind == '429':
                from urllib.error import HTTPError
                raise HTTPError('https://example.invalid/secret-key', 429, message, {}, None)
            if kind == 'network':
                raise TimeoutError(message)
            raise ProviderError(message)
        return True
    monkeypatch.setattr(pool, 'list_subtitles', search)
    monkeypatch.setattr(pool, 'download_subtitle', download)
    video = put(tmp_path / 'Interstellar.2014.mkv', 'video')
    found = download_subtitles(video, 'vi', tmp_path / 'out', max_downloads=1)
    assert len(found) == 1 and found[0].origin == 'podnapisi'
    assert fake_subliminal.downloads == [('opensubtitlescom', '0'), ('podnapisi', 'fallback')]
    assert '8 VI subtitles found on opensubtitlescom but not downloadable' in caplog.text
    assert expected in caplog.text
    if kind == 'auth':
        assert 'without login' in caplog.text
    assert all(secret not in caplog.text for secret in ('secret-user', 'secret-password', 'secret-key'))


def test_auth_only_search_summary_preserves_result_count(tmp_path, fake_subliminal, monkeypatch, caplog):
    import sys
    pool = sys.modules['subliminal'].ProviderPool
    sample = type(fake_subliminal.subtitles[0])
    subs = [sample(str(i), 80) for i in range(8)]
    for i, subtitle in enumerate(subs):
        subtitle.download_count = 100 * (i + 1)
    monkeypatch.setattr(pool, 'list_subtitles', lambda *args: subs)
    class AuthenticationError(Exception):
        pass
    def fail(*args):
        raise AuthenticationError('Cannot authenticate with username and password: secret-password')
    monkeypatch.setattr(pool, 'download_subtitle', fail)
    video = put(tmp_path / 'Interstellar.2014.mkv', 'video')
    assert download_subtitles(video, 'vi', tmp_path / 'out') == []
    assert '8 VI subtitles found on opensubtitlescom but not downloadable without login' in caplog.text
    assert 'highest download count: 800' in caplog.text
    assert 'OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD' in caplog.text
    assert 'secret-password' not in caplog.text


def test_real_pool_propagates_download_failure_without_network(monkeypatch):
    pytest.importorskip('subliminal')
    from jellydub.subsource import _metadata_pool_class
    from subliminal.exceptions import AuthenticationError
    pool_type = _metadata_pool_class()
    class Provider:
        def download_subtitle(self, subtitle):
            raise AuthenticationError('Cannot authenticate with username and password')
    monkeypatch.setattr(pool_type, '__getitem__', lambda self, name: Provider())
    pool = pool_type(providers=['opensubtitlescom'])
    with pytest.raises(AuthenticationError):
        pool.download_subtitle(SimpleNamespace(provider_name='opensubtitlescom'))


def test_real_pool_search_failure_classified_and_other_provider_continues(monkeypatch):
    pytest.importorskip('subliminal')
    from jellydub.subsource import _metadata_pool_class
    from subliminal.exceptions import ProviderError
    from subliminal.video import Movie
    from babelfish import Language
    pool_type = _metadata_pool_class()
    class Failed:
        def list_subtitles(self, *args):
            raise ProviderError('secret-password')
    class Working:
        def list_subtitles(self, *args):
            return ['result']
    monkeypatch.setattr(pool_type, '__getitem__', lambda self, name: Failed() if name == 'opensubtitlescom' else Working())
    pool = pool_type(providers=['opensubtitlescom', 'podnapisi'])
    assert pool.list_subtitles(Movie('movie.mkv', 'movie'), {Language('vie')}) == ['result']
    assert pool.provider_failures == {'opensubtitlescom': 'unavailable'}


def test_movie_broad_search_filtered_before_count_and_download(tmp_path, fake_subliminal, monkeypatch, caplog):
    import sys
    module = sys.modules['subliminal']
    pool = module.ProviderPool
    video = put(tmp_path / 'My.Octopus.Teacher.2020.1080p.NF.WEB-DL.mkv', 'video')
    monkeypatch.setattr(module, 'scan_video', lambda path: SimpleNamespace(
        name=path, title='My Octopus Teacher', year=2020, imdb_id=None, tmdb_id=None))
    sample = type(fake_subliminal.subtitles[0])
    def movie(id, title, year, imdb=None, tmdb=None, score=80):
        sub = sample(id, score)
        sub.movie_kind, sub.movie_title, sub.movie_year = 'movie', title, year
        sub.movie_imdb_id, sub.movie_tmdb_id = imdb, tmdb
        sub.download_count = 100
        return sub
    # 643 provider hits, including unrelated broad-query matches with high scores.
    candidates = [movie(str(i), f'Other Movie {i}', 2020, score=100) for i in range(639)]
    candidates += [movie('exact', 'My Octopus Teacher', 2020, 'tt12888462', '682110', 95),
                   movie('punctuation', 'MY OCTOPUS: TEACHER', 2021, score=90),
                   movie('localized', 'Người thầy bạch tuộc', None, '12888462', score=85),
                   movie('remake', 'My Octopus Teacher', 2010, 'tt99999', score=100)]
    assert len(candidates) == 643
    monkeypatch.setattr(pool, 'list_subtitles', lambda *args: candidates)
    class AuthenticationError(Exception):
        pass
    attempted = []
    def download(self, subtitle):
        attempted.append(subtitle.id)
        raise AuthenticationError('Cannot authenticate with username and password: secret-key')
    monkeypatch.setattr(pool, 'download_subtitle', download)
    assert download_subtitles(video, 'vi', tmp_path / 'out') == []
    assert attempted == ['exact']
    assert '3 VI subtitles found on opensubtitlescom' in caplog.text
    assert '643 VI subtitles' not in caplog.text and 'secret-key' not in caplog.text


def test_movie_known_ids_and_strict_year_filter():
    from jellydub.subsource import _same_work
    target = {'kind': 'movie', 'title': 'My Octopus Teacher', 'year': 2020}
    ids = {'imdb_id': '12888462', 'tmdb_id': '682110'}
    def movie(title, year, **kwargs):
        return dict(kind='movie', title=title, year=year, **kwargs)
    assert _same_work(movie('My.Octopus.Teacher', 2019), target, {})
    assert _same_work(movie('My Octopus Teacher', 2021), target, {})
    assert not _same_work(movie('My Octopus Teacher', 2018), target, {})
    assert not _same_work(movie('My Octopus Teacher', None), target, {})
    assert not _same_work(movie('My Octopus', 2020), target, {})
    assert not _same_work(movie('Another Teacher', 2020), target, {})
    assert _same_work(movie('Localized title', None, imdb_id='12888462'), target, ids)
    assert _same_work(movie('Localized title', None, tmdb_id='682110'), target, ids)
    assert not _same_work(movie('Other film', 2020, imdb_id='99999'), target, ids)
    assert not _same_work(dict(kind='episode', title='My Octopus Teacher', year=2020), target, ids)


def test_movie_anchor_uses_best_exact_title_year(tmp_path, fake_subliminal, monkeypatch):
    import sys
    module = sys.modules['subliminal']
    monkeypatch.setattr(module, 'scan_video', lambda path: SimpleNamespace(
        name=path, title='Movie', year=2020, imdb_id=None, tmdb_id=None))
    sample = type(fake_subliminal.subtitles[0])
    def movie(id, title, year, imdb, score):
        subtitle = sample(id, score)
        subtitle.movie_kind, subtitle.movie_title, subtitle.movie_year = 'movie', title, year
        subtitle.movie_imdb_id = imdb
        return subtitle
    subs = [movie('other', 'Unrelated', 2020, 'tt999', 100),
            movie('near-year', 'Movie', 2021, 'tt888', 99),
            movie('exact', 'Movie', 2020, 'tt123', 90),
            movie('localized', 'Tên khác', None, '123', 89),
            movie('wrong-anchor', 'Bad title', None, '888', 98)]
    monkeypatch.setattr(module.ProviderPool, 'list_subtitles', lambda *args: subs)
    video = put(tmp_path / 'Movie.2020.mkv', 'video')
    found = download_subtitles(video, 'vi', tmp_path / 'out', max_downloads=5)
    assert set(fake_subliminal.downloads) == {'near-year', 'exact', 'localized'}
    assert len(found) == 3
    assert all(c.metadata['work_filter_version'] == 1 for c in found)


def test_episode_filter_series_numbering_and_cosmos_titles():
    from jellydub.subsource import _same_work
    target = dict(kind='episode', series='Cosmos: Possible Worlds', season=2, episode=4, title='Vavilov')
    def ep(series='Cosmos', season=2, episode=4, title='Vavilov'):
        return dict(kind='episode', series=series, season=season, episode=episode, title=title)
    assert _same_work(ep(), target, {})
    assert _same_work(ep(season=1), target, {})
    assert not _same_work(ep(season=1, title='Heaven and Hell'), target, {})
    assert not _same_work(ep(season=1, title=None), target, {})
    assert not _same_work(ep(series='Other show'), target, {})
    assert not _same_work(ep(episode=5), target, {})
    assert not _same_work(ep(season=3), target, {})
    other = dict(kind='episode', series='Other Show', season=2, episode=4, title='Episode')
    assert not _same_work(ep(series='Other Show', season=1, title='Episode'), other, {})


def test_episode_download_filter_counts_after_alternate_queries(tmp_path, fake_subliminal, monkeypatch, caplog):
    import sys
    module = sys.modules['subliminal']
    sample = type(fake_subliminal.subtitles[0])
    good, alternate, wrong_ep, wrong_series, old_cosmos = [sample(name, 90) for name in
                                                         ('good', 'alternate', 'wrong-ep', 'wrong-series', 'old-cosmos')]
    alternate.series_title, alternate.series_season = 'Cosmos', 1
    wrong_ep.series_episode = 5
    wrong_series.series_title = 'Other Series'
    old_cosmos.series_title, old_cosmos.series_season, old_cosmos.movie_title = 'Cosmos', 1, 'Heaven and Hell'
    monkeypatch.setattr(module.ProviderPool, 'list_subtitles', lambda *args: [good, alternate, wrong_ep, wrong_series, old_cosmos])
    video = put(tmp_path / 'Cosmos.Possible.Worlds.S02E04.Vavilov.mkv', 'video')
    found = download_subtitles(video, 'vi', tmp_path / 'out', max_downloads=5)
    assert set(fake_subliminal.downloads) == {'good', 'alternate'}
    assert len(found) == 2


def test_legacy_unfiltered_cache_not_reused(tmp_path, fake_subliminal):
    import json
    video = put(tmp_path / 'Cosmos.Possible.Worlds.S02E04.Vavilov.mkv', 'video')
    first = download_subtitles(video, 'vi', tmp_path / 'out')
    manifest = next((tmp_path / 'out').glob('*.json'))
    entries = json.loads(manifest.read_text())
    for entry in entries:
        entry['metadata'].pop('work_filter_version')
    manifest.write_text(json.dumps(entries))
    assert download_subtitles(video, 'vi', tmp_path / 'out') == first
    assert fake_subliminal.scans == 2
