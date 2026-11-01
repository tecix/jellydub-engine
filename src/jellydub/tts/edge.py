"""Microsoft Edge's online TTS service (no voice cloning)."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..cache import cache_key
from .base import TTSError, TTSProvider


class EdgeTTS(TTSProvider):
    name = "edge"
    max_concurrency = 8

    def __init__(self, voice="vi-VN-HoaiMyNeural", rate="+0%", pitch="+0Hz", volume="+0%"):
        self.voice, self.rate, self.pitch, self.volume = voice, rate, pitch, volume

    def cache_id(self):
        return "edge:" + cache_key(self.voice, self.rate, self.pitch, self.volume)

    def synthesize(self, text: str, out_path: Path, speaker=None):
        async def generate():
            import edge_tts
            await edge_tts.Communicate(text, self.voice, rate=self.rate,
                                       pitch=self.pitch, volume=self.volume).save(str(out_path))
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(generate())
            else:
                # The provider is synchronous, including when called from an async CLI host.
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(lambda: asyncio.run(generate())).result()
        except Exception as exc:
            status = getattr(exc, "status", None)
            transient = (isinstance(exc, (OSError, TimeoutError)) or
                         type(exc).__name__ in {"NoAudioReceived", "ClientConnectionError", "ServerDisconnectedError", "ClientPayloadError", "ServerTimeoutError"} or
                         (isinstance(status, int) and (status >= 500 or status in {408, 429})))
            raise TTSError(f"Edge-TTS failed: {exc}", transient=transient) from exc
