"""Shared data types. OWNED BY THE MANAGER — do not change signatures without asking."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

SAMPLE_RATE = 48_000  # all intermediate WAVs: 48 kHz, mono, s16le


class JellyDubError(Exception):
    """User-facing error; the CLI prints the message without a traceback."""


@dataclass
class Cue:
    """One subtitle entry. Times are seconds (float) relative to media start."""

    index: int
    start: float
    end: float
    text: str
    speaker: str | None = None  # manual speaker / voice-profile label (VieNeu)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SynthResult:
    """Raw TTS output for one cue (48 kHz mono WAV, leading/trailing silence trimmed)."""

    cue_index: int
    path: Path
    duration: float


@dataclass
class PlacedClip:
    """A clip after alignment, ready to be placed on the timeline."""

    cue_index: int
    path: Path          # final (possibly time-stretched + faded) WAV
    start: float        # timeline position in seconds
    duration: float     # duration of `path` in seconds
    tempo: float = 1.0  # applied atempo factor (1.0 = untouched)
    warning: str | None = None  # e.g. overflow that could not be fixed


class MixMode(str, Enum):
    DUB_ONLY = "dub-only"
    VOICEOVER = "voiceover"
    REPLACE = "replace"
