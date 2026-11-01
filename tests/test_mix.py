import math
import struct
import sys
import types
import wave
from pathlib import Path

import pytest

from jellydub.ffmpeg import duration, probe, run_ffmpeg
from jellydub.mix import extract_audio, mix
from jellydub.models import JellyDubError, MixMode
from jellydub.separate import separate_background


def tone(path, seconds=1, frequency=440, channels=1):
    run_ffmpeg(['-f', 'lavfi', '-i', f'sine=frequency={frequency}:duration={seconds}:sample_rate=48000', '-ac', str(channels), str(path)])
    return path


@pytest.mark.parametrize('mode', list(MixMode))
def test_mix_format_and_longest(tmp_path, mode):
    dub = tone(tmp_path / 'dub.wav', .5)
    bed = tone(tmp_path / 'bed.wav', 1, channels=6)
    output = tmp_path / f'{mode.value}.wav'
    mix(mode, dub, output, original_wav=bed, background_wav=bed)
    assert duration(output) == pytest.approx(.5 if mode == MixMode.DUB_ONLY else 1, abs=.002)
    stream = probe(output)['streams'][0]
    assert stream['channels'] == 2
    assert stream['sample_rate'] == '48000'


def test_ducking_reduces_bed(tmp_path):
    bed = tone(tmp_path / 'bed.wav', 2, 200)
    dub = tmp_path / 'dub.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=frequency=1000:duration=1:sample_rate=48000', '-af', 'volume=4,adelay=500,apad,atrim=duration=2', str(dub)])
    output = tmp_path / 'mix.wav'
    mix(MixMode.VOICEOVER, dub, output, original_wav=bed)
    with wave.open(str(output)) as audio:
        samples = struct.unpack('<' + 'h' * (audio.getnframes() * 2), audio.readframes(audio.getnframes()))[::2]
    def amplitude(start, end):
        data = samples[int(start*48000):int(end*48000)]
        return abs(sum(v * complex(math.cos(2*math.pi*200*i/48000), math.sin(2*math.pi*200*i/48000)) for i,v in enumerate(data))) / len(data)
    assert amplitude(.8, 1.2) < amplitude(.1, .3) * .6


def test_input_protection_and_required_bed(tmp_path):
    source = tone(tmp_path / 'source.wav')
    with pytest.raises(JellyDubError):
        mix(MixMode.DUB_ONLY, source, source)
    with pytest.raises(JellyDubError):
        mix(MixMode.REPLACE, source, tmp_path / 'out.wav')


def test_extract_stream_downmix_and_brackets(tmp_path):
    source = tone(tmp_path / 'source [1].wav', channels=6)
    output = tmp_path / 'extract.wav'
    extract_audio(source, output)
    assert probe(output)['streams'][0]['channels'] == 2


def test_separation_fake_cache_and_failure(tmp_path, monkeypatch):
    source = tone(tmp_path / 'input.wav')
    calls = []
    class Fake:
        def __init__(self, **kwargs): self.folder = Path(kwargs['output_dir'])
        def load_model(self, **kwargs): calls.append(kwargs)
        def separate(self, source):
            output = self.folder / 'input_(Instrumental).wav'
            tone(output)
            return [output.name]
    monkeypatch.setitem(sys.modules, 'audio_separator', types.ModuleType('audio_separator'))
    module = types.ModuleType('audio_separator.separator')
    module.Separator = Fake
    monkeypatch.setitem(sys.modules, 'audio_separator.separator', module)
    result = separate_background(source, tmp_path / 'work')
    assert separate_background(source, tmp_path / 'work') == result
    assert len(calls) == 1
    assert probe(result)['streams'][0]['channels'] == 2
    monkeypatch.setattr(Fake, 'separate', lambda *args: [])
    with pytest.raises(JellyDubError, match='--mix voiceover'):
        separate_background(source, tmp_path / 'other')
