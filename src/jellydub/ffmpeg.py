"""Thin FFmpeg/ffprobe helpers. OWNED BY THE MANAGER — shared by all modules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .models import JellyDubError


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise JellyDubError(f"{tool} not found. Install it with: sudo apt install ffmpeg")


def run_ffmpeg(args: list[str]) -> None:
    """Run ffmpeg with -y -hide_banner -loglevel error; raise JellyDubError on failure."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise JellyDubError(f"ffmpeg failed: {' '.join(cmd)}\n{proc.stderr.strip()}")


def probe(path: Path) -> dict:
    """Return ffprobe JSON (format + streams)."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise JellyDubError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def duration(path: Path) -> float:
    """Media duration in seconds."""
    info = probe(path)
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError) as exc:
        raise JellyDubError(f"Could not read duration of {path}") from exc
