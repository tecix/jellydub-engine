import hashlib

import pytest

from jellydub.export import export_mp3, mux_mkv, output_paths
from jellydub.ffmpeg import probe, run_ffmpeg
from jellydub.models import JellyDubError


def test_paths(tmp_path):
    video = tmp_path / 'Movie [2026].mkv'
    paths = output_paths(video)
    assert paths['audio'].name == 'Movie [2026].AI-Dub.vi.mp3'
    assert paths['srt'].name == 'Movie [2026].vi.srt'
    assert output_paths(video, tmp_path / 'out')['mkv'].parent == tmp_path / 'out'
    with pytest.raises(JellyDubError): output_paths(video, label='../bad')


def test_export_and_mux_preserve_source(tmp_path):
    source = tmp_path / 'movie [1].mkv'
    run_ffmpeg(['-f', 'lavfi', '-i', 'testsrc=size=32x32:rate=10:duration=1', '-f', 'lavfi', '-i', 'sine=duration=1', '-c:v', 'ffv1', '-c:a', 'eac3', '-ac', '6', str(source)])
    checksum = hashlib.sha256(source.read_bytes()).digest()
    dub = tmp_path / 'dub.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=800:duration=1', str(dub)])
    mp3 = tmp_path / 'out' / 'movie.AI-Dub.vi.mp3'
    export_mp3(dub, mp3)
    assert probe(mp3)['streams'][0]['channels'] == 2
    output = tmp_path / 'dubbed.mkv'
    mux_mkv(source, mp3, output)
    streams = probe(output)['streams']
    assert streams[0]['codec_name'] == 'ffv1'
    tracks = [s for s in streams if s['codec_type'] == 'audio']
    assert tracks[0]['codec_name'] == 'eac3'
    assert tracks[0]['channels'] == 6
    assert tracks[1]['tags']['language'] == 'vie'
    assert tracks[1]['tags']['title'] == 'Tiếng Việt (AI Dub)'
    assert hashlib.sha256(source.read_bytes()).digest() == checksum
    assert not list(tmp_path.rglob('*.part.*'))
    with pytest.raises(JellyDubError): mux_mkv(source, mp3, source)


def test_atomic_failure_keeps_previous_export(tmp_path, monkeypatch):
    import jellydub.export as module
    output = tmp_path / 'existing.mp3'
    output.write_bytes(b'previous export')
    def fail(args):
        from pathlib import Path
        Path(args[-1]).write_bytes(b'partial')
        raise JellyDubError('encoder failed')
    monkeypatch.setattr(module, 'run_ffmpeg', fail)
    with pytest.raises(JellyDubError, match='encoder failed'):
        export_mp3(tmp_path / 'input.wav', output)
    assert output.read_bytes() == b'previous export'
    assert not (tmp_path / 'existing.part.mp3').exists()
