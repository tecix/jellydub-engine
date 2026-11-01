"""TTS provider interface. OWNED BY THE MANAGER — do not change without asking."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class TTSError(Exception):
    """Raised by providers. `transient=True` means a retry may succeed."""

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class TTSProvider(ABC):
    name: str = "base"
    # Upper bound for concurrent synthesize() calls (threads). Local GPU/CPU engines keep 1.
    max_concurrency: int = 1

    @abstractmethod
    def cache_id(self) -> str:
        """String identifying engine + voice + all settings that change output (used in cache keys)."""

    @abstractmethod
    def synthesize(self, text: str, out_path: Path, speaker: str | None = None) -> None:
        """Write speech for `text` to `out_path` (any format ffmpeg can read).

        `speaker` is an optional manual label (see Cue.speaker); providers without
        voice profiles ignore it.
        """

    def close(self) -> None:
        """Release models/resources (heavy local engines must free memory here)."""
