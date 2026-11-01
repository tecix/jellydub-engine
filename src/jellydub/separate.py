"""Optional local music/effects separation using audio-separator."""
from __future__ import annotations

import os
from pathlib import Path

from .cache import cache_key, tmp_path_for
from .ffmpeg import duration, run_ffmpeg
from .models import JellyDubError

DEFAULT_MODEL = "mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt"


def separate_background(source_audio: Path, work_dir: Path, model: str | None = None, device: str = "auto") -> Path:
    """Return cached stereo background WAV; failures suggest explicit voiceover fallback."""
    hint = "Use --mix voiceover instead."
    try:
        if device not in {"auto", "cpu", "cuda", "mps"}:
            raise ValueError(f"Unsupported separation device: {device}")
        stat = source_audio.stat()
        selected = model or DEFAULT_MODEL
        key = cache_key("background-v1", str(source_audio.resolve()), stat.st_size, stat.st_mtime_ns, selected, device)
        folder = work_dir / f"separation-{key}"
        output = folder / "background.wav"
        if output.is_file() and output.stat().st_size > 0:
            try:
                if abs(duration(output) - duration(source_audio)) <= 0.5:
                    return output
            except JellyDubError:
                pass
        from audio_separator.separator import Separator
        folder.mkdir(parents=True, exist_ok=True)
        separator = Separator(output_dir=str(folder.resolve()), model_file_dir=str((work_dir / "separation-models").resolve()), output_format="WAV", output_single_stem="Instrumental")
        if device != "auto":
            import torch
            separator.torch_device = torch.device(device)
            separator.onnx_execution_provider = ["CUDAExecutionProvider"] if device == "cuda" else ["CPUExecutionProvider"]
        separator.load_model(model_filename=selected)
        files = separator.separate(str(source_audio.resolve()))
        candidates = [Path(p) if Path(p).is_absolute() else folder.resolve() / p for p in files]
        # Explicit stem identification prevents accidentally exporting isolated dialogue.
        stems = [p for p in candidates if "instrumental" in p.stem.lower() or "background" in p.stem.lower()]
        if len(stems) != 1 or not stems[0].is_file():
            raise ValueError("Separator did not return exactly one instrumental/background stem")
        temporary = tmp_path_for(output)
        try:
            run_ffmpeg(["-i", str(stems[0]), "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(temporary)])
            if abs(duration(temporary) - duration(source_audio)) > 0.5:
                raise ValueError("Separated background duration differs from source")
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return output
    except ImportError as exc:
        raise JellyDubError(f"Separation requires the optional 'separate' extra. {hint}") from exc
    except Exception as exc:
        raise JellyDubError(f"Background separation failed: {exc}. {hint}") from exc
