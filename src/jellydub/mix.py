"""FFmpeg audio extraction and mixing."""
from __future__ import annotations

import math
import os
from pathlib import Path

from .cache import tmp_path_for
from .ffmpeg import duration, run_ffmpeg
from .models import JellyDubError, MixMode, SAMPLE_RATE


def extract_audio(video: Path, out_wav: Path, stream_index: int = 0) -> None:
    """Extract audio stream `stream_index` (a:N) of video to 48 kHz stereo WAV."""
    if stream_index < 0:
        raise JellyDubError("Audio stream index must be non-negative")
    _write_audio(["-i", str(video), "-map", f"0:a:{stream_index}", "-vn"], out_wav, [video])


def _write_audio(args: list[str], output: Path, sources: list[Path]) -> None:
    if any(output.resolve() == source.resolve() for source in sources):
        raise JellyDubError("Audio output must not overwrite an input")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = tmp_path_for(output)
    try:
        run_ffmpeg([*args, "-ar", str(SAMPLE_RATE), "-ac", "2", "-c:a", "pcm_s16le", str(temporary)])
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def mix(
    mode: MixMode,
    dub_wav: Path,
    out_wav: Path,
    original_wav: Path | None = None,
    background_wav: Path | None = None,
    duck_db: float = -14.0,
    dub_gain_db: float = 0.0,
) -> None:
    """Mix to 48 kHz stereo WAV, retaining the longest input's duration.

    Voiceover uses smooth speech-triggered sidechain compression. Replacement adds
    the provided background stem; dub-only exports the dub alone.
    """
    try:
        mode = MixMode(mode)
    except ValueError as exc:
        raise JellyDubError(f"Unknown mix mode: {mode}") from exc
    if not all(math.isfinite(value) for value in (duck_db, dub_gain_db)):
        raise JellyDubError("Mix gains must be finite")
    if duck_db > 0:
        raise JellyDubError("Ducking level must be zero or negative dB")
    if mode == MixMode.DUB_ONLY:
        _write_audio(["-i", str(dub_wav), "-af", f"volume={dub_gain_db}dB"], out_wav, [dub_wav])
        return
    bed = original_wav if mode == MixMode.VOICEOVER else background_wav
    if bed is None:
        raise JellyDubError(f"{mode.value} requires {'original audio' if mode == MixMode.VOICEOVER else 'a separated background'}")
    length = max(duration(dub_wav), duration(bed))
    # Padding prevents the sidechain's shorter input from ending the bed early.
    graph = (f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,apad,atrim=duration={length}[bed];"
             f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={dub_gain_db}dB,apad,atrim=duration={length}[dub];")
    if mode == MixMode.VOICEOVER and duck_db < 0:
        # Mix a fixed floor with a compressed remainder to bound attenuation.
        floor = 10 ** (duck_db / 20)
        graph += (f"[bed]asplit=2[floor][compress];[floor]volume={floor}[low];"
                  f"[compress]volume={1-floor}[high];[dub]asplit=2[voice][key];"
                  "[high][key]sidechaincompress=threshold=0.01:ratio=20:attack=20:release=300[duck];"
                  "[low][duck][voice]amix=inputs=3:duration=longest:normalize=0[out]")
    else:
        graph += "[bed][dub]amix=inputs=2:duration=longest:normalize=0[out]"
    _write_audio(["-i", str(bed), "-i", str(dub_wav), "-filter_complex", graph, "-map", "[out]", "-t", str(length)], out_wav, [bed, dub_wav])
