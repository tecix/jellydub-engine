"""HTTP front end of the live dub server (stdlib ThreadingHTTPServer, JSON in UTF-8)."""
from __future__ import annotations

import ipaddress
import json
import re
import shutil
import socket
import sys
import threading
import time
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..config import Config
from ..cache import atomic_write_text, cache_key
from ..models import JellyDubError
from .session import Engine, Session

VERSION = "1"
SESSION_TTL = 30 * 60
MAX_BODY = 32 * 1024 * 1024
SERVICE_TYPE = "_tinytv-dub._tcp.local."


class DubServer:
    """Owns the sessions, the shared translator/TTS provider and the HTTP listener."""

    def __init__(self, config: Config, *, translator=None, provider=None, ttl: float = SESSION_TTL,
                 verbose: bool = False):
        self.config = config
        self.ttl = ttl
        self.verbose = verbose
        self.root = Path(config.cache_dir) / "live"
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self._translator = translator
        self._translator_lock = threading.Lock()   # the model is loaded once and used by one thread at a time
        self._provider = provider
        self.httpd: ThreadingHTTPServer | None = None
        self._stop = threading.Event()
        self._zeroconf = None
        self._service = None

    # ---- engine -----------------------------------------------------------------------------

    def _tts(self):
        if self._provider is None:
            from ..tts import get_provider
            c = self.config
            options = {"rate": c.rate, "pitch": c.pitch} if c.tts == "edge" else {
                "device": "cpu" if c.device == "auto" else c.device, "cache_dir": c.cache_dir / "voices"}
            if c.voice is not None:
                options["voice"] = c.voice
            self._provider = get_provider(c.tts, **options)
        return self._provider

    def _translate(self, cues):
        from .. import pipeline
        with self._translator_lock:
            if self._translator is None:
                c = self.config
                self._translator = pipeline.get_translator(name=c.translator, model=c.translator_model, device=c.device)
            return pipeline.translate_cues(cues, self._translator, self.root / "translations")

    def engine(self) -> Engine:
        return Engine(self._tts(), self._translate, self.root / "tts",
                      max_tempo=self.config.max_tempo, workers=self.config.tts_workers,
                      translate_targets=self._translate_targets)

    def _translate_targets(self, cues, first, end, translated_text):
        """Translate targets with full-source cache identity and read-only dialogue context."""
        from .. import pipeline
        from ..translate import _ChunkedTranslator

        with self._translator_lock:
            if self._translator is None:
                c = self.config
                self._translator = pipeline.get_translator(name=c.translator, model=c.translator_model, device=c.device)
            translator = self._translator
            cache_dir = self.root / "translations"
            if not isinstance(translator, _ChunkedTranslator):
                lo = max(0, first - 3)
                return pipeline.translate_cues(cues[lo:end], translator, cache_dir)[first - lo:]

            identity = translator.cache_id()

            def path_for(pos):
                cue = cues[pos]
                context = [(c.text, c.duration) for c in cues[max(0, pos - 5):pos]]
                return cache_dir / f"{cache_key(identity, cue.text, cue.duration, context)}.json"

            def cached(pos):
                try:
                    value = json.loads(path_for(pos).read_text(encoding="utf-8"))["text"]
                    if isinstance(value, str) and (value.strip() or not cues[pos].text.strip()):
                        return value
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                return None

            result = list(cues[first:end])
            pending = []
            known = dict(translated_text)
            for pos in range(first, end):
                value = cached(pos)
                if value is None and not cues[pos].text.strip():
                    value = cues[pos].text
                if value is None:
                    pending.append(pos)
                else:
                    known[cues[pos].index] = value
                    result[pos - first] = replace(cues[pos], text=value)
            if pending:
                context = []
                for pos in range(max(0, pending[0] - 5), pending[0]):
                    value = known.get(cues[pos].index)
                    if value is None:
                        value = cached(pos)
                    if value is not None:
                        context.append({"source": cues[pos].text, "translated": value})
                values = translator.translate_cues_batch([cues[pos] for pos in pending], context=context)
                if len(values) != len(pending) or any(not isinstance(v, str) or not v.strip() for v in values):
                    raise JellyDubError("Translator returned missing or empty results")
                for pos, value in zip(pending, values):
                    atomic_write_text(path_for(pos), json.dumps({"text": value}, ensure_ascii=False))
                    result[pos - first] = replace(cues[pos], text=value)
            return result

    # ---- sessions ---------------------------------------------------------------------------

    def create_session(self, srt_text: str, source_lang: str = "auto", title: str | None = None,
                       *, on_demand: bool = False) -> Session:
        from .. import pipeline
        from ..subtitles import parse_srt
        if source_lang not in {"auto", "en", "vi"}:
            raise ValueError("source_lang must be auto, en or vi")
        session_id = uuid.uuid4().hex
        work = self.root / "sessions" / session_id
        work.mkdir(parents=True)
        try:
            source = work / "source.srt"
            source.write_text(srt_text, encoding="utf-8")
            try:
                cues = parse_srt(source)
            except JellyDubError as exc:
                raise ValueError(str(exc)) from exc
            language = pipeline.detect_source_language(source)[0] if source_lang == "auto" else source_lang
            session = Session(session_id, cues, language, self.engine(), work, title,
                              defer_start=True, on_demand=on_demand)
        except BaseException:
            shutil.rmtree(work, ignore_errors=True)
            raise
        with self.lock:
            self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        with self.lock:
            session = self.sessions.get(session_id)
        if session:
            session.touch()
        return session

    def delete(self, session_id: str) -> bool:
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if session:
            session.close()
        return session is not None

    def reap(self) -> int:
        """Drop sessions idle for longer than the TTL."""
        now = time.monotonic()
        with self.lock:
            stale = [sid for sid, s in self.sessions.items() if now - s.last_access > self.ttl]
        return sum(self.delete(sid) for sid in stale)

    # ---- lifecycle --------------------------------------------------------------------------

    def bind(self, host: str, port: int) -> ThreadingHTTPServer:
        self.httpd = ThreadingHTTPServer((host, port), _handler(self))
        shutil.rmtree(self.root / "sessions", ignore_errors=True)   # leftovers of a killed server
        self.httpd.daemon_threads = True
        threading.Thread(target=self._reaper, name="dub-reaper", daemon=True).start()
        return self.httpd

    def _reaper(self) -> None:
        while not self._stop.wait(min(30.0, max(0.05, self.ttl / 4))):
            self.reap()

    def serve_forever(self) -> None:
        assert self.httpd is not None
        try:
            self.httpd.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        self.unadvertise()
        for sid in list(self.sessions):
            self.delete(sid)
        if self.httpd:
            self.httpd.server_close()
        for resource in (self._translator, self._provider):
            try:
                resource and resource.close()
            except Exception:
                pass

    def advertise(self, port: int, report=print, addresses: list[str] | None = None) -> bool:
        """Register _tinytv-dub._tcp over mDNS when the optional zeroconf package is installed."""
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            report("mDNS: zeroconf not installed (uv sync --extra live); clients must use the URL")
            return False
        addresses = [socket.inet_aton(a) for a in (addresses or _local_addresses())]
        if not addresses:
            report("mDNS: no LAN address found; not advertising")
            return False
        name = socket.gethostname().split(".")[0] or "jellydub"
        info = ServiceInfo(SERVICE_TYPE, f"TinyTV Dub on {name}.{SERVICE_TYPE}", addresses=addresses,
                           port=port, properties={"v": VERSION}, server=f"{name}.local.")
        self._zeroconf = Zeroconf()
        self._zeroconf.register_service(info)
        self._service = info
        report(f"mDNS: advertising {SERVICE_TYPE} on {', '.join(socket.inet_ntoa(a) for a in addresses)}:{port}")
        return True

    def unadvertise(self) -> None:
        zc, self._zeroconf = self._zeroconf, None
        if zc:
            try:
                zc.unregister_service(self._service)
                zc.close()
            except Exception:
                pass


_VIRTUAL_IFACES = ("lo", "virbr", "docker", "br-", "veth", "vmnet", "vboxnet", "lxcbr", "lxdbr", "cni", "flannel", "cali")
_VIRTUAL_NETS = (ipaddress.ip_network("192.168.122.0/24"), ipaddress.ip_network("172.17.0.0/16"))


def _default_route_iface() -> str | None:
    try:
        with open("/proc/net/route") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if len(fields) > 7 and fields[1] == "00000000" and int(fields[7], 16) == 0:
                    return fields[0]
    except (OSError, StopIteration, ValueError):
        pass
    return None


def _usable_addresses(adapters: list[tuple[str, list[str]]], default_iface: str | None) -> list[str]:
    """Real-interface IPv4 addresses, bridges/containers dropped.

    mDNS is link-local and NsdManager resolves a single address, so only the default-route
    interface is advertised; without a known default route every real address is returned.
    """
    first, rest = [], []
    for name, ips in adapters:
        if name.startswith(_VIRTUAL_IFACES):
            continue
        for ip in ips:
            address = ipaddress.ip_address(ip)
            if address.is_loopback or address.is_link_local or any(address in net for net in _VIRTUAL_NETS):
                continue
            (first if name == default_iface else rest).append(ip)
    return first[:1] or rest


def _local_addresses() -> list[str]:
    """Non-loopback IPv4 addresses of the default-route interface (falls back to real interfaces)."""
    try:
        import ifaddr
    except ImportError:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("10.255.255.255", 1))
                return [probe.getsockname()[0]]
        except OSError:
            return []
    adapters = [(adapter.name, [ip.ip for ip in adapter.ips if ip.is_IPv4]) for adapter in ifaddr.get_adapters()]
    return _usable_addresses(adapters, _default_route_iface())


def _handler(app: DubServer):
    audio = re.compile(r"^/v1/sessions/([0-9a-f]+)/audio/(\d+)\.wav$")
    cues = re.compile(r"^/v1/sessions/([0-9a-f]+)/cues$")
    one = re.compile(r"^/v1/sessions/([0-9a-f]+)$")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "jellydub-live"

        def log_message(self, fmt, *args):
            if app.verbose:
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, status: int, body: bytes = b"", content_type: str = "application/json; charset=utf-8"):
            self.send_response(status)
            if status != 204:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload):
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def _error(self, status: int, message: str):
            self._json(status, {"error": message})

        def _guard(self, action):
            try:
                action()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:   # never leave the client hanging without a body
                try:
                    self._error(500, f"{type(exc).__name__}: {exc}")
                except OSError:
                    pass

        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def do_DELETE(self):
            self._guard(self._delete)

        def _get(self):
            url = urlsplit(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/v1/health":
                return self._json(200, {"ok": True, "version": VERSION, "translator": app.config.translator,
                                        "tts": app.config.tts})
            if match := cues.match(path):
                session = app.get(match.group(1))
                if session is None:
                    return self._error(404, "unknown session")
                query = parse_qs(url.query)
                try:
                    start = float(query.get("from", ["0"])[0])
                    end = float(query.get("to", ["inf"])[0])
                except ValueError:
                    return self._error(400, "from and to must be numbers")
                if start != start or end != end:
                    return self._error(400, "from and to must be numbers")
                session.set_playhead(start)
                return self._json(200, session.view(start, end))
            if match := audio.match(path):
                session = app.get(match.group(1))
                if session is None:
                    return self._error(404, "unknown session")
                clip = session.clip_path(int(match.group(2)))
                if clip is None:
                    return self._error(404, "audio not ready")
                try:
                    data = clip.read_bytes()
                except OSError:
                    return self._error(404, "audio not ready")
                return self._send(200, data, "audio/wav")
            self._error(404, "not found")

        def _post(self):
            if urlsplit(self.path).path.rstrip("/") != "/v1/sessions":
                return self._error(404, "not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._error(400, "bad Content-Length")
            if length <= 0 or length > MAX_BODY:
                return self._error(400 if length <= 0 else 413, "body must be a JSON object up to 32 MiB")
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                srt_text = body["srt"]
                if not isinstance(body, dict) or not isinstance(srt_text, str):
                    raise TypeError
                title = body.get("title")
                source_lang = body.get("source_lang") or "auto"
            except (ValueError, KeyError, TypeError):
                return self._error(400, 'body must be JSON like {"srt": "...", "source_lang": "auto"}')
            try:
                session = app.create_session(srt_text, source_lang, title if isinstance(title, str) else None)
            except ValueError as exc:
                return self._error(400, str(exc))
            except JellyDubError as exc:
                return self._error(500, str(exc))
            self._json(201, {"id": session.id, "source_lang": session.source_lang, "cues": len(session.cues)})

        def _delete(self):
            match = one.match(urlsplit(self.path).path.rstrip("/"))
            if not match:
                return self._error(404, "not found")
            if not app.delete(match.group(1)):
                return self._error(404, "unknown session")
            self._send(204)

    return Handler
