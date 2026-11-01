"""Local VieNeu v3 Turbo SDK adapter with reusable manual voice profiles."""
from __future__ import annotations

import hashlib
import json
import os
import warnings
from pathlib import Path

from ..cache import cache_key, tmp_path_for
from ..ffmpeg import duration
from .base import TTSError, TTSProvider


class VieNeuTTS(TTSProvider):
    name = "vieneu"
    max_concurrency = 1

    def __init__(self, voice="Hải Đăng", reference=None, speaker_refs=None,
                 cache_dir=None, device="cpu"):
        self.voice = voice
        self.reference = Path(reference) if reference else None
        self.speaker_refs = {key: Path(value) for key, value in (speaker_refs or {}).items()}
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache/jellydub/voices"
        self.device = device
        self._model = None
        self._profiles = {}

    @staticmethod
    def _fingerprint(path):
        if path is None:
            return None
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return f"missing:{path}"

    def cache_id(self):
        return "vieneu-v3turbo:" + cache_key(
            self.voice, self.device, self._fingerprint(self.reference),
            {k: self._fingerprint(v) for k, v in self.speaker_refs.items()})

    def _load(self):
        if self._model is None:
            from vieneu import Vieneu
            self._model = Vieneu(mode="v3turbo", device=self.device)
        return self._model

    def _voice(self, model, speaker):
        ref = self.speaker_refs.get(speaker) if speaker else self.reference
        if speaker and ref is None:
            warnings.warn(f"No reference for speaker {speaker!r}; using preset {self.voice}.", stacklevel=2)
            return self.voice
        if ref is None:
            return self.voice
        try:
            valid = ref.is_file() and 3 <= duration(ref) <= 8
        except Exception:
            valid = False
        if not valid:
            warnings.warn(f"Reference {ref} must be a readable 3–8 second clip; using preset {self.voice}.", stacklevel=2)
            return self.voice
        key = cache_key("v3turbo", self._fingerprint(ref))
        if key in self._profiles:
            return self._profiles[key]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        profile_path = self.cache_dir / f"{key}.json"
        profile = None
        if profile_path.exists():
            try:
                import numpy as np  # provided by the optional SDK, never imported for Edge
                data = json.loads(profile_path.read_text(encoding="utf-8"))["presets"][key]
                data["speaker_emb"] = np.asarray(data["speaker_emb"], dtype=np.float32)
                if data.get("codes") is not None:
                    data["codes"] = np.asarray(data["codes"], dtype=np.int64)
                profile = data
            except (ValueError, KeyError, TypeError):
                pass
        if profile is None:
            model.add_voice(key, str(ref))
            temporary = tmp_path_for(profile_path)
            try:
                model.save_voices(str(temporary))
                os.replace(temporary, profile_path)
            finally:
                temporary.unlink(missing_ok=True)
            profile = model.get_preset_voice(key)
        self._profiles[key] = profile
        return profile

    def synthesize(self, text: str, out_path: Path, speaker=None):
        try:
            model = self._load()
            audio = model.infer(text, voice=self._voice(model, speaker))
            model.save(audio, str(out_path))
        except Exception as exc:
            raise TTSError(f"VieNeu-TTS failed (install the vieneu extra for local synthesis): {exc}") from exc

    def close(self):
        model, self._model = self._model, None
        self._profiles.clear()
        if model is not None:
            model.close()
