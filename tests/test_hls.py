"""Real FFmpeg + HTTP, fake speech/translation: no services or model downloads."""
import concurrent.futures
import json
import math
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jellydub.cli import app
from jellydub.config import Config
from jellydub.ffmpeg import probe, run_ffmpeg
from jellydub.live.hls import HlsStream, SegmentTimeout, public_base_url, write_strm
from jellydub.models import JellyDubError
from jellydub.tts.base import TTSProvider

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


class Tone(TTSProvider):
    name = "test-hls"
    max_concurrency = 2

    def __init__(self):
        self.texts = []

    def cache_id(self):
        return "hls-tone-600ms"

    def synthesize(self, text, out_path, speaker=None):
        self.texts.append(text)
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 880 * i / 24000)))
                          for i in range(14400))
        with wave.open(str(out_path), "wb") as audio:
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            audio.writeframes(frames)


class Translator:
    def cache_id(self):
        return "hls-translation"

    def translate_batch(self, texts):
        return ["Xin chào " + text for text in texts]

    def close(self):
        pass


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    root = tmp_path_factory.mktemp("hls-media")
    video = root / "Movie [2026].mkv"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=14.15",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=14.15",
                "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "pcm_s16le", str(video)])
    srt = root / "Movie.en.srt"
    srt.write_text("1\n00:00:00,200 --> 00:00:01,200\nFirst words\n\n"
                   "2\n00:00:03,700 --> 00:00:05,000\nAcross the boundary\n\n"
                   "3\n00:00:09,000 --> 00:00:10,000\nAfter a seek\n\n"
                   "4\n00:00:12,200 --> 00:00:13,500\nThe ending\n", encoding="utf-8")
    return video, srt


@pytest.fixture
def streams(tmp_path, media):
    made = []

    def make(**options):
        config = Config(cache_dir=tmp_path / "cache", mix=options.pop("mix", "voiceover"),
                        source_lang=options.pop("source_lang", "en"), max_tempo=1.35)
        stream = HlsStream(*media, config, translator=Translator(), provider=Tone(),
                           segment_seconds=4, buffer_seconds=0, report=lambda _: None, **options)
        made.append(stream)
        return stream

    yield make
    for stream in made:
        if stream.httpd:
            stream.httpd.shutdown()
        stream.close()


def start_http(stream):
    httpd = stream.bind("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def request(base, path, method="GET", headers=None):
    req = urllib.request.Request(base + path, method=method, headers=headers or {})
    try:
        response = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read(), response.headers


def test_playlist_is_complete_seekable_vod_and_idle_until_segment_request(streams):
    stream = streams()
    base = start_http(stream)
    status, body, _ = request(base, stream.playlist_path)
    text = body.decode()
    assert status == 200
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text and text.endswith("#EXT-X-ENDLIST\n")
    assert text.count("#EXTINF:") == stream.count == 4
    assert "#EXT-X-DISCONTINUITY\n" not in text
    assert abs(sum(float(line.split(":")[1].strip(",")) for line in text.splitlines()
                   if line.startswith("#EXTINF:")) - stream.duration) < 1e-5
    assert stream.session.requested == set()
    assert stream.session.engine.provider.texts == []
    assert request(base, stream.playlist_path, "HEAD")[1] == b""
    assert request(base, f"/hls/{stream.id}/2.ts", "HEAD")[0] == 503
    assert stream.session.requested == set()


def test_seek_generates_only_target_audio_and_encoded_segment(streams):
    stream = streams()
    segment = stream.segment(2)  # 8–12s, with no need to generate the opening
    assert not (stream.cache / "0.ts").exists()
    # One preceding cue is needed to check whether its speech tail crosses 8s.
    assert set(stream.session.engine.provider.texts) == {"Xin chào Across the boundary", "Xin chào After a seek"}
    data = probe(segment)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
    assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
    assert audio["codec_name"] == "aac" and audio["sample_rate"] == "48000" and audio["channels"] == 2
    assert abs(float(data["format"]["duration"]) - 4) < 0.1
    frames = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_frames",
                             "-show_entries", "frame=key_frame", "-of", "json", str(segment)],
                            capture_output=True, text=True, check=True)
    assert json.loads(frames.stdout)["frames"][0]["key_frame"] == 1
    def first_frame(path, seek=None):
        args = ["ffmpeg", "-v", "error"]
        if seek is not None:
            args += ["-ss", str(seek)]
        args += ["-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"]
        return subprocess.run(args, capture_output=True, check=True).stdout
    actual, expected = first_frame(segment), first_frame(stream.video, 8)
    assert len(actual) == len(expected) == 160 * 90
    assert sum(abs(a - b) for a, b in zip(actual, expected)) / len(actual) < 3


def test_speech_tail_survives_segment_boundary(streams, tmp_path):
    stream = streams(mix="dub-only")
    for index, window, expected in [(0, (3.75, 3.95), True), (1, (0.04, 0.2), True), (1, (1, 2), False)]:
        segment = stream.segment(index)
        output = tmp_path / "decoded.wav"
        run_ffmpeg(["-i", str(segment), "-ss", str(window[0]), "-t", str(window[1] - window[0]),
                    "-vn", "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(output)])
        with wave.open(str(output), "rb") as wav:
            data = wav.readframes(wav.getnframes())
        samples = struct.unpack(f"<{len(data) // 2}h", data)
        rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
        assert (rms > 1000) == expected


def test_full_hls_decodes_with_correct_duration_over_http(streams, tmp_path):
    stream = streams()
    base = start_http(stream)
    output = tmp_path / "decoded.mkv"
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i",
                             base + stream.playlist_path, "-map", "0:v:0", "-map", "0:a:0",
                             "-c:v", "ffv1", "-c:a", "pcm_s16le", str(output)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert abs(float(probe(output)["format"]["duration"]) - stream.duration) < 0.2
    assert len(list(stream.cache.glob("*.ts"))) == stream.count
    # Packet timestamps must also be monotonic across independent AAC encoders.
    previous = float("-inf")
    for index in range(stream.count):
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_packets",
                                 "-show_entries", "packet=pts_time", "-of", "json", str(stream.segment(index))],
                                capture_output=True, text=True, check=True)
        packets = json.loads(result.stdout)["packets"]
        for packet in packets:
            timestamp = float(packet["pts_time"])
            assert timestamp > previous
            previous = timestamp


def test_hls_input_seek_has_monotonic_audio(streams):
    stream = streams()
    base = start_http(stream)
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", "8", "-i",
                             base + stream.playlist_path, "-t", "2", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and result.stderr == "", result.stderr


def test_segments_reuse_cache_across_server_restart(streams, monkeypatch):
    first = streams()
    output = first.segment(2)
    before = output.read_bytes()
    first.close()
    second = streams()
    assert second.id == first.id
    monkeypatch.setattr(second, "_encode", lambda *a: pytest.fail("cached segment re-encoded"))
    assert second.segment(2).read_bytes() == before
    assert second.session.engine.provider.texts == []
    different = streams(mix="dub-only")
    assert different.id != first.id


def test_simultaneous_requests_share_one_render(streams, monkeypatch):
    stream = streams()
    original = stream._encode
    calls = []

    def encode(*args):
        calls.append(args[0])
        original(*args)

    monkeypatch.setattr(stream, "_encode", encode)
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        paths = list(pool.map(stream.segment, [2, 2, 2]))
    assert len(calls) == 1 and paths == [paths[0]] * 3


def test_http_ranges_health_and_path_restrictions(streams):
    stream = streams()
    base = start_http(stream)
    path = f"/hls/{stream.id}/2.ts"
    status, data, headers = request(base, path)
    assert status == 200 and headers["Content-Type"] == "video/mp2t"
    for value, expected in [("bytes=0-187", data[:188]), ("bytes=-188", data[-188:]),
                             ("bytes=188-", data[188:])]:
        status, part, headers = request(base, path, headers={"Range": value})
        assert status == 206 and part == expected
        assert headers["Content-Length"] == str(len(part))
        assert headers["Content-Range"].endswith("/" + str(len(data)))
    assert request(base, path, headers={"Range": "bytes=999999999-"})[0] == 416
    assert request(base, path, headers={"Range": "bytes=-0"})[0] == 416
    assert request(base, path, headers={"Range": "bytes=0-1,5-6"})[0] == 416
    status, body, headers = request(base, path, "HEAD")
    assert status == 200 and body == b"" and headers["Content-Length"] == str(len(data))
    status, body, _ = request(base, "/v1/health")
    assert status == 200 and json.loads(body)["mode"] == "hls"
    for invalid in [f"/hls/{stream.id}/999.ts", f"/hls/{stream.id}/../source.srt", "/etc/passwd",
                    f"/hls/{stream.id}/-1.ts", "/hls/wrong/index.m3u8"]:
        assert request(base, invalid)[0] == 404


def test_timeout_and_failure_never_publish_partial_segments(streams, monkeypatch):
    stream = streams()
    base = start_http(stream)
    path = f"/hls/{stream.id}/0.ts"

    def timeout(*args):
        raise SegmentTimeout("still preparing")

    monkeypatch.setattr(stream, "_wait_for_audio", timeout)
    status, _, headers = request(base, path)
    assert status == 503 and headers["Retry-After"] == "2"
    assert not (stream.cache / "0.ts").exists()
    monkeypatch.setattr(stream, "_wait_for_audio", lambda *a: {})

    def fail(*args):
        raise JellyDubError("encoder failed")

    monkeypatch.setattr(stream, "_encode", fail)
    assert request(base, path)[0] == 500
    assert not (stream.cache / "0.ts").exists()


def test_failed_speech_is_not_silently_exported(streams):
    stream = streams()
    with stream.session.cond:
        stream.session.skipped.add(0)
        stream.session.failed[0] = "service unavailable"
    with pytest.raises(JellyDubError, match="Speech synthesis failed for cue 1"):
        stream.segment(0)
    assert not (stream.cache / "0.ts").exists()


def test_long_speech_rejected_instead_of_clipped(streams, tmp_path):
    stream = streams()
    with pytest.raises(JellyDubError, match="exceeds its available time"):
        stream._render_dub(0, 4, {0: (tmp_path / "unused.wav", 5)}, tmp_path / "out.wav")


def test_repeated_vietnamese_dialogue_shares_synthesis_without_cache_races(media, tmp_path):
    video, _ = media
    srt = tmp_path / "Movie.vi.srt"
    srt.write_text("1\n00:00:00,200 --> 00:00:01,500\nXin chao\n\n"
                   "2\n00:00:02,000 --> 00:00:03,500\nXin chao\n", encoding="utf-8")
    provider = Tone()
    stream = HlsStream(video, srt, Config(cache_dir=tmp_path / "cache", mix="dub-only"),
                       provider=provider, segment_seconds=4, buffer_seconds=0, report=lambda _: None)
    try:
        assert stream.session.source_lang == "vi"  # filename tag works without accents
        assert stream.segment(0).is_file()
        assert provider.texts == ["Xin chao"]
        assert stream.session.failed == {}
    finally:
        stream.close()


def test_encoder_timeout_reaps_process(streams):
    stream = streams(wait_seconds=1)
    with pytest.raises(SegmentTimeout, match="Encoder timed out"):
        stream._ffmpeg(["-f", "lavfi", "-i", "anullsrc", "-f", "null", "-"])
    assert stream._processes == set()


def test_aac_padding_does_not_add_a_segment_after_the_video_ends(tmp_path):
    video = tmp_path / "aac.mkv"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(video)])
    srt = tmp_path / "aac.vi.srt"
    srt.write_text("1\n00:00:00,200 --> 00:00:01,500\n[Music]\n", encoding="utf-8")
    assert float(probe(video)["format"]["duration"]) > 2
    stream = HlsStream(video, srt, Config(cache_dir=tmp_path / "cache", mix="voiceover"),
                       provider=Tone(), segment_seconds=2, buffer_seconds=0, report=lambda _: None)
    try:
        assert stream.count == 1 and stream.duration == 2
        assert any(s["codec_type"] == "video" for s in probe(stream.segment(0))["streams"])
    finally:
        stream.close()


def test_shutdown_cancels_encoder_and_waiting_segment(streams):
    stream = streams()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        encode = pool.submit(stream._ffmpeg, ["-re", "-f", "lavfi", "-i", "anullsrc", "-f", "null", "-"])
        deadline = time.monotonic() + 5
        while not stream._processes and time.monotonic() < deadline:
            time.sleep(0.01)
        assert stream._processes
        stream.close()
        with pytest.raises(JellyDubError):
            encode.result(timeout=5)
    assert stream._processes == set()
    with pytest.raises(JellyDubError, match="shutting down"):
        stream.segment(0)
    stream.close()  # idempotent


@pytest.mark.parametrize("origin", ["file:///etc", "http://a/b", "http://u:p@a", "http://a?x=1",
                                     "http://a#x", "http://a\n", "http://a:bad", "", "http://"])
def test_invalid_public_url(origin):
    with pytest.raises(JellyDubError, match="public-url"):
        public_base_url(origin)


def test_strm_preserves_inputs_and_existing_output(tmp_path):
    output = tmp_path / "Movie VI.strm"
    url = "http://192.168.2.10:8766/hls/id/index.m3u8"
    write_strm(output, url)
    assert output.read_text() == url + "\n"
    write_strm(output, url)
    with pytest.raises(JellyDubError, match="already exists"):
        write_strm(output, url + "?changed")
    with pytest.raises(JellyDubError, match="differ from all inputs"):
        write_strm(output, url, inputs=(output,), force=True)
    with pytest.raises(JellyDubError, match="end in .strm"):
        write_strm(tmp_path / "movie.mkv", url)
    write_strm(output, url + "?changed", force=True)
    assert "?changed" in output.read_text()


def test_stream_cli_publishes_after_warmup_and_closes(monkeypatch, tmp_path):
    seen = []

    class Fake:
        playlist_path = "/hls/test/index.m3u8"
        count = 10
        segment_seconds = 6

        def __init__(self, video, srt, config, **options):
            assert config.mix == "voiceover" and config.max_tempo == 1.35
            seen.append(config)

        def bind(self, host, port):
            return type("Http", (), {"server_address": (host, port)})()

        def segment(self, index):
            assert not (tmp_path / "Movie.strm").exists()
            seen.append(index)

        def serve_forever(self):
            assert (tmp_path / "Movie.strm").exists()
            raise KeyboardInterrupt

        def close(self):
            seen.append("closed")

    monkeypatch.setattr("jellydub.live.hls.HlsStream", Fake)
    monkeypatch.setattr("jellydub.cli.require_ffmpeg", lambda: None)
    result = CliRunner().invoke(app, ["stream", "movie.mkv", "--srt", "movie.srt",
                                     "--public-url", "http://192.168.2.10:8766",
                                     "--strm", str(tmp_path / "Movie.strm")])
    assert result.exit_code == 0, result.output
    assert seen[1:] == [0, 1, 2, "closed"]
    assert "http://192.168.2.10:8766/hls/test/index.m3u8" in result.output


@pytest.mark.parametrize("options", [{"segment_seconds": float("nan")}, {"buffer_seconds": -1},
                                     {"wait_seconds": 0}])
def test_invalid_stream_options_rejected_before_models(media, tmp_path, options):
    with pytest.raises(JellyDubError):
        HlsStream(*media, Config(cache_dir=tmp_path), **options)
