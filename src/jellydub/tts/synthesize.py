"""Resumable per-cue speech synthesis and normalization."""
from __future__ import annotations

import os
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from pathlib import Path
from typing import Callable

from ..cache import cache_key, tmp_path_for
from ..ffmpeg import duration, run_ffmpeg
from ..models import Cue, SynthResult, SAMPLE_RATE
from .base import TTSError, TTSProvider


def _valid_duration(path):
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        value = duration(path)
        return value if value > 0 else None
    except Exception:
        return None


def _has_speakable_content(text: str) -> bool:
    """Ignore symbol-only cues and lines consisting entirely of annotations.

    This is only a classification check: spoken text and its cache key retain
    their original spelling, whitespace, and markers.
    """
    for line in text.splitlines():
        line = line.strip(" \t*♪♫#~−–—-")
        if re.fullmatch(r"(?:(?:\[[^\[\]]*\]|\([^()]*\))\s*)+", line):
            continue
        if any(char.isalpha() or char.isdigit() for char in line):
            return True
    return False


class _AdaptiveLimiter:
    """Limit new requests; already running calls finish when the limit shrinks."""

    def __init__(self, cap):
        self.cap = self.allowed = cap
        self.active = self.successes = 0
        self.condition = threading.Condition()

    def acquire(self):
        with self.condition:
            self.condition.wait_for(lambda: self.active < self.allowed)
            self.active += 1

    def release(self, *, success=False, transient=False):
        with self.condition:
            self.active -= 1
            if transient:
                self.allowed = max(1, self.allowed // 2)
                self.successes = 0
            elif success:
                self.successes += 1
                if self.successes >= 20:
                    self.allowed = min(self.cap, self.allowed + 1)
                    self.successes = 0
            else:
                self.successes = 0
            self.condition.notify_all()


def synthesize_cues(
    cues: list[Cue], provider: TTSProvider, cache_dir: Path, retries: int = 4,
    on_progress: Callable[[int, int], None] | None = None, *, workers: int = 1,
) -> tuple[list[SynthResult], list[tuple[Cue, str]]]:
    """Synthesize unique lines concurrently, returning results/failures in cue order.

    Progress runs only on the calling thread, once per completed cue (including
    duplicates and non-speech cues). Provider concurrency defaults to one for safety.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cap = max(1, min(workers, getattr(provider, "max_concurrency", 1)))
    limiter = _AdaptiveLimiter(cap)
    provider_id = provider.cache_id()
    groups = {}
    outcomes = {}
    completed = 0

    def progress(amount):
        nonlocal completed
        for _ in range(amount):
            completed += 1
            if on_progress:
                on_progress(completed, len(cues))

    for cue in cues:
        if _has_speakable_content(cue.text):
            groups.setdefault((cue.text, cue.speaker), []).append(cue)
        else:
            progress(1)

    def generate(key):
        text, speaker = key
        target = cache_dir / (cache_key(provider_id, text, speaker) + ".wav")
        raw = target.with_suffix(".raw.wav")
        temporary = tmp_path_for(target)
        measured = _valid_duration(target)
        if measured is not None:
            return target, measured, None
        for attempt in range(max(0, retries) + 1):
            success = transient = False
            error = None
            limiter.acquire()
            try:
                time.sleep(random.uniform(0.1, 0.3))
                provider.synthesize(text, raw, speaker=speaker)
                run_ffmpeg(["-i", str(raw), "-af",
                            "silenceremove=start_periods=1:start_duration=0.01:start_threshold=-50dB,"
                            "areverse,silenceremove=start_periods=1:start_duration=0.01:start_threshold=-50dB,areverse",
                            "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(temporary)])
                measured = _valid_duration(temporary)
                if measured is None:
                    raise TTSError("TTS produced missing or silent audio", transient=True)
                os.replace(temporary, target)
                success = True
            except Exception as exc:
                transient = isinstance(exc, TTSError) and exc.transient
                error = str(exc)
            finally:
                limiter.release(success=success, transient=transient)
                raw.unlink(missing_ok=True)
                temporary.unlink(missing_ok=True)
            if success:
                return target, measured, None
            if not transient or attempt >= max(0, retries):
                return target, None, error
            time.sleep(min(2 ** attempt, 8) + random.uniform(0, 1))

    with ThreadPoolExecutor(max_workers=cap) as executor:
        pending = {executor.submit(generate, key): key for key in groups}
        for future in as_completed(pending):
            key = pending[future]
            try:
                outcomes[key] = future.result()
            except Exception as exc:
                outcomes[key] = (None, None, str(exc))
            progress(len(groups[key]))

    results, failures = [], []
    for cue in cues:
        if not _has_speakable_content(cue.text):
            continue
        target, measured, error = outcomes[(cue.text, cue.speaker)]
        if error is None:
            results.append(SynthResult(cue.index, target, measured))
        else:
            failures.append((cue, error))
    return results, failures
