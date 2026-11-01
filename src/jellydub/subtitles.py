"""SRT parsing/writing. OWNER: agent-a (subs)."""
from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import srt

from .cache import atomic_write_text
from .models import Cue, JellyDubError


def parse_srt(path: Path) -> list[Cue]:
    """Read SRT, strip formatting, extract speakers, and sort by start time."""
    try:
        data = Path(path).read_bytes()
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252")
        result = []
        for item in srt.parse(text):
            content = re.sub(r"<[^>]*>|\{\\[^}]*\}", "", item.content)
            content = " ".join(content.split())
            speaker = None
            marker = re.match(r"^\[\[([^\]]+)\]\]\s*", content)
            name = re.match(r"^([A-ZÀ-Ỹ][A-ZÀ-Ỹ0-9 _.'’-]*):\s*", content)
            match = marker or name
            if match:
                speaker = match.group(1).strip()
                content = content[match.end():]
            start, end = item.start.total_seconds(), item.end.total_seconds()
            if start < 0 or end <= start:
                raise ValueError(f"Invalid timestamps for cue {item.index}")
            result.append(Cue(item.index, start, end, content, speaker))
        if not result:
            raise ValueError("no subtitle cues")
        return sorted(result, key=lambda cue: cue.start)
    except (OSError, UnicodeError, ValueError, srt.SRTParseError) as exc:
        raise JellyDubError(f"Cannot read subtitles {path}: {exc}") from exc


def write_srt(cues: list[Cue], path: Path) -> None:
    """Atomically write original indices/times; render speakers as readable NAME: prefixes.

    Mixed-case labels use explicit markers so they also round-trip unambiguously.
    """
    items = []
    for cue in cues:
        prefix = ""
        if cue.speaker:
            prefix = (f"{cue.speaker}: " if re.fullmatch(r"[A-ZÀ-Ỹ][A-ZÀ-Ỹ0-9 _.'’-]*", cue.speaker)
                      else f"[[{cue.speaker}]] ")
        items.append(srt.Subtitle(cue.index, timedelta(seconds=cue.start),
                                  timedelta(seconds=cue.end), prefix + cue.text))
    try:
        atomic_write_text(Path(path), srt.compose(items, reindex=False))
    except OSError as exc:
        raise JellyDubError(f"Cannot write subtitles {path}: {exc}") from exc
