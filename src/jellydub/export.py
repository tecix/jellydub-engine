"""Atomic Jellyfin-compatible audio and Matroska exports."""
from __future__ import annotations

import os
from pathlib import Path

from .cache import tmp_path_for
from .ffmpeg import probe, run_ffmpeg
from .models import JellyDubError


def output_paths(video: Path, out_dir: Path | None = None, label: str = "AI-Dub") -> dict[str, Path]:
    """Return Vietnamese subtitle, external MP3 and optional MKV destinations."""
    if not label or any(c in label for c in '/\\\x00') or label in {'.', '..'}:
        raise JellyDubError("Output label must be a non-empty filename component")
    folder = out_dir if out_dir is not None else video.parent
    paths = {"srt": folder / f"{video.stem}.vi.srt", "audio": folder / f"{video.stem}.{label}.vi.mp3", "mkv": folder / f"{video.stem}.{label}.vi.mkv"}
    if any(p.resolve() == video.resolve() for p in paths.values()):
        raise JellyDubError("Export path would overwrite the source")
    return paths


def _export(args: list[str], output: Path, inputs: list[Path]) -> None:
    if any(output.resolve() == p.resolve() for p in inputs):
        raise JellyDubError("Export must not overwrite an input")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = tmp_path_for(output)
    try:
        run_ffmpeg([*args, str(temporary)])
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def export_mp3(mixed_wav: Path, out_mp3: Path, bitrate: str = "192k") -> None:
    """Encode stereo MP3 atomically."""
    _export(["-i", str(mixed_wav), "-map", "0:a:0", "-vn", "-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-b:a", bitrate], out_mp3, [mixed_wav])


def mux_mkv(video: Path, dub_audio: Path, out_mkv: Path, title: str = "Tiếng Việt (AI Dub)") -> None:
    """Copy all original streams and append an AAC Vietnamese audio track."""
    count = sum(s.get("codec_type") == "audio" for s in probe(video)["streams"])
    _export(["-i", str(video), "-i", str(dub_audio), "-map", "0", "-map", "1:a:0", "-map_metadata", "0", "-map_chapters", "0", "-c", "copy", f"-c:a:{count}", "aac", f"-b:a:{count}", "192k", f"-metadata:s:a:{count}", "language=vie", f"-metadata:s:a:{count}", f"title={title}", f"-disposition:a:{count}", "0"], out_mkv, [video, dub_audio])
