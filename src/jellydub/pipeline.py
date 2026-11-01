"""Resumable stage orchestration; model lifetimes never overlap."""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Callable

from .cache import atomic_write_bytes, cache_key, tmp_path_for
from .config import Config
from .export import export_mp3, mux_mkv, output_paths
from .ffmpeg import duration, require_ffmpeg
from .mix import extract_audio, mix
from .models import JellyDubError, MixMode
from .separate import separate_background
from .subtitles import parse_srt, write_srt
from .timing import align, render_timeline
from .tts import get_provider
from .tts.synthesize import synthesize_cues
from .validate import check_audio

Reporter = Callable[[str], None]


def _fingerprint(path: Path) -> tuple:
    stat = path.stat()
    return str(path.resolve()), stat.st_size, stat.st_mtime_ns


def _work_dir(video: Path, srt: Path, config: Config) -> Path:
    refs = []
    for reference in config.reference or []:
        path = Path(reference.split("=", 1)[-1]).expanduser()
        refs.append(_fingerprint(path) if path.is_file() else str(path))
    # Scheduling does not change speech content. Preserve pre-workers cache keys
    # so existing jobs can resume when concurrency is adjusted.
    cache_settings = asdict(config)
    for option in ("tts_workers", "subs", "download", "search_online", "providers", "subtitle_credentials_env"):
        cache_settings.pop(option, None)
    key = cache_key(_fingerprint(video), srt.read_bytes().hex(), cache_settings, refs)
    work = config.cache_dir / f"{video.stem}-{key}"
    work.mkdir(parents=True, exist_ok=True)
    return work


def _inputs(video: Path, srt: Path, config: Config) -> None:
    config.validate()
    for path in (video, srt):
        if not path.is_file():
            raise JellyDubError(f"Input file does not exist: {path}")
    require_ffmpeg()


def get_translator(**options):
    # Import only after language detection has selected English.
    from .translate import get_translator as factory
    return factory(**options)


def translate_cues(*args, **options):
    from .translate import translate_cues as stage
    return stage(*args, **options)


def detect_source_language(srt: Path) -> tuple[str, str]:
    """Deterministic filename/diacritics heuristic; ambiguous input defaults to English."""
    import unicodedata
    tags = srt.name.lower().split(".")
    if tags[-1] == "srt" and any(tag in {"vi", "vie"} for tag in tags[1:-1]):
        return "vi", "Vietnamese filename tag"
    marked = set("ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ")
    texts = [unicodedata.normalize("NFC", cue.text).lower() for cue in parse_srt(srt) if cue.text.strip()]
    if texts and sum(any(char in marked for char in text) for text in texts) / len(texts) > 0.30:
        return "vi", "Vietnamese diacritics in more than 30% of cues"
    return "en", "no clear Vietnamese filename tag or diacritics"


def _translated(srt: Path, work: Path, config: Config, report: Reporter) -> Path:
    language, reason = ((config.source_lang, "explicit --source-lang") if config.source_lang != "auto"
                        else detect_source_language(srt))
    report(f"Source language: {language} ({reason})")
    if language == "vi":
        report("Vietnamese subtitles: skipping translation")
        return srt
    target = work / "translated.vi.srt"
    if not target.is_file() or not target.stat().st_size:
        report(f"Translating subtitles ({config.translator})…")
        translator = get_translator(name=config.translator, model=config.translator_model, device=config.device)
        try:
            cues = translate_cues(parse_srt(srt), translator, work / "translations",
                                  on_progress=lambda n, total: report(f"Translation {n}/{total}"))
            write_srt(cues, target)
        finally:
            translator.close()
    return target


def _publish_srt(source: Path, target: Path, inputs: tuple[Path, ...]) -> None:
    if source.resolve() == target.resolve():
        return
    if any(target.resolve() == p.resolve() for p in inputs):
        raise JellyDubError(f"Refusing to overwrite source file: {target}")
    atomic_write_bytes(target, source.read_bytes())


def translate(video: Path, srt: Path, config: Config, report: Reporter = print) -> Path:
    _inputs(video, srt, config)
    work = _work_dir(video, srt, config)
    translated = _translated(srt, work, config, report)
    target = output_paths(video, config.out_dir)["srt"]
    _publish_srt(translated, target, (video, srt))
    return target


def _stage(path: Path, action: Callable[[Path], None]) -> None:
    if path.is_file() and path.stat().st_size:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = tmp_path_for(path)
    try:
        action(temporary)
        if not temporary.is_file() or not temporary.stat().st_size:
            raise JellyDubError(f"Stage produced no output: {path.name}")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def dub(video: Path, srt: Path | None, config: Config, report: Reporter = print) -> dict[str, Path]:
    config.validate()
    if not video.is_file():
        raise JellyDubError(f"Input file does not exist: {video}")
    require_ffmpeg()
    original_srt = srt
    srt, language = _resolve_subtitle(video, srt, config, report)
    if language is not None:
        config = replace(config, source_lang=language)
    _inputs(video, srt, config)
    work = _work_dir(video, srt, config)
    outputs = output_paths(video, config.out_dir)
    for key in ("audio", "mkv"):
        if any(outputs[key].resolve() == p.resolve() for p in (video, srt)):
            raise JellyDubError(f"Refusing to overwrite source: {outputs[key]}")
    translated = _translated(srt, work, config, report)
    cues = parse_srt(translated)
    _publish_srt(translated, outputs["srt"], (video, original_srt or srt))
    total = duration(video)
    if any(c.start < 0 or c.end > total + 0.05 or c.end < c.start for c in cues):
        raise JellyDubError("Subtitle timestamps fall outside the video duration")
    options = {"rate": config.rate, "pitch": config.pitch} if config.tts == "edge" else {"device": "cpu" if config.device == "auto" else config.device,
                                                                                    "cache_dir": config.cache_dir / "voices"}
    if config.voice is not None:
        options["voice"] = config.voice
    if config.tts == "vieneu":
        refs = {}
        unlabelled = []
        for value in config.reference or []:
            if "=" in value:
                label, filename = value.split("=", 1)
                refs[label] = Path(filename).expanduser()
            else:
                unlabelled.append(Path(value).expanduser())
        if len(unlabelled) > 1:
            raise JellyDubError("Use LABEL=FILE for multiple reference voices")
        if refs:
            options["speaker_refs"] = refs
        if unlabelled:
            options["reference"] = unlabelled[0]
    report(f"Synthesizing {len(cues)} subtitle segments ({config.tts})…")
    provider = get_provider(config.tts, **options)
    try:
        results, failures = synthesize_cues(cues, provider, work / "tts", workers=config.tts_workers,
                                          on_progress=lambda n, count: report(f"TTS {n}/{count}"))
    finally:
        provider.close()
    if failures:
        for cue, error in failures:
            report(f"Warning: segment {cue.index} failed: {error}")
        raise JellyDubError(f"{len(failures)} TTS segments failed. Re-run the same command to retry; successful segments are cached.")
    report("Aligning speech…")
    clips = align(cues, results, work / "aligned", max_tempo=config.max_tempo)
    warning_count = sum(bool(clip.warning) for clip in clips)
    if warning_count:
        report(f"Timing warnings: {warning_count} segment(s)")
    for clip in clips:
        if clip.warning:
            report(f"Warning: segment {clip.cue_index}: {clip.warning}")
    if any(c.start + c.duration > total + 0.02 for c in clips):
        raise JellyDubError("Aligned speech extends beyond the video; shorten the final subtitles and re-run to avoid truncation")
    timeline = work / "timeline.wav"
    _stage(timeline, lambda p: render_timeline(clips, total, p))
    mode = MixMode(config.mix)
    original = background = None
    if mode != MixMode.DUB_ONLY:
        original = work / "original.wav"
        _stage(original, lambda p: extract_audio(video, p, stream_index=config.audio_stream))
    if mode == MixMode.REPLACE:
        report("Separating original dialogue locally…")
        try:
            background = separate_background(original, work / "separation", device=config.device)
        except JellyDubError as exc:
            if not config.fallback_voiceover:
                raise JellyDubError(f"Separation failed: {exc}. Try --mix voiceover or --fallback-voiceover.") from exc
            report(f"Warning: separation failed; using voiceover: {exc}")
            mode = MixMode.VOICEOVER
    mixed = work / f"mixed-{mode.value}.wav"
    _stage(mixed, lambda p: mix(mode, timeline, p, original_wav=original, background_wav=background))
    problems = check_audio(mixed, total)
    if problems:
        raise JellyDubError("Mixed audio validation failed: " + "; ".join(problems))
    # Export from the keyed work directory on every run: an existing user output may be stale.
    encoded = work / "export.mp3"
    _stage(encoded, lambda p: export_mp3(mixed, p))
    problems = check_audio(encoded, total)
    if problems:
        raise JellyDubError("Export validation failed: " + "; ".join(problems))
    _copy_output(encoded, outputs["audio"])
    if config.mkv:
        muxed = work / "export.mkv"
        _stage(muxed, lambda p: mux_mkv(video, encoded, p))
        _copy_output(muxed, outputs["mkv"])
    report(f"Done: {outputs['audio']}")
    return {k: v for k, v in outputs.items() if k != "mkv" or config.mkv}


def _copy_output(source: Path, target: Path) -> None:
    """Publish potentially large media without reading it all into memory."""
    import shutil
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = tmp_path_for(target)
    try:
        shutil.copyfile(source, temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


# Lazy adapters preserve importability while subtitle-source modules/optional extras
# are unavailable. Tests patch these contracts, never the real network providers.
def find_local_subtitles(*args, **kwargs):
    from .subsource import find_local_subtitles as stage
    return stage(*args, **kwargs)


def extract_embedded_subtitles(*args, **kwargs):
    from .subsource import extract_embedded_subtitles as stage
    return stage(*args, **kwargs)


def download_subtitles(*args, **kwargs):
    from .subsource import download_subtitles as stage
    return stage(*args, **kwargs)


def estimate_sync(*args, **kwargs):
    from .subsync import estimate_sync as stage
    return stage(*args, **kwargs)


def apply_sync(*args, **kwargs):
    from .subsync import apply_sync as stage
    return stage(*args, **kwargs)


def _source_dir(video: Path, config: Config) -> Path:
    folder = config.cache_dir / "subtitles" / f"{video.stem}-{cache_key(_fingerprint(video))}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _local_candidates(video: Path, language: str, work: Path, report: Reporter):
    candidates = list(find_local_subtitles(video, language))
    try:
        candidates.extend(extract_embedded_subtitles(video, language, work / "embedded"))
    except JellyDubError:
        report(f"Warning: could not extract embedded {language} subtitles")
    return sorted(candidates, key=lambda c: c.score, reverse=True)


class _ReferenceCues(list):
    """A cue list retaining alternative full-track timing references."""

    def __init__(self, alternatives):
        super().__init__(alternatives[0][1])
        self.alternatives = alternatives


def _english_reference(video: Path, work: Path, report: Reporter):
    references = []
    for candidate in _local_candidates(video, "en", work, report):
        if (getattr(candidate, "metadata", {}) or {}).get("forced", False):
            report(f"Skipping forced English sync reference: {candidate.path}")
            continue
        if candidate.score < 0.5:
            continue
        try:
            cues = parse_srt(candidate.path)
            if cues:
                references.append((candidate, cues))
        except JellyDubError:
            continue
    # Actual parsed cue counts beat metadata counts that may be missing/stale.
    references.sort(key=lambda item: (len(item[1]), item[0].origin == "embedded", item[0].score), reverse=True)
    if references:
        candidate, cues = references[0]
        report(f"Sync reference: {candidate.path} ({candidate.origin}, {len(cues)} cues)")
        return _ReferenceCues([(candidate.path, cues) for candidate, cues in references])
    report("No English timing reference; subtitle synchronization is unverified")
    return None


def _reference_for_candidate(reference, cues, report: Reporter):
    alternatives = getattr(reference, "alternatives", [("selected reference", reference)])
    for path, candidate_reference in alternatives:
        if candidate_reference is None:
            continue
        if len(candidate_reference) < 0.3 * len(cues):
            report(f"Skipping sparse English sync reference {path}: {len(candidate_reference)} cues for {len(cues)} candidate cues (below 30%)")
            continue
        return candidate_reference
    if reference is not None:
        report("No sufficiently complete English reference; subtitle synchronization is unverified")
    return None


def strip_junk(*args, **kwargs):
    from .subquality import strip_junk as stage
    return stage(*args, **kwargs)


def assess_subtitle(*args, **kwargs):
    from .subquality import assess_subtitle as stage
    return stage(*args, **kwargs)


def _best_candidate(candidates, reference, work: Path, report: Reporter):
    import math
    ranked = []
    for candidate in candidates:
        report(f"Candidate: {candidate.path} origin={candidate.origin} score={candidate.score:.3f} {candidate.note}")
        try:
            cues = parse_srt(candidate.path)
            if not cues:
                raise JellyDubError("empty subtitles")
            candidate_reference = _reference_for_candidate(reference, cues, report)
            quality = assess_subtitle(cues, lang=candidate.language, reference=candidate_reference)
            candidate.quality = quality.score
            candidate.issues = list(dict.fromkeys([*getattr(candidate, "issues", []), *quality.issues]))
            result = estimate_sync(cues, candidate_reference) if candidate_reference is not None else None
            reason = ""
            if not math.isfinite(quality.score) or quality.score < 0.5:
                reason = "subtitle quality below 0.5"
            if result is not None:
                values = (result.offset, result.scale, result.matched_ratio, result.residual)
                if (not all(math.isfinite(v) for v in values) or result.scale <= 0 or
                        result.matched_ratio < 0.5 or result.residual > 0.3):
                    reason = f"matched_ratio={result.matched_ratio:.3f}, residual={result.residual:.3f}s"
                sync_quality = max(0., min(1., result.matched_ratio)) * max(0., 1. - result.residual / 0.3)
            else:
                sync_quality = 0.5  # neutral, explicitly unverified
            local = candidate.origin in {"local", "sidecar", "embedded", "explicit"}
            prescore = 1.0 if local else max(0., min(1., getattr(candidate, "prescore", 0.0)))
            final = 0.4 * prescore + 0.4 * quality.score + 0.2 * sync_quality
            ranked.append((final, candidate, cues, result, reason))
        except JellyDubError:
            ranked.append((-1., candidate, [], None, "unreadable subtitle or synchronization failed"))
    ranked.sort(key=lambda row: (row[0], row[1].score), reverse=True)
    report("Rank | Origin | Uploader/trusted | Downloads | Rating | Quality | Sync offset | Final | Issues")
    for number, (final, candidate, cues, result, reason) in enumerate(ranked, 1):
        metadata = getattr(candidate, "metadata", {}) or {}
        uploader = metadata.get("uploader", {})
        if isinstance(uploader, dict):
            uploader = uploader.get("name") or uploader.get("rank") or "unknown"
        trusted = "trusted" if metadata.get("from_trusted") else "unverified"
        downloads = metadata.get("download_count", metadata.get("new_download_count", "?"))
        rating = metadata.get("ratings", metadata.get("rating", "?"))
        quality_value = getattr(candidate, "quality", None)
        quality_text = f"{quality_value:.3f}" if quality_value is not None else "?"
        offset = f"{result.offset:+.3f}s" if result is not None else "unverified"
        issues = "; ".join([*getattr(candidate, "issues", []), *([reason] if reason else [])]) or "none"
        report(f"{number} | {candidate.origin} | {uploader}/{trusted} | {downloads} | {rating} | {quality_text} | {offset} | {max(0., final):.3f} | {issues}")
        if reason:
            report(f"Rejected {candidate.path}: {reason}")
    for final, candidate, cues, result, reason in ranked:
        if reason:
            continue
        corrected = apply_sync(cues, result) if result is not None else cues
        cleaned = strip_junk(corrected)
        if not cleaned:
            report(f"Rejected {candidate.path}: no dialogue remains after removing ads/credits")
            continue
        sync_note = f"offset {result.offset:+.3f}s, scale {result.scale:.6f}" if result else "offset unverified"
        report(f"Using {candidate.origin} {candidate.language.upper()} subtitle {candidate.path}; {sync_note}; final={final:.3f}")
        if cleaned == cues:
            return candidate.path
        values = (result.offset, result.scale) if result else None
        target = work / f"selected-{cache_key(candidate.path.read_bytes().hex(), values, cleaned)}.{candidate.language}.srt"
        if not target.is_file() or not target.stat().st_size:
            write_srt(cleaned, target)
        return target
    return None


def _online_candidates(video, language, work, config, report, local_candidates, reference):
    import math
    if language == "vi" and config.download and not config.search_online:
        for candidate in local_candidates:
            if candidate.origin not in {"local", "sidecar", "embedded"} or candidate.language != "vi":
                continue
            try:
                cues = parse_srt(candidate.path)
                timing = _reference_for_candidate(reference, cues, report)
                if not cues or timing is None:
                    continue
                quality = assess_subtitle(cues, lang="vi", reference=timing)
                if not math.isfinite(quality.score) or quality.score < 0.9:
                    continue
                result = estimate_sync(cues, timing)
                if (all(math.isfinite(v) for v in (result.offset, result.scale, result.matched_ratio, result.residual))
                        and result.scale > 0 and result.matched_ratio >= 0.9 and result.residual <= 0.1
                        and strip_junk(apply_sync(cues, result))):
                    report("skipping online search: good local subtitle")
                    return []
            except JellyDubError:
                continue
    return _download_candidates(video, language, work, config, report)


def _download_candidates(video: Path, language: str, work: Path, config: Config, report: Reporter):
    import importlib.util
    import os
    if not config.download:
        report("Subtitle downloads disabled")
        return []
    if importlib.util.find_spec("subliminal") is None:
        report("Subtitle downloads skipped: install the opensubtitles extra")
        return []
    credentials = {key: os.environ[name] for key, name in config.subtitle_credentials_env.items()
                   if os.environ.get(name)}
    report(f"Searching online for {language.upper()} subtitles")
    try:
        return download_subtitles(video, language, work / "downloaded", providers=config.providers,
                                  credentials=credentials or None, max_downloads=2)
    except (JellyDubError, OSError):
        # Never interpolate provider exceptions: they may contain credentials.
        report("Warning: subtitle download unavailable; continuing with local sources")
        return []


def _resolve_subtitle(video: Path, explicit: Path | None, config: Config, report: Reporter):
    if explicit is not None:
        if not explicit.is_file():
            raise JellyDubError(f"Input file does not exist: {explicit}")
        report(f"Using explicit subtitle: {explicit}")
        language = config.source_lang if config.source_lang != "auto" else detect_source_language(explicit)[0]
        if language != "vi":
            return explicit, None
        from types import SimpleNamespace
        work = _source_dir(video, config)
        reference = _english_reference(video, work, report)
        chosen = _best_candidate([SimpleNamespace(path=explicit, origin="explicit", language="vi", score=1.0,
                                                  note="user supplied")], reference, work, report)
        if chosen is None:
            raise JellyDubError("Explicit Vietnamese subtitle failed timing checks; supply subtitles matching this video")
        return chosen, "vi"
    work = _source_dir(video, config)
    reference = _english_reference(video, work, report)
    if config.subs != "translate":
        candidates = []
        if config.subs != "download":
            candidates.extend(_local_candidates(video, "vi", work, report))
        if config.subs in {"auto", "download"}:
            candidates.extend(_online_candidates(video, "vi", work, config, report, candidates, reference))
        chosen = _best_candidate(candidates, reference, work, report)
        if chosen is not None:
            return chosen, "vi"
        if config.subs in {"local", "download"}:
            raise JellyDubError(f"No acceptable Vietnamese subtitles found in --subs {config.subs} mode; try --subs auto or provide --srt")
    for candidate in _local_candidates(video, "en", work, report):
        try:
            if candidate.score >= 0.5 and parse_srt(candidate.path):
                report(f"No accepted Vietnamese subtitle; translating English subtitle {candidate.path}")
                return candidate.path, "en"
        except JellyDubError:
            continue
    raise JellyDubError("No suitable subtitles found. Supply --srt EN.srt or VI.srt, or enable subtitle downloads")


def subs(video: Path, config: Config, *, lang: str = "vi", force: bool = False,
         report: Reporter = print) -> Path:
    """Find and sync a subtitle; atomically publish only when replacement is allowed."""
    config.validate()
    if lang not in {"vi", "en"}:
        raise JellyDubError("Subtitle language must be vi or en")
    if not video.is_file():
        raise JellyDubError(f"Input file does not exist: {video}")
    require_ffmpeg()
    work = _source_dir(video, config)
    reference = _english_reference(video, work, report) if lang == "vi" else None
    candidates = _local_candidates(video, lang, work, report)
    candidates.extend(_online_candidates(video, lang, work, config, report, candidates, reference))
    selected = _best_candidate(candidates, reference, work, report)
    if selected is None:
        raise JellyDubError(f"No acceptable {lang.upper()} subtitles found")
    target = (config.out_dir or video.parent) / f"{video.stem}.{lang}.srt"
    if target.resolve() == video.resolve():
        raise JellyDubError("Refusing to overwrite source video")
    if selected.resolve() == target.resolve():
        report(f"Subtitle already at {target}; unchanged")
        return target
    content = selected.read_bytes()
    if target.exists():
        if target.read_bytes() == content:
            report(f"Subtitle already at {target}; unchanged")
            return target
        if not force:
            raise JellyDubError(f"Subtitle output already exists: {target}; use --force to replace it")
    atomic_write_bytes(target, content)
    report(f"Saved subtitle: {target}")
    return target
