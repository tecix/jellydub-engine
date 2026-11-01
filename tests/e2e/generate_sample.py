"""Generate a reproducible 15-second synthetic movie, without downloads.

Usage: .venv/bin/python tests/e2e/generate_sample.py /tmp/jellydub-sample.mkv
"""
from pathlib import Path
import argparse

from jellydub.ffmpeg import run_ffmpeg


def generate(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite {path}")
    run_ffmpeg(['-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=24',
                '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=48000',
                '-t', '15', '-c:v', 'mpeg4', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-ac', '2', '-shortest', str(path)])
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    generate(parser.parse_args().output)
