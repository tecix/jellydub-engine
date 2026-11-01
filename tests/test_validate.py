from jellydub.ffmpeg import run_ffmpeg
from jellydub.validate import check_audio


def test_missing_empty_invalid(tmp_path):
    path = tmp_path / 'audio.wav'
    assert 'Missing' in check_audio(path, 1)[0]
    path.touch()
    assert 'Empty' in check_audio(path, 1)[0]
    path.write_text('invalid')
    assert 'Invalid' in check_audio(path, 1)[0]


def test_duration_and_silence(tmp_path):
    tone = tmp_path / 'tone.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'sine=duration=1', str(tone)])
    assert check_audio(tone, 1) == []
    assert 'duration mismatch' in check_audio(tone, 2)[0]
    silence = tmp_path / 'silence.wav'
    run_ffmpeg(['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo', '-t', '1', str(silence)])
    assert any('Silent' in problem for problem in check_audio(silence, 1))
