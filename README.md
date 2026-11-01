# JellyDub Engine

JellyDub turns English or Vietnamese SRT subtitles into a synchronized Vietnamese audio track for Jellyfin. It uses filesystem caches, FFmpeg, and pluggable speech engines. The original movie stays untouched. No transcription, database, server, or paid API is required.

Edge-TTS is the default and sends subtitle text to Microsoft's online speech service. Default OPUS-MT translation, alignment, mixing, and optional VieNeu synthesis run locally; model downloads need internet once. Optional `--translator gemini` uses the official online Gemini API; `agy` is experimental. Edge-TTS is an unofficial client, requires internet for uncached speech, and does not clone voices.

## Ubuntu installation

Install FFmpeg and [uv](https://docs.astral.sh/uv/getting-started/installation/), then from this checkout:

```bash
sudo apt update
sudo apt install ffmpeg
uv sync --extra translate
uv run jellydub doctor
```

Python 3.11–3.13 is supported. `uv sync` alone suffices for already Vietnamese subtitles with Edge-TTS. Optional local synthesis and background separation:

```bash
uv sync --extra translate --extra vieneu --extra separate
```

Extras may download large packages. Model weights are fetched on their first actual use. `doctor` checks executable/package availability without downloading or loading models; it does not verify service connectivity or whether weights are cached.

## Find existing subtitles first

`dub` can run without `--srt`. The default `--subs auto` gathers local and embedded Vietnamese subtitles and compares them with online Vietnamese candidates when the optional extra is installed and downloads are enabled. Only if no acceptable Vietnamese candidate is found does it translate a local or embedded English subtitle. An explicit `--srt` takes priority over discovery.

```bash
uv run jellydub dub movie.mkv --no-download --out-dir ./dubbed
# Enable optional online subtitle providers:
uv sync --extra opensubtitles
uv run jellydub subs movie.mkv --lang vi --out-dir ./dubbed
uv run jellydub dub movie.mkv --out-dir ./dubbed
```

Online searches are skipped in `dub --subs auto` and `subs` when a local/sidecar/embedded Vietnamese candidate has quality ≥0.9 and verified sync with matched ratio ≥0.9 and residual ≤0.1 seconds. The log says `skipping online search: good local subtitle`. Use `--search-online` to compare online candidates anyway (or set `search_online=true` in TOML). `--no-download` still disables network searches even with this override. An unverified timing match does not qualify for the shortcut.

`jellydub subs` lists candidate origins, match scores and notes, checks timing, and saves the best accepted subtitle as `<movie-stem>.vi.srt`. It never replaces different existing output without `--force`; identical output is left untouched. Use `--no-download` for a local-only search. Local searches include the movie directory and `subtitle/`, `subtitles/`, and `Subs/`; text subtitle streams can be extracted from the movie into cache. Bitmap subtitles need conversion outside JellyDub.

Candidates receive a final score of **0.4 × metadata prescore + 0.4 × local quality + 0.2 × sync quality**. Local/embedded files receive metadata prescore 1.0; downloads use the provider's prescore based on release match, trusted uploader and popularity, with penalties for machine translation. Quality checks cover encoding corruption, untranslated lines, duplicate/empty cues, timing errors, reading speed and coverage. Quality below 0.5 is rejected, so a broken local file cannot displace an acceptable download. Sync quality is matched ratio × (1 − residual/0.3), bounded below at zero; without a reference it is a neutral 0.5. These are quick screening heuristics, not a guarantee of translation accuracy; review the chosen result.

`subs` prints a ranked table with origin, public uploader/trust facts, downloads, rating, quality, sync offset, final score and issues. Missing metadata is shown as unknown. After selection, junk/ad/credit cues are stripped into a cached copy before publication; source files stay unchanged. Local-only and explicit Vietnamese selections receive the same quality checks. If correction or cleanup is needed for an explicit subtitle already at the default output path, use a separate `--out-dir` to preserve that source.

Forced English tracks are excluded from timing references. Among matching English tracks, the one with the most parsed cues is preferred (embedded wins a tie), so a full SDH track takes priority over a sparse track. References with fewer than 30% of a Vietnamese candidate’s cues are skipped in favour of the next reference; if none is sufficiently complete, synchronization is marked unverified. Vietnamese candidates, including explicit Vietnamese SRTs, are checked for offset and frame-rate drift. Matches below 50% or with median residual above 0.3 seconds are rejected; discovery tries the next candidate. Logs show rejection reasons and the selected offset/scale. If no English reference exists, the candidate is used with an explicit unverified-sync notice; review it before dubbing. Corrections are written to cache; found source files stay untouched. An explicit Vietnamese file that fails the timing check stops the command instead of being translated.

`--subs local` searches local/embedded Vietnamese only, and `--subs download` searches online Vietnamese only; both stop if none is accepted. `--subs translate` selects local/embedded English for translation. `--no-download` always disables online searches. An explicit SRT remains authoritative in every mode. To preview and review sourcing separately:

```bash
uv run jellydub subs movie.mkv --no-download --out-dir ./dubbed
uv run jellydub dub movie.mkv --srt dubbed/movie.vi.srt --out-dir ./dubbed
# Override providers (repeat the option or use comma-separated names):
uv run jellydub subs movie.mkv --providers opensubtitlescom,podnapisi --providers gestdown
```

Online subtitle downloads may require an OpenSubtitles account and are subject to provider quotas. The defaults are `opensubtitlescom`, `podnapisi`, and `gestdown`; at most two best candidates are downloaded per search, with cached downloads reused. Set `OPENSUBTITLES_USERNAME`, `OPENSUBTITLES_PASSWORD`, and optionally `OPENSUBTITLES_APIKEY` in your environment. Never put credential values in TOML or CLI arguments. Config `subtitle_credentials_env` can map `username`, `password`, and `apikey` to alternative environment variable **names**. Missing extras or provider failures allow automatic discovery to fall back to English; strict modes report no acceptable Vietnamese subtitle. Providers receive video identity/release information during an online search.

## Translate, review, dub

```bash
uv run jellydub translate movie.mkv --srt movie.en.srt --out-dir ./dubbed
# Review/edit dubbed/movie.vi.srt, then:
uv run jellydub dub movie.mkv --srt dubbed/movie.vi.srt --out-dir ./dubbed
```

`--source-lang auto` is the default for both commands. A `.vi.` or `.vie.` filename tag, or Vietnamese diacritics in more than 30% of nonempty cues, selects Vietnamese and skips translation entirely, including translator imports and model downloads. The decision is logged. Ambiguous subtitles default to English; use `--source-lang vi` or `--source-lang en` to override. Vietnamese subtitles bypass translation; selected files may receive timing correction and junk removal in a cached copy. Sources are never overwritten.

### Translation backends

`--translator opus-mt` is the offline default: the model runs locally after its initial download. Its output can contain unnatural wording or repeated phrases; generation guards reduce repetitions but manual review is still recommended.

For translation quality, we recommend `--translator gemini`, the official **online** Gemini API backend. Obtain an API key from [Google AI Studio](https://aistudio.google.com/apikey) and set `GEMINI_API_KEY` in your environment (`GOOGLE_API_KEY` is a fallback). No additional Python dependency is needed. It sends subtitle text and dialogue context to Google. A [free tier](https://ai.google.dev/gemini-api/docs/billing) is available for eligible models, subject to quotas; free-tier content may be used to improve Google products, with regional exceptions described in the [Gemini API terms](https://ai.google.dev/gemini-api/terms). Review those terms before sending private material.

```bash
export GEMINI_API_KEY='YOUR_API_KEY' # replace privately; never commit or log the key
uv run jellydub translate movie.mkv --srt movie.en.srt --translator gemini --out-dir ./dubbed
# Review/edit dubbed/movie.vi.srt, then synthesize without re-translating:
uv run jellydub dub movie.mkv --srt dubbed/movie.vi.srt --out-dir ./dubbed
```

Use `--translator-model MODEL_ID` to override the selected backend's model; leaving it unset uses the backend default. Both options also work with `dub` and in TOML as `translator` / `translator_model`. Keep API keys in environment variables, outside TOML and command-line options.

`--translator agy` remains an **experimental online** backend using the Antigravity CLI and a Google account signed in through that CLI. It also sends subtitle text and context to Google. Review [Antigravity Additional Terms §6](https://antigravity.google/terms) before scripted use: restrictions on third-party use and possible account suspension make the official Gemini API the recommended programmatic route. Install/sign in to Antigravity separately if choosing this backend.

Vietnamese input still skips translation entirely, regardless of the selected backend. Changing translator or model selects a separate work cache so a previously translated SRT from another backend is not reused.

One-step examples:

```bash
uv run jellydub dub movie.mkv --srt movie.en.srt --tts edge --voice vi-VN-NamMinhNeural --mix voiceover
uv run jellydub dub movie.mkv --srt movie.en.srt --tts vieneu --reference actor.wav --mix replace
uv run jellydub dub movie.mkv --srt movie.vi.srt --source-lang vi --tts edge --mix dub-only
```

Both `vi-VN-HoaiMyNeural` (default) and `vi-VN-NamMinhNeural` are available through Edge-TTS. Set `--rate '+5%'` and `--pitch '+0Hz'` to adjust Edge synthesis. VieNeu uses its own preset names; omit `--voice` to select its default. Local VieNeu uses CPU when `--device auto`; select an explicitly supported device for GPU execution.

Set `--tts-workers N` (or TOML `tts_workers`) to control parallel speech requests; the default is 4. We recommend 3–4 workers for Edge-TTS; higher values risk throttling. Concurrency is capped by the provider (Edge: 8, VieNeu: 1). Transient failures reduce concurrency and retry with backoff; sustained successes gradually restore it. Identical text/speaker pairs share synthesis within a run. Changing the worker count reuses existing cache entries.

```bash
uv run jellydub dub movie.mkv --srt movie.vi.srt --tts edge --tts-workers 3
```

For Edge-TTS HTTP 403 errors, check system clock synchronization and whether `edge-tts` is outdated; these are usual causes. Increasing worker count is unlikely to help. Re-run after correcting the cause to reuse successful cached segments.

Manual voice labels can use `TYSON: text` or `[[TYSON]] text` in SRT cues. With VieNeu, use one clean 3–8 second reference as the default, or repeat labelled references:

```bash
uv run jellydub dub movie.mkv --srt movie.vi.srt --tts vieneu \
  --reference 'TYSON=tyson.wav' --reference 'NARRATOR=narrator.wav' \
  --out-dir ./dubbed
```

There is no automatic speaker identification. Missing/unsuitable reference clips fall back to the preset with a warning. Voice profiles are cached under the configured cache directory.

## Mixing and outputs

- `--mix dub-only`: Vietnamese dialogue over silence.
- `--mix voiceover`: original stereo/downmixed audio, ducked under Vietnamese speech.
- `--mix replace`: expensive local separation to retain background music/effects. Failure stops with guidance; `--fallback-voiceover` explicitly allows automatic fallback. See [separation notes](docs/separation.md).

`--audio-stream N` selects the zero-based original audio stream (`a:N`), including for 5.1 sources. Mixing downmixes to stereo; optional MKV export preserves original streams.

```bash
uv run jellydub dub 'movie[1].mkv' --srt 'movie[1].vi.srt' \
  --source-lang vi --mix voiceover --audio-stream 0 --mkv --out-dir ./dubbed
```

Outputs follow [Jellyfin's external-file naming](https://jellyfin.org/docs/general/server/media/movies/#external-subtitles-and-audio-tracks):

```text
movie.vi.srt
movie.AI-Dub.vi.mp3
movie.AI-Dub.vi.mkv   # only with --mkv
```

External files normally belong beside the movie in the Jellyfin library. `--out-dir` writes into a separate destination for review; move approved sidecars next to the movie for library discovery. The optional MKV keeps the original video/audio streams via stream copy and adds the dub. Existing generated outputs can be replaced; source video and input subtitles are protected.

## Configuration and recovery

Copy [jellydub.example.toml](jellydub.example.toml), then pass `--config FILE`. Explicit CLI options override TOML. Use `--no-mkv` or `--no-fallback-voiceover` to override enabled config flags.

```bash
uv run jellydub dub movie.mkv --srt movie.vi.srt --config jellydub.example.toml \
  --cache-dir /tmp/jellydub-cache --out-dir ./dubbed
```

Work directories default to `~/.cache/jellydub/<movie-stem>-<hash>/`. The key includes input identity, subtitle content, references, and output settings (excluding worker count and source-search controls). Translation and per-segment audio are cached; complete timeline/mix/encoding stages are reused. Interrupted stage files are never treated as complete. Re-run the same command after an error. Failed TTS segments produce a warning summary and a nonzero exit; successful speech is cached and no incomplete dub is exported.

Progress reports cue counts, including long subtitle files. Timing uses mild pitch-preserving speed changes, bounded by `--max-tempo` (default 1.25), and reports overflow warnings. Speech that extends beyond the end of the movie stops export instead of being silently truncated. Shorten the final cue's text and re-run.

## Watch on-demand dubbing inside Jellyfin

`jellydub stream` serves a seekable HLS video with Vietnamese speech mixed into its audio. Open it
in the normal Jellyfin TV player; the TinyTV apps are not needed for this mode. Supply a local,
seekable SDR video and a matching English or Vietnamese SRT:

```bash
uv run jellydub stream '/media/Movies/Movie/Movie.mkv' \
  --srt '/media/Movies/Movie/Movie.en.srt' \
  --public-url http://192.168.2.10:8766 \
  --strm '/media/Movies/Movie Vietnamese/Movie Vietnamese.strm'
```

Replace the example IP with the laptop's LAN address. Both the Jellyfin server and TV must reach
it. Put the `.strm` in a folder already covered by a Jellyfin library, scan that library, then play
the new **Movie Vietnamese** item. Keep the command running while watching. The original video
is untouched. The command prints its playlist URL; omit `--strm` to use that URL in another HLS
player. A different existing shortcut is protected unless you pass `--force`.

The translator, model, voice, and TTS options work as in `serve`. For the existing experimental
Antigravity setup, add `--translator agy-stream --translator-model gemini-3.8-flash-low`.
Vietnamese SRTs skip translation (`--source-lang vi` overrides detection).

The server prepares an opening buffer (18 seconds by default), then produces missing video
segments when the player requests them. Seeking requests the segment at the new position;
translation and speech prioritize that section rather than generating the whole movie first.
Speech that crosses a segment boundary is included on both sides without losing its tail.
Completed segments survive restarts under `~/.cache/jellydub/hls/`; restart the same command to
reuse them and the same URL. Changing the video identity, subtitles, voice, translator settings,
mix, or segment length uses a different cache and URL, so republish/rescan the shortcut.

Useful controls:

```bash
uv run jellydub stream Movie.mkv --srt Movie.vi.srt \
  --source-lang vi --mix voiceover --audio-stream 0 \
  --segment-seconds 6 --buffer-seconds 18 --wait-seconds 120 --port 8766
```

`--mix voiceover` is the streaming default: original audio is ducked while Vietnamese speech plays.
`--mix dub-only` uses speech over silence. TOML settings are supported with `--config`; explicit
CLI options take precedence. `--buffer-seconds 0` warms only the first video segment and disables
speech lookahead. `--no-warmup` starts HTTP immediately, leaving preparation to the first request.
The server auto-detects a LAN address unless `--public-url` is supplied. `GET /v1/health` reports
the playlist path and duration. Uncached segment HEAD probes return 503 without starting work;
GET requests wait for generation and return 503 with `Retry-After: 2` if their preparation times out.

This first version encodes **H.264, SDR, up to 1920×1080 at 30 fps, with stereo AAC**; it does not
copy the source video, preserve HDR/5.1, accept remote URLs/`.strm` inputs, or perform background
separation. Encoding runs one segment at a time to bound CPU use. A slow laptop, cold translation,
or a seek into uncached dialogue can buffer; startup/seek latency depends on the source and
services. Independently encoded TS segments retain their position on the movie's timeline. Playback and seek
compatibility on a particular Jellyfin/TV combination still need a device check.

Speech that cannot fit before the next subtitle cue (or the final cue's end) fails that segment
instead of exporting truncated/missing dialogue. Shorten the reported cue or increase `--max-tempo`
(up to 2), then restart. Translation, speech, and encoder errors are logged and never publish a
partial segment. The server exposes only its configured stream and has no authentication; use a
trusted LAN/tailnet. Segment caches can grow to the size of a full encoded movie; remove an old
`hls/<id>/` directory while its server is stopped to reclaim space.

## Live dubbing server

`jellydub serve` runs an HTTP server that dubs on demand for the TinyTV Dub app: a client POSTs the subtitle track of the video that is playing, then polls for Vietnamese clips around its playhead. Translation and Edge-TTS run ahead of the playhead (cues within about 90 s first, re-prioritised at once on a seek), each clip is tempo-fitted to its slot (`--max-tempo`, default 1.35) and served as mono 24 kHz s16le WAV. Translations and clips share the normal caches, so re-watching is instant. There is no authentication; use it on a trusted LAN or tailnet only.

```bash
uv run jellydub serve                       # 0.0.0.0:8765, opus-mt + Edge-TTS
uv run jellydub serve --port 8765 --translator gemini --voice vi-VN-NamMinhNeural
uv sync --extra live                        # optional: advertise _tinytv-dub._tcp over mDNS
```

### Translation on a CPU laptop

For the i7-1265U laptop with about 15 GiB RAM, online Gemini translation avoids loading a local language model alongside speech and audio processing. The official `--translator gemini` backend remains the recommended API route. OPUS-MT remains the lightweight offline option; a larger local LLM needs a separate quality and latency benchmark before using it for live playback.

For an existing Antigravity login, `--translator agy-stream` is an opt-in experimental alternative. It keeps a sandboxed `agy` process warm using Antigravity's documented [streaming JSON interface](https://antigravity.google/docs/cli/headless), submits small subtitle batches, and uses structured responses. Herdr can host the server; translation uses pipes directly rather than reading another pane's screen. The worker starts lazily, rotates periodically to bound conversation growth, and closes with the server. It still sends subtitle text to Google and uses your account's quota.

```bash
uv run jellydub serve --translator agy-stream --translator-model gemini-3.8-flash-low
```

The streaming backend defaults to `gemini-3.8-flash-low`, verified in this machine's `agy models` list on 2026-10-31. Keep the model override explicit if availability changes. The requested Gemini 3.5 Flash is absent from that list; Google's [model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash) marks the API model deprecated. Existing `agy` calls retain their separate-process behavior and medium-effort default.

A three-line Flash Low smoke test on this machine took 9.2 seconds end to end (5.3 seconds reported by the CLI), returning natural Vietnamese with zero thinking tokens. A persistent-worker check then took 8.9 seconds for the first two-cue batch and 2.4 seconds for the next two-cue batch, using the same process. These are translation-only samples, not an end-to-end TV latency guarantee. Network latency, quotas, uncached speech, and cold startup still affect playback. Run the normal offline backend by omitting `--translator`.

On-device check (2026-10-31, Philips Android 5.1): casting an English-subtitled lesson through `agy-stream` produced an active mono 24 kHz Vietnamese track and −12 dB movie ducking; pause restored the movie volume. A pause → seek → resume test triggered firmware AAC decoder timeouts and a stalled video playhead even though translated clips were ready. Recasting recovered playback. The server now waits for the first playhead hint before scheduling, avoiding translation of opening cues when playback starts mid-video. The user listened on the TV and confirmed the result sounded good.

Endpoints: `GET /v1/health`, `POST /v1/sessions` (`{"srt","source_lang","title"}`), `GET /v1/sessions/{id}/cues?from=&to=`, `GET /v1/sessions/{id}/audio/{n}.wav`, `DELETE /v1/sessions/{id}`. mDNS advertises only the IPv4 address of the default-route interface (clients resolve a single address); override with `--advertise-ip IP` (repeatable). Sessions expire 30 minutes after their last request. The translation model is loaded on the first English session.

## Tests

Offline unit tests use fakes and generated media; no model downloads or network requests:

```bash
.venv/bin/python -m pytest tests/test_cli.py tests/test_pipeline.py -q
```

Generate a reproducible 15-second synthetic movie and run the real Edge-TTS integration test:

```bash
.venv/bin/python tests/e2e/generate_sample.py /tmp/jellydub-sample.mkv
.venv/bin/python -m pytest tests/e2e/test_e2e.py -m 'e2e and not slow' -q
# Optional English translation variant (downloads the local translation model):
.venv/bin/python -m pytest tests/e2e/test_e2e.py -m 'e2e and slow' -q
```

The integration test generates its own movie, uses committed short SRT samples, validates audio and MKV tracks, checks the source is unchanged, and reruns to verify synthesis cache reuse. E2E/slow tests are excluded by default; Edge requires working network access.

## Limitations and licenses

Subtitle timing is preserved, but dense dialogue may require manual edits. Translation quality and Vietnamese pronunciation vary. Auto language detection is a heuristic: unaccented Vietnamese needs `--source-lang vi`. Separation can leave dialogue remnants or remove wanted sounds, and may be slow on CPU. Intermediate uncompressed audio and optional MKV caches need substantial disk space for long movies. Only one heavy model is used at a time.

JellyDub is MIT-licensed. Third-party software and downloaded weights retain their own terms; see [third-party license notes](LICENSES-THIRD-PARTY.md). OPUS-MT public models are generally described as CC-BY-4.0, while the selected Helsinki-NLP model card currently labels this specific English–Vietnamese checkpoint Apache-2.0; retain attribution and verify the downloaded revision. VieNeu v3 Turbo code/weights use Apache-2.0. Separation checkpoint licensing depends on the exact downloaded model. Microsoft's service terms apply separately from the Edge-TTS client's open-source license.

## Standalone installation

```bash
git clone https://github.com/tecix/jellydub-engine.git
cd jellydub-engine
uv sync --extra translate
uv run jellydub doctor
uv run jellydub dub movie.mkv --srt movie.vi.srt --source-lang vi --out-dir ./dubbed
```

This repository includes the Python engine, CLI, live API and experimental HLS server. Jellyfin and TinyTV are optional clients. Package imports remain experimental; use the CLI or documented HTTP API for integrations. Install in another environment with `pip install git+https://github.com/tecix/jellydub-engine.git`.

## Roadmap

These phases are planned work, with no committed delivery dates.

| Phase | Deliverables | Completion criteria |
| --- | --- | --- |
| 0 — Standalone engine | Python package, CLI, live API, experimental HLS, documentation | Independent installation and passing offline tests |
| 1 — Release foundations | CI, tagged releases, stable API/config contracts | Reproducible wheels and compatibility policy |
| 2 — Streaming reliability | Seek cancellation, bounded queues, cache eviction, retry budgets | Repeatable startup/seek benchmarks on Jellyfin and Android TV |
| 3 — Quality | Translation evaluation set, pronunciation controls, speaker mapping | Reproducible quality and latency comparisons |
| 4 — Deployment | Container, authenticated API, integration examples | Documented deployment connected to a real client |
| 5 — Scale | Persistent queues, metrics, resource limits, optional GPU workers | Recovery and load tests with documented capacity |

## Development

```bash
uv sync --group dev
uv run pytest -q
uv build
```

Do not commit credentials, media, model weights or caches. Network/model tests are opt-in as described above.
