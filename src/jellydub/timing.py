"""Alignment of synthesized speech to subtitle timing. OWNER: agent-c (timing)."""

from __future__ import annotations

import math
import os
import wave
from pathlib import Path

from .cache import cache_key, tmp_path_for
from .ffmpeg import duration, run_ffmpeg
from .models import SAMPLE_RATE, Cue, JellyDubError, PlacedClip, SynthResult

_GAP = 0.01


def _timing_warning(index, drift, overflow, tempo, max_overhang, threshold):
    def noticeable(value):
        return value > 0 and (value >= threshold or math.isclose(value, threshold, abs_tol=1e-12))

    warnings = []
    if noticeable(drift):
        warnings.append(f"cue {index} shifted by {drift:.3f}s to avoid overlap")
    if noticeable(overflow):
        warnings.append(f"cue {index} overflow {overflow:.3f}s at tempo {tempo:.3f}")
    if warnings and max(drift, overflow) > max_overhang + 0.000001:
        warnings.append(f"exceeds max_overhang {max_overhang:.3f}s; preserving speech")
    return "; ".join(warnings) or None


def plan_alignment(
    cues: list[Cue], results: list[SynthResult], max_tempo: float = 1.25,
    max_overhang: float = 0.6, *, warn_threshold: float = 0.02,
) -> list[PlacedClip]:
    """Pure timing plan; impossible drift bounds are reported without cutting speech.

    Missing synthesis results are skipped. Cues are ordered by timestamp, with input
    order breaking ties. The final cue uses its own end as its available slot.
    """
    if not math.isfinite(max_tempo) or max_tempo < 1:
        raise JellyDubError("max_tempo must be finite and at least 1")
    if not math.isfinite(max_overhang) or max_overhang < 0:
        raise JellyDubError("max_overhang must be finite and nonnegative")
    if not math.isfinite(warn_threshold) or warn_threshold < 0:
        raise JellyDubError("warn_threshold must be finite and nonnegative")
    by_index = {r.cue_index: r for r in results}
    ordered = sorted(cues, key=lambda c: c.start)
    placed = []
    previous_end = -_GAP
    for pos, cue in enumerate(ordered):
        if not all(math.isfinite(t) for t in (cue.start, cue.end)) or cue.start < 0 or cue.end < cue.start:
            raise JellyDubError(f"Invalid timestamps for cue {cue.index}")
        result = by_index.get(cue.index)
        if result is None:
            continue
        if not math.isfinite(result.duration) or result.duration <= 0:
            raise JellyDubError(f"Invalid speech duration for cue {cue.index}")
        start = max(cue.start, previous_end + _GAP)
        boundary = ordered[pos + 1].start if pos + 1 < len(ordered) else cue.end
        slot = max(0.0, boundary - start - (_GAP if pos + 1 < len(ordered) else 0))
        tempo = min(max_tempo, max(1.0, result.duration / slot)) if slot else max_tempo
        length = result.duration / tempo
        warning = _timing_warning(
            cue.index, start - cue.start, max(0.0, start + length - boundary),
            tempo, max_overhang, warn_threshold,
        )
        placed.append(PlacedClip(cue.index, result.path, start, length, tempo, warning))
        previous_end = start + length
    return placed


def align(
    cues: list[Cue],
    results: list[SynthResult],
    work_dir: Path,
    max_tempo: float = 1.25,
    max_overhang: float = 0.6,
    fade_ms: int = 15,
    *, warn_threshold: float = 0.02,
) -> list[PlacedClip]:
    """Place each clip at its cue start. Available slot = until next cue start (not just cue end).
    If speech is longer than the slot: speed up with ffmpeg atempo (pitch preserved) up to max_tempo;
    if still too long, allow overhang up to max_overhang seconds, else shift/keep and set a warning.
    Never truncate speech. Apply fade in/out. Overlapping cues must not produce overlapping clips.
    Warn only when total shift or overflow reaches warn_threshold seconds.
    Outputs cached in work_dir (deterministic names) so reruns are cheap."""
    if not math.isfinite(fade_ms) or fade_ms < 0:
        raise JellyDubError("fade_ms must be nonnegative")
    plan = plan_alignment(cues, results, max_tempo, max_overhang, warn_threshold=warn_threshold)
    ordered = sorted(cues, key=lambda c: c.start)
    timing = {
        cue.index: (cue.start, ordered[pos + 1].start if pos + 1 < len(ordered) else cue.end)
        for pos, cue in enumerate(ordered)
    }
    work_dir.mkdir(parents=True, exist_ok=True)
    previous_end = -_GAP
    for clip in plan:
        source = clip.path.resolve()
        try:
            stat = source.stat()
        except OSError as exc:
            raise JellyDubError(f"Could not read speech for cue {clip.cue_index}: {exc}") from exc
        key = cache_key("alignment-v1", str(source), stat.st_size, stat.st_mtime_ns, clip.tempo, fade_ms)
        target = work_dir / f"aligned-{key}.wav"
        if not target.exists():
            fade = min(fade_ms / 1000, clip.duration / 2)
            filters = [f"atempo={clip.tempo:.12g}"]
            if fade:
                # Reverse twice to fade the true end despite atempo sample rounding.
                filters.extend([f"afade=t=in:d={fade:.9f}", "areverse", f"afade=t=in:d={fade:.9f}", "areverse"])
            temporary = tmp_path_for(target)
            try:
                run_ffmpeg(["-i", str(source), "-af", ",".join(filters), "-ar", str(SAMPLE_RATE),
                            "-ac", "1", "-c:a", "pcm_s16le", str(temporary)])
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        clip.path = target
        clip.duration = duration(target)
        # atempo may differ by a few samples from the estimated duration.
        clip.start = max(clip.start, previous_end + _GAP)
        cue_start, boundary = timing[clip.cue_index]
        clip.warning = _timing_warning(
            clip.cue_index, clip.start - cue_start,
            max(0.0, clip.start + clip.duration - boundary),
            clip.tempo, max_overhang, warn_threshold,
        )
        previous_end = clip.start + clip.duration
    return plan


def render_timeline(clips: list[PlacedClip], total_duration: float, out_wav: Path) -> None:
    """Render clips onto a silent 48 kHz mono track of exactly total_duration seconds.

    Duration is rounded to the nearest sample. Stream PCM in bounded chunks so long
    movies and thousands of clips require no large filter graph or in-memory mix.
    Reject overlapping or out-of-bounds speech rather than silently truncating it.
    """
    if not math.isfinite(total_duration) or total_duration <= 0:
        raise JellyDubError("Timeline duration must be finite and positive")
    total_frames = round(total_duration * SAMPLE_RATE)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    temporary = tmp_path_for(out_wav)
    cursor = 0
    silence = b"\0" * (SAMPLE_RATE * 2)
    try:
        with wave.open(str(temporary), "wb") as output:
            output.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            for clip in sorted(clips, key=lambda c: c.start):
                if not math.isfinite(clip.start) or clip.start < 0:
                    raise JellyDubError(f"Invalid placement for cue {clip.cue_index}")
                if clip.path.resolve() == out_wav.resolve():
                    raise JellyDubError("Timeline output must differ from clip inputs")
                start = round(clip.start * SAMPLE_RATE)
                with wave.open(str(clip.path), "rb") as source:
                    if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
                        raise JellyDubError(f"Cue {clip.cue_index} must be 48 kHz mono PCM s16le WAV")
                    if start < cursor:
                        raise JellyDubError(f"Cue {clip.cue_index} overlaps previous speech; run alignment first")
                    if start + source.getnframes() > total_frames:
                        raise JellyDubError(f"Cue {clip.cue_index} exceeds source duration; shorten the dialogue before rendering")
                    while cursor < start:
                        count = min(SAMPLE_RATE, start - cursor)
                        output.writeframesraw(silence[:count * 2])
                        cursor += count
                    while data := source.readframes(SAMPLE_RATE):
                        output.writeframesraw(data)
                        cursor += len(data) // 2
            while cursor < total_frames:
                count = min(SAMPLE_RATE, total_frames - cursor)
                output.writeframesraw(silence[:count * 2])
                cursor += count
        os.replace(temporary, out_wav)
    except (OSError, wave.Error) as exc:
        raise JellyDubError(f"Could not render timeline: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
