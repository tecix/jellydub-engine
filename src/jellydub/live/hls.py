"""On-demand, seekable HLS for the ordinary Jellyfin player.

Only the file supplied at startup is exposed. Playlists are a complete VOD index;
segment requests drive translation and synthesis, including after a seek. Each TS
is independently encoded with timestamps anchored to its place in the movie.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from ..cache import atomic_write_text, cache_key
from ..config import Config
from ..ffmpeg import probe
from ..models import JellyDubError
from .server import DubServer
from .session import OUT_RATE

FPS = 30
CACHE_VERSION = "hls-v3"


class SegmentTimeout(JellyDubError):
    """Generation is still pending; clients may retry the same URL."""


def public_base_url(value: str) -> str:
    """Validate the externally reachable server origin used in a STRM file."""
    try:
        parts = urlsplit(value)
        valid = (parts.scheme in {"http", "https"} and parts.hostname and
                 not parts.username and not parts.password and not parts.query and not parts.fragment and
                 parts.path in {"", "/"} and not any(ord(c) <= 32 or ord(c) == 127 for c in value))
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise JellyDubError("--public-url must be an http(s) origin, e.g. http://192.168.2.10:8766")
    return value.rstrip("/")


def write_strm(path: Path, url: str, *, inputs: tuple[Path, ...] = (), force: bool = False) -> None:
    """Publish a Jellyfin shortcut, preserving existing files unless opted in."""
    if path.suffix.lower() != ".strm":
        raise JellyDubError("--strm must end in .strm")
    if any(path.resolve() == source.resolve() for source in inputs):
        raise JellyDubError("STRM output must differ from all inputs")
    text = url + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") == text:
            return
        if not force:
            raise JellyDubError(f"{path} already exists; use --force to replace it")
    atomic_write_text(path, text)


def _video_duration(info: dict, video: dict) -> float:
    """Prefer the video timeline; AAC padding can extend the container past EOF."""
    candidates = [video.get("duration")]
    tag = next((v for k, v in video.get("tags", {}).items() if k.upper() == "DURATION"), None)
    if isinstance(tag, str):
        try:
            hours, minutes, seconds = tag.split(":")
            candidates.append(int(hours) * 3600 + int(minutes) * 60 + float(seconds)
                              - float(video.get("start_time") or 0))
        except ValueError:
            pass
    candidates.append(info.get("format", {}).get("duration"))
    for candidate in candidates:
        try:
            length = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(length) and length > 0:
            return length
    raise JellyDubError("Source must have a known, finite duration")


class HlsStream:
    """One configured source and its durable, content-addressed segment cache."""

    def __init__(self, video: Path, srt: Path, config: Config, *, segment_seconds: float = 6,
                 buffer_seconds: float = 18, wait_seconds: float = 120,
                 translator=None, provider=None, report=print):
        self.video = video.expanduser().resolve()
        self.srt = srt.expanduser().resolve()
        self.config = config
        self.report = report
        for value, name, lo, hi in ((segment_seconds, "segment-seconds", 2, 30),
                                    (buffer_seconds, "buffer-seconds", 0, 120),
                                    (wait_seconds, "wait-seconds", 1, 600)):
            if not math.isfinite(value) or not lo <= value <= hi:
                raise JellyDubError(f"--{name} must be between {lo} and {hi}")
        if config.mix not in {"voiceover", "dub-only"}:
            raise JellyDubError("Streaming supports --mix voiceover or dub-only")
        if not self.video.is_file() or not self.srt.is_file():
            raise JellyDubError("Streaming needs an existing video file and an SRT file")
        info = probe(self.video)
        videos = [s for s in info["streams"] if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")]
        if not videos:
            raise JellyDubError("Source has no video stream")
        self.video_index = videos[0]["index"]
        if videos[0].get("color_transfer") in {"smpte2084", "arib-std-b67"}:
            raise JellyDubError("Streaming currently needs SDR video; convert HDR to SDR first")
        length = _video_duration(info, videos[0])
        self.total_frames = math.ceil(length * FPS)
        self.duration = self.total_frames / FPS
        self.segment_frames = round(segment_seconds * FPS)
        self.segment_seconds = self.segment_frames / FPS
        self.count = math.ceil(self.total_frames / self.segment_frames)
        self.buffer_seconds = buffer_seconds
        self.wait_seconds = wait_seconds
        audios = [s for s in info["streams"] if s.get("codec_type") == "audio"]
        if config.mix == "voiceover" and audios and config.audio_stream >= len(audios):
            raise JellyDubError(f"Source has no audio stream a:{config.audio_stream}")
        self.has_bed = config.mix == "voiceover" and bool(audios)
        # Parse before starting a worker, and reject ambiguous/overlapping starts.
        from ..subtitles import parse_srt
        cues = parse_srt(self.srt)
        if any(c.start >= self.duration or c.end > self.duration + 1 / FPS for c in cues):
            raise JellyDubError("Subtitles extend beyond the video; correct their timing first")
        if any(b.start <= a.start for a, b in zip(cues, cues[1:])):
            raise JellyDubError("Streaming requires distinct subtitle start times")
        self.dub = DubServer(config, translator=translator, provider=provider)
        try:
            from ..pipeline import detect_source_language
            language = (detect_source_language(self.srt)[0] if config.source_lang == "auto"
                        else config.source_lang)
            subtitle_bytes = self.srt.read_bytes()
            try:
                subtitle_text = subtitle_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                subtitle_text = subtitle_bytes.decode("cp1252")
            self.session = self.dub.create_session(subtitle_text, language, self.video.stem, on_demand=True)
            stat = self.video.stat()
            settings = asdict(config)
            for key in ("cache_dir", "out_dir", "tts_workers", "download", "subs", "providers",
                        "search_online", "subtitle_credentials_env"):
                settings.pop(key, None)
            self.id = cache_key(CACHE_VERSION, str(self.video), stat.st_size, stat.st_mtime_ns,
                                hashlib.sha256(subtitle_bytes).hexdigest(), settings, self.session.engine.provider.cache_id(),
                                self.segment_frames, self.total_frames)
            self.cache = Path(config.cache_dir).expanduser() / "hls" / self.id
            self.cache.mkdir(parents=True, exist_ok=True)
        except BaseException:
            self.dub.shutdown()
            raise
        self.closed = threading.Event()
        self._process_lock = threading.Lock()
        self._processes: set[subprocess.Popen] = set()
        self._render_lock = threading.Lock()  # bound video encoding to one ffmpeg per source
        self._slots = threading.BoundedSemaphore(2)  # current request + one seek/prefetch
        self._locks_guard = threading.Lock()
        self._segment_locks: dict[int, threading.Lock] = {}
        self.httpd: ThreadingHTTPServer | None = None

    @property
    def playlist_path(self) -> str:
        return f"/hls/{self.id}/index.m3u8"

    def bounds(self, index: int) -> tuple[float, float]:
        if not 0 <= index < self.count:
            raise IndexError("unknown segment")
        return (index * self.segment_frames / FPS,
                min((index + 1) * self.segment_frames, self.total_frames) / FPS)

    def playlist(self) -> bytes:
        rows = ["#EXTM3U", "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{math.ceil(self.segment_seconds)}",
                "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS"]
        for index in range(self.count):
            start, end = self.bounds(index)
            rows.extend([f"#EXTINF:{end - start:.6f},", f"{index}.ts"])
        rows.append("#EXT-X-ENDLIST")
        return ("\n".join(rows) + "\n").encode()

    def _cue_indices(self, start: float, end: float) -> list[int]:
        # Include a preceding cue's tail when it crosses a segment boundary.
        cues = self.session.cues
        return [c.index for i, c in enumerate(cues)
                if c.start < end and (cues[i + 1].start if i + 1 < len(cues) else c.end) > start]

    def _wait_for_audio(self, start: float, end: float, deadline: float) -> dict:
        session = self.session
        needed = self._cue_indices(start, end)
        ahead = self._cue_indices(start, min(self.duration, end + self.buffer_seconds))
        with session.cond:
            session.requested.update(ahead)
            session.set_playhead(start)
            session.cond.notify_all()
            while True:
                if self.closed.is_set() or session.closed:
                    raise JellyDubError("Stream is shutting down")
                if session.error:
                    raise JellyDubError(session.error)
                for index in needed:
                    if index in session.failed:
                        raise JellyDubError(f"Speech synthesis failed for cue {index + 1}: {session.failed[index]}")
                if all(session._finished(i) for i in needed):
                    return {i: session.clips[i] for i in needed if i in session.clips}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SegmentTimeout("Speech is still being prepared; retry this segment")
                session.cond.wait(min(remaining, 0.5))

    def _render_dub(self, start: float, end: float, clips: dict, output: Path) -> None:
        frames = round((end - start) * OUT_RATE)
        cursor = 0
        with wave.open(str(output), "wb") as target:
            target.setparams((1, 2, OUT_RATE, 0, "NONE", "not compressed"))
            for index, (path, length) in sorted(clips.items()):
                cue = self.session.cues[index]
                boundary = (self.session.cues[index + 1].start
                            if index + 1 < len(self.session.cues) else min(cue.end, self.duration))
                if cue.start + length > boundary + 1 / OUT_RATE:
                    raise JellyDubError(f"Speech for cue {index + 1} exceeds its available time; "
                                        "shorten that subtitle or increase --max-tempo and restart")
                with wave.open(str(path), "rb") as source:
                    skip = max(0, round((start - cue.start) * OUT_RATE))
                    offset = max(0, round((cue.start - start) * OUT_RATE))
                    size = min(source.getnframes() - skip, frames - offset)
                    if size <= 0:
                        continue
                    target.writeframesraw(b"\0" * ((offset - cursor) * 2))
                    source.setpos(skip)
                    target.writeframesraw(source.readframes(size))
                    cursor = offset + size
            target.writeframesraw(b"\0" * ((frames - cursor) * 2))

    def _encode(self, start: float, end: float, clips: dict, output: Path) -> None:
        length = end - start
        with tempfile.TemporaryDirectory(prefix="render-", dir=self.cache) as work:
            work = Path(work)
            speech = work / "speech.wav"
            self._render_dub(start, end, clips, speech)
            inputs = ["-ss", f"{start:.9f}", "-i", str(self.video), "-i", str(speech)]
            graph = (f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                     f"apad,atrim=duration={length:.9f},asetpts=PTS-STARTPTS[dub];")
            if self.has_bed:
                # Extract audio first: feeding both video and a branched audio
                # graph from one demuxer can deadlock FFmpeg's bounded queues.
                bed = work / "bed.wav"
                self._ffmpeg(["-ss", f"{start:.9f}", "-i", str(self.video),
                              "-map", f"0:a:{self.config.audio_stream}", "-vn", "-t", f"{length:.9f}",
                              "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(bed)])
                inputs.extend(["-i", str(bed)])
                # Same floor-bounded, speech-triggered ducking as the offline mix.
                graph += ("[2:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                          f"apad,atrim=duration={length:.9f},asetpts=PTS-STARTPTS,asplit=2[low][high];"
                          "[low]volume=0.2511886432[floor];[high]volume=0.7488113568[top];"
                          "[dub]asplit=2[voice][key];"
                          "[top][key]sidechaincompress=threshold=0.01:ratio=20:attack=20:release=300[duck];"
                          "[floor][duck][voice]amix=inputs=3:duration=longest:normalize=0[mix]")
            else:
                graph += "[dub]anull[mix]"
            temporary = work / "segment.ts"
            self._ffmpeg([*inputs,
                        "-filter_complex_threads", "1", "-filter_complex", graph,
                        "-map", f"0:{self.video_index}", "-map", "[mix]", "-map_metadata", "-1", "-sn", "-dn",
                        "-vf", "setpts=PTS-STARTPTS,scale=w='min(1920,iw)':h='min(1080,ih)':"
                        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        f"fps={FPS},tpad=stop_mode=clone:stop_duration=1",
                        "-t", f"{length:.9f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.1",
                        "-threads", "2", "-bf", "0", "-g", str(FPS * 2), "-sc_threshold", "0",
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
                        # Each independent AAC encoder emits a priming packet.
                        # Drop it so it cannot overlap the preceding segment's
                        # last packet and produce backwards audio timestamps.
                        "-bsf:a", "noise=amount=0:drop='eq(n,0)'",
                        "-metadata:s:a:0", "language=vie", "-metadata:s:a:0", "title=Tiếng Việt (AI Dub)",
                        "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0",
                        "-output_ts_offset", f"{start + 1:.9f}", "-mpegts_copyts", "1",
                        "-avoid_negative_ts", "disabled", "-f", "mpegts", str(temporary)])
            if self.closed.is_set():
                raise JellyDubError("Stream is shutting down")
            if temporary.stat().st_size == 0:
                raise JellyDubError("FFmpeg produced an empty segment")
            os.replace(temporary, output)

    def _ffmpeg(self, args: list[str]) -> None:
        """Bound subprocess lifetime and stop encoders when the server closes."""
        with self._process_lock:
            if self.closed.is_set():
                raise JellyDubError("Stream is shutting down")
            process = subprocess.Popen(["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", *args],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._processes.add(process)
        try:
            try:
                _, error = process.communicate(timeout=self.wait_seconds)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.communicate()
                raise SegmentTimeout("Encoder timed out; retry the segment or increase --wait-seconds") from exc
            if process.returncode:
                raise JellyDubError(f"FFmpeg failed: {error.decode(errors='replace').strip()}")
        finally:
            # KeyboardInterrupt or any other early exit must also reap the child.
            if process.poll() is None:
                process.kill()
                process.communicate()
            with self._process_lock:
                self._processes.discard(process)

    def segment(self, index: int) -> Path:
        if self.closed.is_set():
            raise JellyDubError("Stream is shutting down")
        start, end = self.bounds(index)
        output = self.cache / f"{index}.ts"
        if output.is_file():
            return output
        deadline = time.monotonic() + self.wait_seconds
        with self._locks_guard:
            lock = self._segment_locks.setdefault(index, threading.Lock())
        if not lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise SegmentTimeout("Segment generation is busy; retry this segment")
        try:
            if output.is_file():
                return output
            if not self._slots.acquire(timeout=max(0, deadline - time.monotonic())):
                raise SegmentTimeout("Segment generation is busy; retry this segment")
            try:
                self.report(f"Preparing segment {index + 1}/{self.count} ({start:.1f}–{end:.1f}s)")
                clips = self._wait_for_audio(start, end, deadline)
                if not self._render_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                    raise SegmentTimeout("Video encoding is busy; retry this segment")
                try:
                    if self.closed.is_set():
                        raise JellyDubError("Stream is shutting down")
                    self._encode(start, end, clips, output)
                finally:
                    self._render_lock.release()
            finally:
                self._slots.release()
            return output
        finally:
            lock.release()

    def bind(self, host: str, port: int) -> ThreadingHTTPServer:
        self.httpd = ThreadingHTTPServer((host, port), _handler(self))
        self.httpd.daemon_threads = True
        return self.httpd

    def serve_forever(self) -> None:
        assert self.httpd is not None
        try:
            self.httpd.serve_forever()
        finally:
            self.close()

    def close(self) -> None:
        with self._process_lock:
            if self.closed.is_set():
                return
            self.closed.set()
            for process in self._processes:
                if process.poll() is None:
                    process.terminate()
        self.dub.shutdown()
        if self.httpd:
            self.httpd.server_close()


def _handler(stream: HlsStream):
    prefix = f"/hls/{stream.id}/"
    segment = re.compile(r"([0-9]{1,9})\.ts")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "jellydub-hls"
        timeout = 30

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                # HLS clients routinely abandon keep-alive sockets after seeks.
                pass

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _error(self, status, message, headers=None):
            self._send(status, json.dumps({"error": message}).encode(), headers=headers)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            try:
                self._get()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except SegmentTimeout as exc:
                self._error(503, str(exc), {"Retry-After": "2", "Cache-Control": "no-store"})
            except (JellyDubError, OSError) as exc:
                stream.report(f"Streaming error: {exc}")
                self._error(500, str(exc), {"Cache-Control": "no-store"})
            except Exception as exc:
                stream.report(f"Unexpected streaming error: {type(exc).__name__}: {exc}")
                self._error(500, "Unexpected streaming error", {"Cache-Control": "no-store"})

        def _get(self):
            path = urlsplit(self.path).path
            if path == "/v1/health":
                return self._send(200, json.dumps({"ok": True, "mode": "hls", "id": stream.id,
                                                   "duration": stream.duration, "segments": stream.count,
                                                   "playlist": stream.playlist_path}).encode(),
                                  headers={"Cache-Control": "no-store"})
            if path == stream.playlist_path:
                return self._send(200, stream.playlist(), "application/vnd.apple.mpegurl",
                                  {"Cache-Control": "no-cache"})
            if path.startswith(prefix) and (match := segment.fullmatch(path[len(prefix):])):
                index = int(match.group(1))
                if index >= stream.count:
                    return self._error(404, "unknown segment")
                # HEAD probes must not trigger expensive translation/encoding.
                output = stream.cache / f"{index}.ts"
                if self.command == "HEAD" and not output.is_file():
                    return self._error(503, "segment not generated yet", {"Retry-After": "2"})
                output = stream.segment(index)
                data = output.read_bytes()
                headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=31536000, immutable"}
                requested = self.headers.get("Range")
                if requested:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                    if not match or not any(match.groups()):
                        return self._send(416, headers={"Content-Range": f"bytes */{len(data)}"})
                    lo, hi = match.groups()
                    a = int(lo) if lo else max(0, len(data) - int(hi))
                    b = min(int(hi), len(data) - 1) if lo and hi else len(data) - 1
                    if a > b or a >= len(data):
                        return self._send(416, headers={"Content-Range": f"bytes */{len(data)}"})
                    headers["Content-Range"] = f"bytes {a}-{b}/{len(data)}"
                    return self._send(206, data[a:b + 1], "video/mp2t", headers)
                return self._send(200, data, "video/mp2t", headers)
            self._error(404, "not found")

    return Handler
