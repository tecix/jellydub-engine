"""Inspect exported audio without modifying it."""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

from .ffmpeg import duration
from .models import JellyDubError


def check_audio(path: Path, expected_duration: float, tolerance: float = 0.5) -> list[str]:
    """Return problems for missing, empty, invalid, mistimed or silent audio."""
    if not math.isfinite(expected_duration) or expected_duration < 0 or not math.isfinite(tolerance) or tolerance < 0:
        raise JellyDubError("Expected duration and tolerance must be finite and non-negative")
    if not path.is_file():
        return [f"Missing audio file: {path}"]
    if path.stat().st_size == 0:
        return [f"Empty audio file: {path}"]
    problems = []
    try:
        actual = duration(path)
    except JellyDubError as exc:
        return [f"Invalid audio: {exc}"]
    if not math.isfinite(actual) or abs(actual - expected_duration) > tolerance:
        problems.append(f"Audio duration mismatch: expected {expected_duration:.3f}s, got {actual:.3f}s")
    result = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    if result.returncode:
        problems.append(f"Cannot decode audio: {result.stderr.strip()}")
    else:
        match = re.search(r"max_volume:\s*(-?inf|[-\d.]+) dB", result.stderr)
        if match is None:
            problems.append("Could not measure audio volume")
        elif float(match.group(1)) <= -90:
            problems.append("Silent audio output (peak at or below -90 dBFS)")
    return problems
