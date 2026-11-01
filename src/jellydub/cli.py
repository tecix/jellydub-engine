"""Command line interface for JellyDub."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
from typing import Annotated

import typer

from .config import load_config
from .ffmpeg import require_ffmpeg
from .models import JellyDubError
from . import pipeline

app = typer.Typer(help="Translate subtitles and create Vietnamese audio for Jellyfin.", no_args_is_help=True)


def _report(message: str) -> None:
    typer.echo(message, err=True)


def _run(action):
    try:
        return action()
    except (JellyDubError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def translate(
    video: Path,
    srt: Annotated[Path, typer.Option("--srt", help="English subtitle file")],
    out_dir: Annotated[Path | None, typer.Option()] = None,
    cache_dir: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option(help="TOML configuration file")] = None,
    device: Annotated[str | None, typer.Option()] = None,
    source_lang: Annotated[str | None, typer.Option(help="auto, en or vi")] = None,
    translator: Annotated[str | None, typer.Option(help="opus-mt (offline), gemini (API), agy or agy-stream (experimental CLI)")] = None,
    translator_model: Annotated[str | None, typer.Option(help="Model override for the selected translator")] = None,
):
    """Translate English subtitles for review before dubbing."""
    def run():
        settings = load_config(config, source_lang=source_lang, out_dir=out_dir, cache_dir=cache_dir, device=device,
                               translator=translator, translator_model=translator_model)
        result = pipeline.translate(video, srt, settings, report=_report)
        typer.echo(str(result))
    _run(run)


@app.command()
def dub(
    video: Path,
    srt: Annotated[Path | None, typer.Option("--srt", help="Explicit English or Vietnamese SRT")] = None,
    source_lang: Annotated[str | None, typer.Option(help="auto (default), en or vi")] = None,
    translator: Annotated[str | None, typer.Option(help="opus-mt (offline), gemini (API), agy or agy-stream (experimental CLI)")] = None,
    translator_model: Annotated[str | None, typer.Option(help="Model override for the selected translator")] = None,
    tts: Annotated[str | None, typer.Option(help="edge or vieneu")] = None,
    tts_workers: Annotated[int | None, typer.Option(min=1, help="Concurrent TTS requests (default 4; limited by provider)")] = None,
    voice: Annotated[str | None, typer.Option()] = None,
    rate: Annotated[str | None, typer.Option()] = None,
    pitch: Annotated[str | None, typer.Option()] = None,
    reference: Annotated[list[str] | None, typer.Option(help="FILE or LABEL=FILE; repeat for speakers")] = None,
    mix: Annotated[str | None, typer.Option(help="dub-only, voiceover or replace")] = None,
    mkv: Annotated[bool | None, typer.Option("--mkv/--no-mkv")] = None,
    out_dir: Annotated[Path | None, typer.Option()] = None,
    cache_dir: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option()] = None,
    audio_stream: Annotated[int | None, typer.Option(help="Zero-based source audio stream (a:N)")] = None,
    fallback_voiceover: Annotated[bool | None, typer.Option("--fallback-voiceover/--no-fallback-voiceover")] = None,
    device: Annotated[str | None, typer.Option()] = None,
    max_tempo: Annotated[float | None, typer.Option()] = None,
    subs: Annotated[str | None, typer.Option(help="auto, local, download or translate")] = None,
    download: Annotated[bool | None, typer.Option("--download/--no-download")] = None,
    search_online: Annotated[bool | None, typer.Option("--search-online/--no-search-online", help="Search online even when a good local subtitle exists")] = None,
    providers: Annotated[list[str] | None, typer.Option(help="Provider name; repeat or separate with commas")] = None,
):
    """Create a synchronized Vietnamese dub; keep the original video."""
    options = dict(source_lang=source_lang, translator=translator, translator_model=translator_model, tts=tts, tts_workers=tts_workers, voice=voice, rate=rate, pitch=pitch,
                   reference=reference or None, mix=mix, mkv=mkv, out_dir=out_dir,
                   cache_dir=cache_dir, audio_stream=audio_stream,
                   fallback_voiceover=fallback_voiceover, device=device, max_tempo=max_tempo,
                   subs=subs, download=download, search_online=search_online, providers=_providers(providers))
    _run(lambda: pipeline.dub(video, srt, load_config(config, **options), report=_report))


def _providers(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    return [part.strip() for value in values for part in value.split(",") if part.strip()]


@app.command()
def subs(
    video: Path,
    lang: Annotated[str, typer.Option(help="Subtitle language (vi or en)")] = "vi",
    download: Annotated[bool | None, typer.Option("--download/--no-download")] = None,
    search_online: Annotated[bool | None, typer.Option("--search-online/--no-search-online", help="Search online even when a good local subtitle exists")] = None,
    providers: Annotated[list[str] | None, typer.Option(help="Provider name; repeat or separate with commas")] = None,
    out_dir: Annotated[Path | None, typer.Option()] = None,
    cache_dir: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option()] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing different subtitle output")] = False,
):
    """Find, synchronize and save the best matching subtitle."""
    def run():
        settings = load_config(config, download=download, search_online=search_online, providers=_providers(providers),
                               out_dir=out_dir, cache_dir=cache_dir)
        typer.echo(str(pipeline.subs(video, settings, lang=lang, force=force, report=_report)))
    _run(run)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to listen on (0.0.0.0 = all, incl. LAN and tailnet)")] = "0.0.0.0",
    port: Annotated[int, typer.Option()] = 8765,
    translator: Annotated[str | None, typer.Option(help="opus-mt (offline), gemini (API), agy or agy-stream (experimental CLI)")] = None,
    translator_model: Annotated[str | None, typer.Option(help="Model override for the selected translator")] = None,
    tts: Annotated[str | None, typer.Option(help="edge or vieneu")] = None,
    tts_workers: Annotated[int | None, typer.Option(min=1, help="Concurrent TTS requests (default 4; limited by provider)")] = None,
    voice: Annotated[str | None, typer.Option()] = None,
    rate: Annotated[str | None, typer.Option()] = None,
    pitch: Annotated[str | None, typer.Option()] = None,
    max_tempo: Annotated[float | None, typer.Option(help="Maximum speed-up to fit a clip into its slot (default 1.35)")] = None,
    cache_dir: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option()] = None,
    device: Annotated[str | None, typer.Option()] = None,
    mdns: Annotated[bool, typer.Option("--mdns/--no-mdns", help="Advertise _tinytv-dub._tcp (needs the live extra)")] = True,
    advertise_ip: Annotated[list[str] | None, typer.Option(help="Address to advertise over mDNS instead of auto-detected real interfaces; repeatable")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", help="Log every request")] = False,
):
    """Run the live dubbing server used by the TinyTV Dub app (LAN only, no auth)."""
    def run():
        from .live.server import DubServer
        settings = load_config(config, translator=translator, translator_model=translator_model, tts=tts,
                               tts_workers=tts_workers, voice=voice, rate=rate, pitch=pitch,
                               max_tempo=max_tempo if max_tempo is not None or config else 1.35,
                               cache_dir=cache_dir, device=device)
        require_ffmpeg()
        server = DubServer(settings, verbose=verbose)
        httpd = server.bind(host, port)
        actual = httpd.server_address[1]
        _report(f"Live dub server on http://{host}:{actual} (translator {settings.translator}, tts {settings.tts})")
        if mdns:
            server.advertise(actual, _report, advertise_ip)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            _report("Stopping")
    _run(run)


@app.command()
def stream(
    video: Path,
    srt: Annotated[Path, typer.Option("--srt", help="English or Vietnamese SRT matched to this video")],
    host: Annotated[str, typer.Option(help="Listening interface (trusted LAN/tailnet only)")] = "0.0.0.0",
    port: Annotated[int, typer.Option(min=0, max=65535)] = 8766,
    public_url: Annotated[str | None, typer.Option(help="Origin reachable by Jellyfin and TV, e.g. http://192.168.2.10:8766")] = None,
    strm: Annotated[Path | None, typer.Option(help="Write a .strm shortcut into a Jellyfin library folder")] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing different STRM file")] = False,
    segment_seconds: Annotated[float, typer.Option(min=2, max=30, help="Video segment length")] = 6,
    buffer_seconds: Annotated[float, typer.Option(min=0, max=120, help="Speech lookahead and initial video buffer")] = 18,
    wait_seconds: Annotated[float, typer.Option(min=1, max=600, help="Wait for speech/encoder before returning retryable HTTP 503")] = 120,
    warmup: Annotated[bool, typer.Option("--warmup/--no-warmup", help="Prepare the opening buffer before publishing the shortcut")] = True,
    source_lang: Annotated[str | None, typer.Option(help="auto, en or vi")] = None,
    translator: Annotated[str | None, typer.Option(help="opus-mt, gemini, agy or agy-stream")] = None,
    translator_model: Annotated[str | None, typer.Option()] = None,
    tts: Annotated[str | None, typer.Option(help="edge or vieneu")] = None,
    tts_workers: Annotated[int | None, typer.Option(min=1)] = None,
    voice: Annotated[str | None, typer.Option()] = None,
    rate: Annotated[str | None, typer.Option()] = None,
    pitch: Annotated[str | None, typer.Option()] = None,
    mix: Annotated[str | None, typer.Option(help="voiceover (default) or dub-only")] = None,
    audio_stream: Annotated[int | None, typer.Option(min=0)] = None,
    max_tempo: Annotated[float | None, typer.Option(help="Maximum speech speed-up (default 1.35)")] = None,
    cache_dir: Annotated[Path | None, typer.Option()] = None,
    config: Annotated[Path | None, typer.Option()] = None,
    device: Annotated[str | None, typer.Option()] = None,
):
    """Serve on-demand Vietnamese-dubbed HLS for Jellyfin's normal TV player."""
    def run():
        import math
        from .live.hls import HlsStream, public_base_url, write_strm
        from .live.server import _local_addresses

        # Reject invalid origins before loading models or publishing any output.
        origin = public_base_url(public_url) if public_url is not None else None
        settings = load_config(config, source_lang=source_lang, translator=translator,
                               translator_model=translator_model, tts=tts, tts_workers=tts_workers,
                               voice=voice, rate=rate, pitch=pitch, audio_stream=audio_stream,
                               mix=mix if mix is not None or config else "voiceover",
                               max_tempo=max_tempo if max_tempo is not None or config else 1.35,
                               cache_dir=cache_dir, device=device)
        require_ffmpeg()
        server = HlsStream(video, srt, settings, segment_seconds=segment_seconds,
                           buffer_seconds=buffer_seconds, wait_seconds=wait_seconds, report=_report)
        try:
            httpd = server.bind(host, port)
            if origin is None:
                address = host
                if host == "0.0.0.0":
                    address = next(iter(_local_addresses()), "127.0.0.1")
                origin = public_base_url(f"http://{address}:{httpd.server_address[1]}")
            url = origin + server.playlist_path
            if strm is not None:
                # Check collisions before spending time on a cold speech/model startup.
                if strm.suffix.lower() != ".strm":
                    raise JellyDubError("--strm must end in .strm")
                if strm.exists() and strm.read_text(encoding="utf-8") != url + "\n" and not force:
                    raise JellyDubError(f"{strm} already exists; use --force to replace it")
            if warmup:
                count = min(server.count, max(1, math.ceil(buffer_seconds / server.segment_seconds)))
                _report(f"Preparing {count} opening segment(s) before playback")
                for index in range(count):
                    server.segment(index)
            if strm is not None:
                write_strm(strm, url, inputs=tuple(p for p in (video, srt, config) if p is not None), force=force)
                _report(f"Jellyfin shortcut: {strm} (scan its library folder, then play this item)")
            typer.echo(url)
            _report("HLS ready; keep this process running while watching")
            server.serve_forever()
        except KeyboardInterrupt:
            _report("Stopping")
        finally:
            server.close()
    _run(run)


@app.command()
def doctor():
    """Check FFmpeg and installed optional packages without loading models."""
    missing = False
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        typer.echo(f"{tool}: {found or 'MISSING (sudo apt install ffmpeg)'}")
        missing |= found is None
    for extra, modules in (("core", ("edge_tts", "srt")),
                           ("translate", ("transformers", "torch", "sentencepiece", "sacremoses")),
                           ("vieneu", ("vieneu",)), ("separate", ("audio_separator",))):
        absent = [name for name in modules if importlib.util.find_spec(name) is None]
        typer.echo(f"{extra}: {'installed' if not absent else 'missing ' + ', '.join(absent)}")
        if extra == "core" and absent:
            missing = True
    if missing:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
