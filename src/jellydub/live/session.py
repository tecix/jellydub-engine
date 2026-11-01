"""One live dubbing session: playhead-prioritised translation + TTS + tempo fitting."""
from __future__ import annotations

import os
import shutil
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..cache import tmp_path_for
from ..ffmpeg import run_ffmpeg
from ..models import Cue
from ..timing import align
from ..tts.synthesize import _has_speakable_content, synthesize_cues

OUT_RATE = 24_000
AHEAD_WINDOW = (-2.0, 90.0)   # seconds around the playhead that are always served first
CHUNK = 6                     # cues translated per call (first audio within seconds)
CONTEXT = 3                   # preceding cues sent along for translation context
MAX_BACKLOG = 8               # translated-but-unspoken cues allowed before translation pauses
MAX_TTS_FAILURES = 5          # consecutive failures with no success: surface an error


@dataclass
class Engine:
    """Shared, per-server pieces a session needs."""

    provider: object
    translate: Callable[[list[Cue]], list[Cue]]   # source cues (+context) -> translated cues
    tts_dir: Path
    max_tempo: float = 1.35
    workers: int = 4
    translate_targets: Callable[[list[Cue], int, int, dict[int, str]], list[Cue]] | None = None


class Session:
    def __init__(self, session_id: str, cues: list[Cue], source_lang: str, engine: Engine,
                 work_dir: Path, title: str | None = None, *, defer_start: bool = False,
                 on_demand: bool = False):
        self.id = session_id
        self.source_lang = source_lang
        self.title = title
        self.engine = engine
        self.work = work_dir
        self.work.mkdir(parents=True, exist_ok=True)
        self.cues = [replace(cue, index=pos) for pos, cue in enumerate(sorted(cues, key=lambda c: c.start))]
        self.cond = threading.Condition()
        self.last_access = time.monotonic()
        self.playhead = 0.0
        self._waiting_for_playhead = defer_start
        self.text: dict[int, str] = {}
        self.clips: dict[int, tuple[Path, float]] = {}
        self.failed: dict[int, str] = {}
        self.requested: set[int] | None = set() if on_demand else None
        self._speech_locks: dict[str, threading.Lock] = {}
        # Cues with nothing to say (annotations) or whose synthesis failed never get audio.
        self.skipped = {c.index for c in self.cues if not _has_speakable_content(c.text)}
        self.inflight: set[int] = set()
        self.translating = False
        self.error: str | None = None
        self.failures = self.successes = 0
        self.closed = False
        if source_lang == "vi":
            self.text = {c.index: c.text for c in self.cues}
        workers = max(1, min(engine.workers, getattr(engine.provider, "max_concurrency", 1)))
        self.workers = workers
        self.tts_pool = ThreadPoolExecutor(workers, thread_name_prefix=f"dub-tts-{session_id[:6]}")
        self.tr_pool = ThreadPoolExecutor(1, thread_name_prefix=f"dub-tr-{session_id[:6]}")
        self.thread = threading.Thread(target=self._run, name=f"dub-{session_id[:6]}", daemon=True)
        self.thread.start()

    # ---- public API -------------------------------------------------------------------------

    def touch(self) -> None:
        self.last_access = time.monotonic()

    def set_playhead(self, seconds: float) -> None:
        with self.cond:
            if self._waiting_for_playhead or seconds != self.playhead:
                self._waiting_for_playhead = False
                self.playhead = seconds
                self.cond.notify_all()

    def view(self, start: float, end: float) -> dict:
        """State, ready_until and ready cues in [start, end]."""
        with self.cond:
            out = []
            ready_until = None
            for cue in self.cues:
                if cue.start >= start and ready_until is None and not self._finished(cue.index):
                    ready_until = cue.start
                if start <= cue.start <= end and cue.index in self.clips:
                    out.append({"i": cue.index, "start": round(cue.start, 3), "end": round(cue.end, 3),
                                "text": self.text[cue.index], "duration": round(self.clips[cue.index][1], 3),
                                "audio": f"/v1/sessions/{self.id}/audio/{cue.index}.wav"})
            if ready_until is None:
                ready_until = self.cues[-1].end
            if self.error:
                state = "error"
            else:
                state = "ready" if ready_until == self.cues[-1].end and self._all_finished_from(start) else "working"
            return {"state": state, "error": self.error, "ready_until": round(ready_until, 3), "cues": out}

    def clip_path(self, pos: int) -> Path | None:
        with self.cond:
            clip = self.clips.get(pos)
            return clip[0] if clip else None

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()
        self.tts_pool.shutdown(wait=False, cancel_futures=True)
        self.tr_pool.shutdown(wait=False, cancel_futures=True)
        shutil.rmtree(self.work, ignore_errors=True)

    # ---- scheduling -------------------------------------------------------------------------

    def _finished(self, pos: int) -> bool:
        return pos in self.clips or pos in self.skipped

    def _all_finished_from(self, start: float) -> bool:
        return all(self._finished(c.index) for c in self.cues if c.start >= start)

    def _priority(self) -> list[int]:
        """Cue positions: playhead window first, then ahead, then behind (rarely needed)."""
        lo, hi = self.playhead + AHEAD_WINDOW[0], self.playhead + AHEAD_WINDOW[1]

        def rank(cue: Cue):
            if lo <= cue.start <= hi:
                return 0, cue.start
            return (1, cue.start) if cue.start > hi else (2, cue.start)
        return [c.index for c in sorted(self.cues, key=rank)
                if not self._finished(c.index) and (self.requested is None or c.index in self.requested)]

    def _run(self) -> None:
        with self.cond:
            while not self.closed:
                if not self.error:
                    self._schedule()
                self.cond.wait(0.5)

    def _schedule(self) -> None:
        if self._waiting_for_playhead:
            return
        order = self._priority()
        for pos in order:
            if len(self.inflight) >= self.workers:
                break
            if pos in self.text and pos not in self.inflight:
                self.inflight.add(pos)
                self.tts_pool.submit(self._speak, pos)
        # Only work ahead of the next translation in the current priority order
        # should hold it back. After a seek, old translated cues rank behind the
        # new playhead and must not delay translation there.
        backlog = 0
        for pos in order:
            if pos not in self.text:
                if not self.translating and backlog < MAX_BACKLOG:
                    self.translating = True
                    self.tr_pool.submit(self._translate, pos)
                break
            backlog += 1

    def _fail(self, message: str) -> None:
        self.error = message
        self.cond.notify_all()

    def _translate(self, first: int) -> None:
        try:
            with self.cond:
                if self.closed:
                    return
                known_text = dict(self.text)
            lo = max(0, first - CONTEXT)
            end = min(len(self.cues), first + CHUNK)
            if self.engine.translate_targets is not None:
                translated = self.engine.translate_targets(self.cues, first, end, known_text)
            else:
                translated = self.engine.translate(self.cues[lo:end])[first - lo:]
            with self.cond:
                if self.closed:
                    return
                for cue in translated:
                    self.text[cue.index] = cue.text
                    if not _has_speakable_content(cue.text):
                        self.skipped.add(cue.index)
        except Exception as exc:
            with self.cond:
                if not self.closed:
                    self._fail(f"Translation failed: {exc}")
        finally:
            with self.cond:
                self.translating = False
                self.cond.notify_all()

    def _speak(self, pos: int) -> None:
        try:
            if self.closed:
                return
            path, length = self._synthesize(pos)
            with self.cond:
                if self.closed:
                    return
                self.clips[pos] = (path, length)
                self.successes += 1
                self.failures = 0
        except Exception as exc:
            with self.cond:
                if self.closed:
                    return
                self.skipped.add(pos)
                self.failed[pos] = str(exc)
                self.failures += 1
                if self.failures >= MAX_TTS_FAILURES and not self.successes:
                    self._fail(f"Speech synthesis failed: {exc}")
        finally:
            with self.cond:
                self.inflight.discard(pos)
                self.cond.notify_all()
                closed = self.closed
            # A running synthesis can recreate its output directory after close()
            # removes it. Discard these late files as well as the late result.
            if closed:
                shutil.rmtree(self.work, ignore_errors=True)

    def _synthesize(self, pos: int) -> tuple[Path, float]:
        cue = replace(self.cues[pos], text=self.text[pos], speaker=None)
        # Repeated dialogue shares synthesis/alignment paths. Serialize identical
        # text so concurrent cues cannot replace or delete one another's temps.
        with self.cond:
            lock = self._speech_locks.setdefault(cue.text, threading.Lock())
        with lock:
            if self.closed:
                raise RuntimeError("Session is closed")
            return self._synthesize_clip(pos, cue)

    def _synthesize_clip(self, pos: int, cue: Cue) -> tuple[Path, float]:
        results, failures = synthesize_cues([cue], self.engine.provider, self.engine.tts_dir, retries=2)
        if failures:
            raise RuntimeError(failures[0][1])
        # Tempo fitting against the real neighbours: the other cues only supply slot boundaries.
        placed = align(self.cues, results, self.work / "aligned", max_tempo=self.engine.max_tempo)[0]
        target = self.work / f"{pos}.wav"
        temporary = tmp_path_for(target)
        try:
            run_ffmpeg(["-i", str(placed.path), "-ar", str(OUT_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(temporary)])
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        with wave.open(str(target), "rb") as clip:
            return target, clip.getnframes() / clip.getframerate()
