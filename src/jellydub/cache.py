"""Filesystem cache helpers. OWNED BY THE MANAGER — shared by all modules."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def cache_key(*parts: object) -> str:
    """Stable sha256 hex digest (first 16 chars) of the JSON-encoded parts."""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def tmp_path_for(path: Path) -> Path:
    """Temp sibling path to write into, then os.replace() onto `path` (keeps the suffix for ffmpeg)."""
    return path.with_name(f"{path.stem}.part{path.suffix}")
