from pathlib import Path

import pytest

from jellydub.models import JellyDubError
from jellydub.subtitles import parse_srt, write_srt


def test_roundtrip(tmp_path):
    source = tmp_path / 'movie[1].en.srt'
    source.write_text('9\n00:00:03,001 --> 00:00:04,999\n<i>TYSON: Hello</i>\nworld\n\n2\n00:00:00,123 --> 00:00:01,456\n{\\an8}[[actor]] Hi!\n', encoding='utf-8-sig')
    cues = parse_srt(source)
    assert [c.index for c in cues] == [2, 9]
    assert cues[0].speaker == 'actor'
    assert cues[1].speaker == 'TYSON'
    assert cues[1].text == 'Hello world'
    dest = tmp_path / 'movie.vi.srt'
    write_srt(cues, dest)
    assert parse_srt(dest) == cues
    assert 'TYSON: Hello world' in dest.read_text()


def test_cp1252(tmp_path):
    path = tmp_path / 'old.srt'
    path.write_bytes('1\n00:00:00,000 --> 00:00:01,000\nCafé\n'.encode('cp1252'))
    assert parse_srt(path)[0].text == 'Café'


@pytest.mark.parametrize('content', ['', 'not srt', '1\n00:00:02,000 --> 00:00:01,000\nOops\n'])
def test_invalid(tmp_path, content):
    path = tmp_path / 'bad.srt'
    path.write_text(content)
    with pytest.raises(JellyDubError):
        parse_srt(path)


def test_missing(tmp_path):
    with pytest.raises(JellyDubError):
        parse_srt(tmp_path / 'absent.srt')
