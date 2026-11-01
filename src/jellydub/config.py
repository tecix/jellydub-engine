"""TOML configuration with explicit CLI overrides."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
import tomllib

from .models import JellyDubError


@dataclass
class Config:
    source_lang: str = "auto"
    translator: str = "opus-mt"
    translator_model: str | None = None
    tts: str = "edge"
    tts_workers: int = 4
    voice: str | None = None
    rate: str = "+0%"
    pitch: str = "+0Hz"
    reference: list[str] | None = None
    mix: str = "dub-only"
    mkv: bool = False
    out_dir: Path | None = None
    cache_dir: Path = Path("~/.cache/jellydub")
    audio_stream: int = 0
    fallback_voiceover: bool = False
    device: str = "auto"
    max_tempo: float = 1.25
    subs: str = "auto"
    download: bool = True
    search_online: bool = False
    providers: list[str] | None = None
    subtitle_credentials_env: dict[str, str] = field(default_factory=lambda: {
        "username": "OPENSUBTITLES_USERNAME", "password": "OPENSUBTITLES_PASSWORD",
        "apikey": "OPENSUBTITLES_APIKEY",
    })

    def validate(self) -> None:
        for key, choices in (("source_lang", {"auto", "en", "vi"}), ("tts", {"edge", "vieneu"}),
                             ("mix", {"dub-only", "voiceover", "replace"}),
                             ("translator", {"opus-mt", "agy", "agy-stream", "gemini"}),
                             ("subs", {"auto", "local", "download", "translate"})):
            if getattr(self, key) not in choices:
                raise JellyDubError(f"Invalid {key}: {getattr(self, key)!r}; choose {', '.join(sorted(choices))}")
        if type(self.tts_workers) is not int or self.tts_workers < 1:
            raise JellyDubError("tts_workers must be a positive integer")
        if type(self.audio_stream) is not int or self.audio_stream < 0:
            raise JellyDubError("audio_stream must be a non-negative integer")
        if not isinstance(self.max_tempo, (float, int)) or not 1 <= self.max_tempo <= 2:
            raise JellyDubError("max_tempo must be between 1 and 2")
        for key in ("mkv", "fallback_voiceover", "download", "search_online"):
            if type(getattr(self, key)) is not bool:
                raise JellyDubError(f"{key} must be a boolean")
        for key in ("rate", "pitch", "device"):
            if not isinstance(getattr(self, key), str):
                raise JellyDubError(f"{key} must be a string")
        if self.translator_model is not None and (
                not isinstance(self.translator_model, str) or not self.translator_model.strip()):
            raise JellyDubError("translator_model must be a nonempty string")
        if self.voice is not None and not isinstance(self.voice, str):
            raise JellyDubError("voice must be a string")
        if self.reference is not None and (not isinstance(self.reference, list) or
                                          any(not isinstance(v, str) for v in self.reference)):
            raise JellyDubError("reference must be an array of paths or LABEL=PATH strings")
        if self.providers is not None and (not isinstance(self.providers, list) or not self.providers or
                any(not isinstance(v, str) or not v.strip() for v in self.providers)):
            raise JellyDubError("providers must be a nonempty array of provider names")
        import re
        if (not isinstance(self.subtitle_credentials_env, dict) or
                any(k not in {"username", "password", "apikey"} or not isinstance(v, str) or
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v)
                    for k, v in self.subtitle_credentials_env.items())):
            raise JellyDubError("subtitle_credentials_env must map username/password/apikey to environment variable names")
        self.cache_dir = Path(self.cache_dir).expanduser()
        if self.out_dir is not None:
            self.out_dir = Path(self.out_dir).expanduser()


def load_config(path: Path | None = None, **overrides) -> Config:
    """Load [jellydub] TOML settings; non-None CLI values take precedence."""
    values = {}
    if path is not None:
        try:
            with path.open("rb") as handle:
                document = tomllib.load(handle)
            values = document.get("jellydub", document)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise JellyDubError(f"Cannot read config {path}: {exc}") from exc
        if not isinstance(values, dict):
            raise JellyDubError("[jellydub] must be a TOML table")
    known = {f.name for f in fields(Config)}
    unknown = (set(values) | set(overrides)) - known
    if unknown:
        raise JellyDubError(f"Unknown configuration options: {', '.join(sorted(unknown))}")
    values = {**values, **{k: v for k, v in overrides.items() if v is not None}}
    try:
        config = Config(**values)
        config.validate()
    except (TypeError, ValueError) as exc:
        raise JellyDubError(f"Invalid configuration: {exc}") from exc
    return config
