"""Opt-in real services test: never selected in the default offline suite."""
from pathlib import Path
import hashlib

import pytest
from typer.testing import CliRunner

from jellydub.cli import app
from jellydub.ffmpeg import duration, probe
from jellydub.validate import check_audio
from generate_sample import generate


@pytest.mark.e2e
@pytest.mark.parametrize('language', ['vi', pytest.param('en', marks=pytest.mark.slow)])
def test_real_dub(tmp_path, language):
    video = generate(tmp_path / 'sample[1].mkv')
    original_hash = hashlib.sha256(video.read_bytes()).hexdigest()
    output = tmp_path / 'output'
    args = ['dub', str(video), '--srt', str(Path(__file__).with_name(f'sample.{language}.srt')),
            '--source-lang', language, '--tts', 'edge', '--mix', 'voiceover', '--mkv',
            '--out-dir', str(output), '--cache-dir', str(tmp_path / 'cache')]
    runner = CliRunner()
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    audio = output / 'sample[1].AI-Dub.vi.mp3'
    assert not check_audio(audio, duration(video))
    assert (output / 'sample[1].vi.srt').stat().st_size
    mkv = output / 'sample[1].AI-Dub.vi.mkv'
    info = probe(mkv)
    assert len([s for s in info['streams'] if s['codec_type'] == 'audio']) == 2
    assert next(s for s in info['streams'] if s['codec_type'] == 'video')['codec_name'] == 'mpeg4'
    assert hashlib.sha256(video.read_bytes()).hexdigest() == original_hash
    # A second run must reuse synthesis outputs.
    cache_wavs = list((tmp_path / 'cache').glob('*/tts/*.wav'))
    assert cache_wavs
    stamps = {p: p.stat().st_mtime_ns for p in cache_wavs}
    resumed = runner.invoke(app, args)
    assert resumed.exit_code == 0, resumed.output
    assert all(p.stat().st_mtime_ns == stamp for p, stamp in stamps.items())
