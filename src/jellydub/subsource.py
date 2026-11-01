"""Discover existing subtitles without changing the source media or sidecars."""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .cache import atomic_write_text, cache_key, tmp_path_for
from .ffmpeg import probe, run_ffmpeg
from .models import Cue, JellyDubError
from .subtitles import parse_srt, write_srt


@dataclass
class SubtitleCandidate:
    path: Path
    origin: str
    language: str
    score: float
    note: str = ""
    metadata: dict = field(default_factory=dict)
    prescore: float = 0.0
    quality: float | None = None
    issues: list[str] = field(default_factory=list)


_LANGS = {"vi": {"vi", "vie", "vietnamese"}, "en": {"en", "eng", "english"}}
_VI = re.compile(r"[ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ]", re.I)


def looks_vietnamese(cues: list[Cue]) -> bool:
    """Vietnamese diacritics in strictly more than 30% of nonempty cues."""
    texts = [unicodedata.normalize("NFC", c.text) for c in cues if c.text.strip()]
    return bool(texts) and sum(bool(_VI.search(t)) for t in texts) / len(texts) > .3


def _language(lang: str) -> str:
    value = lang.lower()
    return next((code for code, aliases in _LANGS.items() if value in aliases), value)


def _filename_language(path: Path) -> str | None:
    for token in re.split(r"[.\s_\-\[\]()]+", path.stem.lower()):
        for code, aliases in _LANGS.items():
            if token in aliases:
                return code
    return None


def _words(text: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", text.casefold())) - {"the", "a", "an"}


def _metadata(path: Path) -> dict:
    name = re.sub(r"(?i)(?:[. _-](?:vi|vie|en|eng|vietnamese|english))+(?=\.srt$)", "", path.name)
    try:
        from guessit import guessit
        data = dict(guessit(name))
    except ImportError:
        data = {}
    # Regex also gives useful titles when optional guessit is absent.
    match = re.search(r"(?i)s(\d{1,2})e(\d{1,3})", name)
    if match:
        data.setdefault("season", int(match[1]))
        data.setdefault("episode", int(match[2]))
        data.setdefault("title", re.sub(r"[._]+", " ", name[:match.start()]).strip())
        tail = re.split(r"(?i)[. _-](?:\d{3,4}p|web|bluray|hdtv|dsnp|h[ .]?26[45])", Path(name).stem[match.end():])[0]
        data.setdefault("episode_title", re.sub(r"[._]+", " ", tail).strip(" -"))
    data.setdefault("title", re.sub(r"[._]+", " ", Path(name).stem))
    return data


def _overlap(a: str, b: str) -> float:
    first, second = _words(a), _words(b)
    return len(first & second) / max(1, len(first | second))


def _match_name(video: dict, subtitle: dict) -> tuple[float, str]:
    series = _overlap(str(video.get("title", "")), str(subtitle.get("title", "")))
    title = _overlap(str(video.get("episode_title", "")), str(subtitle.get("episode_title", "")))
    episode = video.get("episode") is not None and video.get("episode") == subtitle.get("episode")
    season = video.get("season") == subtitle.get("season")
    if title >= .5 and series > 0:
        return min(.98, .65 + .2 * title + .1 * series + .03 * episode), f"episode title match '{video.get('episode_title')}'"
    if episode and series >= .3:
        return (.85 if season else .65) + .05 * series, ("same season/episode" if season else "same episode, alternate season")
    if video.get("episode") is not None:
        # A shared series alone must not select a different episode.
        return 0, "different or unknown episode"
    return .8 * series, "movie title overlap"


def find_local_subtitles(video: Path, lang: str, search_dirs: list[Path] | None = None) -> list[SubtitleCandidate]:
    video, lang = Path(video), _language(lang)
    aliases = _LANGS.get(lang, {lang})
    roots = [video.parent, *(Path(p) for p in (search_dirs or []))]
    directories = [p for root in roots for p in (root, root / 'subtitle', root / 'subtitles', root / 'Subs')]
    files = set()
    for directory in directories:
        try:
            files.update(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == '.srt')
        except OSError:
            continue
    metadata = _metadata(video)
    candidates = []
    for path in sorted(files):
        if path.resolve() == video.resolve():
            continue
        try:
            cues = parse_srt(path)
        except JellyDubError:
            continue
        tagged = _filename_language(path)
        detected = 'vi' if looks_vietnamese(cues) else tagged
        if detected is None:
            english = {'the', 'a', 'an', 'we', 'you', 'i', 'is', 'are', 'this', 'that', 'and', 'of', 'to'}
            if sum(bool(_words(c.text) & english) for c in cues) / len(cues) > .3:
                detected = 'en'
        if detected != lang:
            continue
        sidecar = path.parent.resolve() == video.parent.resolve() and any(
            path.name.casefold() == f'{video.stem}.{alias}.srt'.casefold() for alias in aliases)
        if sidecar:
            score, note = 1., 'exact language sidecar'
        else:
            score, note = _match_name(metadata, _metadata(path))
        if score > 0:
            candidates.append(SubtitleCandidate(path, 'sidecar' if sidecar else 'local', lang, score, note, prescore=1.0))
    return sorted(candidates, key=lambda c: (-c.score, str(c.path)))


def _media_id(video: Path) -> str:
    stat = video.stat()
    return cache_key(str(video.resolve()), stat.st_size, stat.st_mtime_ns)


def extract_embedded_subtitles(video: Path, lang: str, out_dir: Path) -> list[SubtitleCandidate]:
    video, out_dir, lang = Path(video), Path(out_dir), _language(lang)
    result = []
    identity = _media_id(video)
    for stream in probe(video).get('streams', []):
        if stream.get('codec_type') != 'subtitle' or _language(stream.get('tags', {}).get('language', 'und')) != lang:
            continue
        index, codec = stream['index'], stream.get('codec_name')
        title = str(stream.get('tags', {}).get('title', ''))
        disposition = {key: int(_flag((stream.get('disposition') or {}).get(key, 0)))
                       for key in ('forced', 'hearing_impaired', 'default')}
        if disposition['forced'] or re.search(r'\bforced\b', title, re.I):
            logging.info('Skipping forced subtitle stream %s for full-dialogue use', index)
            continue
        if codec not in {'subrip', 'srt', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}:
            logging.warning('Skipping bitmap/unsupported subtitle stream %s (%s); OCR is unavailable', index, codec)
            continue
        path = out_dir / f'{identity}.stream-{index}.{lang}.srt'
        try:
            valid_cache = False
            if path.exists():
                try:
                    cues = parse_srt(path)
                    valid_cache = bool(cues)
                except JellyDubError:
                    pass
            if not valid_cache:
                out_dir.mkdir(parents=True, exist_ok=True)
                temporary = tmp_path_for(path)
                try:
                    run_ffmpeg(['-i', str(video), '-map', f'0:{index}', '-c:s', 'srt', str(temporary)])
                    cues = parse_srt(temporary)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            metadata = {'stream_index': index, 'codec': codec, 'title': title,
                        'disposition': disposition, 'cue_count': len(cues)}
            note = (f'embedded full-dialogue stream {index} ({codec}); title={title or "untitled"}; '
                    f'forced=0, hearing_impaired={disposition["hearing_impaired"]}, '
                    f'default={disposition["default"]}; {len(cues)} cues')
            result.append(SubtitleCandidate(path, 'embedded', lang,
                                            .99 if disposition['default'] else .98,
                                            note, metadata, prescore=1.0))
        except (JellyDubError, OSError):
            logging.warning('Could not extract embedded subtitle stream %s', index)
    return sorted(result, key=lambda c: (-c.metadata['cue_count'],
                                        -c.metadata['disposition']['default'],
                                        c.metadata['stream_index']))


@contextmanager
def _quiet_provider_logs():
    # Providers may log HTTP response bodies or login exceptions containing credentials.
    # Disable third-party logging only during synchronous provider operations, restoring state.
    previous = logging.root.manager.disable
    logging.disable(max(previous, logging.CRITICAL))
    try:
        yield
    finally:
        logging.disable(previous)


def _queries(video):
    result = [video]
    series = getattr(video, 'series', None)
    if not series:
        return result
    names = [series]
    guessed = _metadata(Path(video.name)).get('title')
    if guessed:
        names.append(str(guessed))
    for name in list(names):
        names.append(re.split(r'\s*:\s*', name)[0])
        names.append(re.sub(r'(?i)\bpossible worlds\b', '', name).strip(' .:-'))
    seasons = list(dict.fromkeys([getattr(video, 'season', 1), 1, 2]))
    seen = {(series.casefold(), getattr(video, 'season', 1))}
    for name in dict.fromkeys(n for n in names if n):
        for season in seasons:
            if (name.casefold(), season) in seen:
                continue
            alternate = copy.copy(video)
            if hasattr(video, 'external_ids'):
                alternate.external_ids = copy.copy(video.external_ids)
            alternate.series, alternate.season = name, season
            # Remove ids that could force providers back to the original season/series.
            for attr in ('imdb_id', 'tmdb_id', 'tvdb_id', 'series_imdb_id', 'series_tmdb_id', 'series_tvdb_id'):
                if hasattr(alternate, attr):
                    setattr(alternate, attr, None)
            result.append(alternate)
            seen.add((name.casefold(), season))
    return result[:12]




_CACHE_LOCK = threading.Lock()


def _configure_subliminal_cache() -> None:
    """Configure the process-local cache once, preserving any caller configuration.

    Memory avoids writing provider auth tokens to disk and requires no new dependency.
    """
    from subliminal import region
    with _CACHE_LOCK:
        if not region.is_configured:
            region.configure('dogpile.cache.memory')


def _failure_kind(error: Exception) -> str:
    """Classify provider errors internally; never expose their credential-bearing text."""
    name = type(error).__name__.casefold()
    text = str(error).casefold()
    response = getattr(error, 'response', None)
    status = getattr(response, 'status_code', None) or getattr(error, 'code', None)
    if status == 429 or '429' in text or any(part in name for part in ('downloadlimit', 'quota')) or 'quota' in text:
        return 'quota'
    if status in (401, 403) or 'authenticat' in name or 'authenticat' in text or 'login required' in text:
        return 'auth'
    if (any(part in name for part in ('timeout', 'connection', 'urlerror', 'requestexception'))
            or any(part in text for part in ('timed out', 'connection error', 'connection refused', 'network error', 'name resolution'))
            or isinstance(error, (TimeoutError, ConnectionError))):
        return 'network'
    if status is not None or any(part in name for part in ('provider', 'opensubtitlescomerror', 'serviceunavailable', 'configuration', 'regionnotconfigured')):
        return 'unavailable'
    return 'unknown'


def _failure_message(provider: str, kind: str) -> str:
    if kind == 'auth' and provider == 'opensubtitlescom':
        return ('OpenSubtitles.com downloads need a free account: set '
                'OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD')
    if kind == 'auth':
        return 'authentication required; configure a provider account'
    if kind == 'quota':
        return 'daily download quota reached; try again after the quota resets'
    if kind == 'network':
        return 'network error; check connectivity and retry'
    return 'provider unavailable; try another provider or retry later'

def _metadata_pool_class():
    """Lazy local subclasses preserve provider facts without touching the registry/package."""
    from subliminal import ProviderPool
    from subliminal.providers.opensubtitlescom import OpenSubtitlesComProvider, OpenSubtitlesComSubtitle

    class RankedSubtitle(OpenSubtitlesComSubtitle):
        @classmethod
        def from_response(cls, response, **kwargs):
            subtitle = super().from_response(response, **kwargs)
            subtitle.raw_attributes = copy.deepcopy(response.get('attributes', {}))
            return subtitle

    class RankedProvider(OpenSubtitlesComProvider):
        subtitle_class = RankedSubtitle

    class RankedPool(ProviderPool):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.provider_failures = {}

        def list_subtitles(self, video, languages):
            # The upstream pool logs and swallows provider exceptions. Preserve a safe
            # category instead, while continuing independently with other providers.
            from subliminal.extensions import provider_manager
            subtitles = []
            for name in self.providers:
                if name in self.discarded_providers:
                    continue
                try:
                    cls = provider_manager[name].plugin
                    supported = cls.check_languages(set(languages))
                    if cls.check(video) and supported:
                        subtitles.extend(self[name].list_subtitles(video, supported))
                except Exception as error:
                    self.provider_failures[name] = _failure_kind(error)
            return subtitles

        def download_subtitle(self, subtitle):
            # Let callers see the failure category; upstream returns False for everything.
            self[subtitle.provider_name].download_subtitle(subtitle)
            return subtitle.is_valid()

        def __getitem__(self, name):
            if name != 'opensubtitlescom':
                return super().__getitem__(name)
            if name not in self.providers:
                raise KeyError(name)
            if name not in self.initialized_providers:
                provider = RankedProvider(**self.provider_configs.get(name, {}))
                provider.initialize()
                self.initialized_providers[name] = provider
            return self.initialized_providers[name]

    return RankedPool


_FACTS = ('from_trusted', 'ratings', 'votes', 'download_count', 'new_download_count',
          'machine_translated', 'ai_translated', 'hearing_impaired', 'fps', 'release',
          'moviehash_match', 'upload_date')


def _provider_facts(subtitle) -> dict:
    raw = getattr(subtitle, 'raw_attributes', {}) or {}
    # Persist only ranking facts, never arbitrary API response fields/credentials.
    facts = {key: raw.get(key, getattr(subtitle, key, None)) for key in _FACTS}
    uploader = raw.get('uploader', {}) or {}
    if isinstance(uploader, dict):
        facts['uploader'] = {key: uploader[key] for key in ('rank', 'name') if key in uploader}
    return json.loads(json.dumps(facts, default=str))


def _number(value, default=0.) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError):
        return default


def _flag(value) -> bool:
    return value is True or str(value).casefold() in {'1', 'true', 'yes'}


def _release_similarity(video: Path, release: str) -> float:
    if not release:
        return 0.
    first, second = _metadata(video), _metadata(Path(release))
    features = ['release_group', 'source', 'screen_size']
    known = [key for key in features if first.get(key)]
    technical = sum(str(first[key]).casefold() == str(second.get(key, '')).casefold()
                    for key in known) / max(1, len(known))
    return .5 * _overlap(video.stem, Path(release).stem) + .5 * technical


def _prescore(facts: dict, match: float, video: Path, fps=None) -> tuple[float, str]:
    if _flag(facts.get('machine_translated')) or _flag(facts.get('ai_translated')):
        return 0., 'excluded machine/AI translation'
    release = _release_similarity(video, str(facts.get('release') or ''))
    source_fps, target_fps = _number(facts.get('fps')), _number(fps)
    fps_match = 1. if source_fps > 0 and target_fps > 0 and abs(source_fps - target_fps) < .03 else 0.
    hash_match = _flag(facts.get('moviehash_match'))
    exact = 1. if hash_match else .55 * max(0., min(1., match)) + .35 * release + .1 * fps_match
    rank = str((facts.get('uploader') or {}).get('rank', '')).casefold()
    trusted = _flag(facts.get('from_trusted')) or any(word in rank for word in ('administrator', 'trusted', 'translator', 'gold'))
    trust = 1. if trusted else .2
    downloads = max(0., _number(facts.get('download_count'))) + max(0., _number(facts.get('new_download_count')))
    popularity = min(1., math.log1p(downloads) / math.log1p(100_000))
    votes = max(0., _number(facts.get('votes')))
    rating = max(0., min(10., _number(facts.get('ratings'), 6.)))
    bayesian = (10 * 6. + votes * rating) / (10 + votes) / 10
    score = .65 * exact + .25 * trust + .1 * (.6 * popularity + .4 * bayesian)
    reasons = ['movie hash match'] if hash_match else [f'release similarity {release:.0%}', f'provider match {match:.0%}']
    if fps_match:
        reasons.append('matching fps')
    if trusted:
        reasons.append('trusted uploader' + (f' ({rank})' if rank else ''))
    reasons += [f'{downloads:g} downloads', f'Bayesian rating {bayesian * 10:.1f}/10']
    if _flag(facts.get('hearing_impaired')):
        score -= .03
        reasons.append('hearing-impaired penalty')
    return max(0., min(1., score)), '; '.join(reasons)


def _normalized_title(value) -> str:
    text = unicodedata.normalize('NFKD', str(value or '')).casefold()
    return ' '.join(re.findall(r'[^\W_]+', ''.join(c for c in text if not unicodedata.combining(c))))


def _title_matches(first, second) -> bool:
    a, b = _normalized_title(first), _normalized_title(second)
    if not a or not b:
        return False
    words_a, words_b = set(a.split()), set(b.split())
    dice = 2 * len(words_a & words_b) / (len(words_a) + len(words_b))
    return dice >= .85 or SequenceMatcher(None, a, b).ratio() >= .9


def _identifier(value) -> str:
    # OpenSubtitles uses numeric IMDb ids; subliminal often uses the tt prefix.
    return str(value or '').lower().removeprefix('tt').lstrip('0')


def _subtitle_work(subtitle) -> dict:
    raw = getattr(subtitle, 'raw_attributes', {}) or {}
    details = raw.get('feature_details', {}) or {}
    def first(*values):
        return next((v for v in values if v is not None and v != ''), None)
    def attr(*names):
        return first(*(getattr(subtitle, name, None) for name in names))
    series = first(attr('series_title', 'series'), details.get('parent_title'))
    season = first(attr('series_season', 'season'), details.get('season_number'))
    episode = first(attr('series_episode', 'episode'), details.get('episode_number'))
    kind = first(attr('movie_kind'), details.get('feature_type'))
    if not kind:
        kind = 'episode' if season is not None or episode is not None or series else 'movie'
    kind = str(kind).casefold()
    title = first(attr('movie_title', 'title'), details.get('title'))
    if kind == 'episode' and not series:
        # Podnapisi's title is the series name, not the episode title.
        series, title = title, None
    return {'kind': kind, 'title': title, 'series': series, 'season': season, 'episode': episode,
            'year': first(attr('movie_year', 'year'), details.get('year')),
            'imdb_id': _identifier(first(attr('movie_imdb_id', 'imdb_id'), details.get('imdb_id'))),
            'tmdb_id': _identifier(first(attr('movie_tmdb_id', 'tmdb_id'), details.get('tmdb_id')))}


def _target_work(video: Path, scanned) -> dict:
    guessed = _metadata(video)
    series = getattr(scanned, 'series', None)
    return {'kind': 'episode' if series else 'movie',
            'series': series, 'season': getattr(scanned, 'season', guessed.get('season')),
            'episode': getattr(scanned, 'episode', guessed.get('episode')),
            'title': (getattr(scanned, 'title', None) or guessed.get('episode_title')) if series
                     else (getattr(scanned, 'title', None) or guessed.get('title')),
            'year': getattr(scanned, 'year', None) or guessed.get('year'),
            'imdb_id': _identifier(getattr(scanned, 'imdb_id', None)),
            'tmdb_id': _identifier(getattr(scanned, 'tmdb_id', None))}


def _movie_title_year(work: dict, target: dict) -> bool:
    year, target_year = _number(work.get('year')), _number(target.get('year'))
    return (work['kind'] == 'movie' and _title_matches(work.get('title'), target.get('title'))
            and year > 0 and target_year > 0 and abs(year - target_year) <= 1)


def _same_work(work: dict, target: dict, ids: dict) -> bool:
    if work['kind'] != target['kind']:
        return False
    if target['kind'] == 'movie':
        return _movie_title_year(work, target) or any(
            work.get(key) and work[key] == ids.get(key) for key in ('imdb_id', 'tmdb_id'))
    series_match = _title_matches(work.get('series'), target.get('series'))
    cosmos = {_normalized_title(work.get('series')), _normalized_title(target.get('series'))}
    cosmos_alias = cosmos <= {'cosmos', 'cosmos possible worlds'} and bool(all(cosmos))
    if not (series_match or cosmos_alias):
        return False
    episode, wanted = _number(work.get('episode'), -1), _number(target.get('episode'), -2)
    if episode != wanted or episode < 0:
        return False
    season, wanted_season = _number(work.get('season'), -1), _number(target.get('season'), -2)
    if season == wanted_season and season >= 0:
        return True
    # Cosmos Possible Worlds is sometimes numbered season 1. Episode title must
    # also agree, otherwise Cosmos (1980) S01E04 is a different work.
    return (cosmos_alias and {season, wanted_season} == {1., 2.}
            and _title_matches(work.get('title'), target.get('title')))

def download_subtitles(video: Path, lang: str, out_dir: Path, providers: list[str] | None = None,
                       credentials: dict[str, str] | None = None, max_downloads: int = 2) -> list[SubtitleCandidate]:
    if max_downloads <= 0:
        return []
    video, out_dir, lang = Path(video), Path(out_dir), _language(lang)
    providers = list(providers) if providers is not None else ['opensubtitlescom', 'podnapisi', 'gestdown']
    if not providers:
        return []
    try:
        from babelfish import Language
        from subliminal import compute_score, scan_video
        from subliminal.score import get_scores
    except ImportError:
        raise JellyDubError('Subtitle downloads require the opensubtitles extra; install jellydub[opensubtitles]') from None
    identity = cache_key(_media_id(video), lang, providers, 'subsource-v1')
    manifest = out_dir / f'{identity}.json'
    try:
        entries = json.loads(manifest.read_text(encoding='utf-8'))
        cached = [SubtitleCandidate(out_dir / e['file'], e['origin'], lang, e['score'], e['note'],
                                    e.get('metadata', {}), e.get('prescore', _prescore(e.get('metadata', {}), e['score'], video)[0]),
                                    e.get('quality'), e.get('issues', [])) for e in entries]
        if cached and all(c.metadata.get('work_filter_version') == 1 and c.path.parent == out_dir
                          and parse_srt(c.path) for c in cached):
            return sorted(cached, key=lambda c: -c.prescore)[:max_downloads]
    except (OSError, ValueError, KeyError, TypeError, JellyDubError):
        pass
    credentials = credentials or {}
    options = {key: credentials.get(key) or credentials.get('OPENSUBTITLES_' + key.upper()) or os.environ.get('OPENSUBTITLES_' + key.upper())
               for key in ('username', 'password', 'apikey')}
    configs = {'opensubtitlescom': {k: v for k, v in options.items() if v}}
    result = []
    failures, found_facts = {}, {}
    try:
        with _quiet_provider_logs():
            _configure_subliminal_cache()
            scanned = scan_video(str(video))
            language = Language.fromalpha2(lang)
            ranked, listed = {}, {}
            target = _target_work(video, scanned)
            with _metadata_pool_class()(providers=providers, provider_configs=configs) as pool:
                for query in _queries(scanned):
                    try:
                        subtitles = pool.list_subtitles(query, {language})
                        failures.update(getattr(pool, 'provider_failures', {}))
                        for subtitle in subtitles:
                            if subtitle.language != language:
                                continue
                            key = (subtitle.provider_name, str(subtitle.id))
                            score = compute_score(subtitle, query) / max(1, get_scores(query)['hash'])
                            if key not in listed or score > listed[key][0]:
                                listed[key] = (score, subtitle, _subtitle_work(subtitle))
                    except Exception as error:
                        failures.setdefault('search', _failure_kind(error))
                        continue
                ids = {key: target.get(key) for key in ('imdb_id', 'tmdb_id')}
                if target['kind'] == 'movie':
                    exact = [item for item in listed.values() if _movie_title_year(item[2], target)]
                    if exact:
                        # Anchor IDs only to the strongest title/year match, never to
                        # an arbitrary broad-query hit. Then accept localized titles by id.
                        anchor = max(exact, key=lambda item: (
                            _normalized_title(item[2].get('title')) == _normalized_title(target.get('title')),
                            _number(item[2].get('year')) == _number(target.get('year')), item[0]))[2]
                        ids = {key: ids[key] or anchor.get(key) for key in ids}
                for score, subtitle, work in listed.values():
                    if not _same_work(work, target, ids):
                        continue
                    facts = _provider_facts(subtitle)
                    facts['work'] = work
                    facts['work_filter_version'] = 1
                    found_facts.setdefault(subtitle.provider_name, {})[str(subtitle.id)] = facts
                    prescore, note = _prescore(facts, score, video, getattr(scanned, 'fps', None))
                    if prescore <= 0:
                        continue
                    key = (subtitle.provider_name, str(subtitle.id))
                    ranked[key] = (prescore, score, subtitle, facts, note)
                attempts, blocked = 0, set()
                best = sorted(ranked.values(), key=lambda item: -item[0])
                for prescore, score, subtitle, facts, note in best:
                    if attempts >= max_downloads or len(result) >= max_downloads:
                        break
                    provider = subtitle.provider_name
                    if provider in blocked:
                        continue
                    attempts += 1
                    try:
                        if not pool.download_subtitle(subtitle):
                            failures[provider] = 'unavailable'
                            blocked.add(provider)
                            attempts -= 1
                            continue
                        path = out_dir / f'{identity}.{cache_key(provider, str(subtitle.id))}.{lang}.srt'
                        out_dir.mkdir(parents=True, exist_ok=True)
                        temporary = tmp_path_for(path)
                        try:
                            atomic_write_text(temporary, subtitle.text)
                            cues = parse_srt(temporary)
                            write_srt(cues, temporary)
                            temporary.replace(path)
                        finally:
                            temporary.unlink(missing_ok=True)
                        result.append(SubtitleCandidate(path, provider, lang, score, note, facts, prescore))
                    except Exception as error:
                        kind = _failure_kind(error)
                        failures[provider] = kind
                        if kind in {'auth', 'quota', 'network', 'unavailable'}:
                            blocked.add(provider)
                            # These failures happen before a valid download. Skip the
                            # rest of this provider, keeping the quota budget for others.
                            attempts -= 1
                        continue
    except Exception as error:
        failures.setdefault('search', _failure_kind(error))
    successful = {candidate.origin for candidate in result}
    for provider, kind in sorted(failures.items()):
        label = provider if provider in {'opensubtitlescom', 'podnapisi', 'gestdown'} else 'subtitle provider'
        facts = list(found_facts.get(provider, {}).values())
        if facts and provider not in successful:
            reason = 'without login' if kind == 'auth' else 'due to ' + {
                'quota': 'daily download quota reached', 'network': 'network errors',
            }.get(kind, 'provider unavailability')
            downloads = max((_number(f.get('download_count')) + _number(f.get('new_download_count')) for f in facts), default=0)
            logging.warning('%s %s subtitles found on %s but not downloadable %s (highest download count: %g). %s',
                            len(facts), lang.upper(), label, reason, downloads, _failure_message(provider, kind))
        else:
            logging.warning('%s: %s', label, _failure_message(provider, kind))
    if not result:
        logging.warning('No downloadable subtitles found; providers may be unavailable or require login')
    else:
        atomic_write_text(manifest, json.dumps([{'file': c.path.name, 'origin': c.origin, 'score': c.score, 'note': c.note,
                                                'metadata': c.metadata, 'prescore': c.prescore,
                                                'quality': c.quality, 'issues': c.issues}
                                                for c in result], ensure_ascii=False))
    return result
