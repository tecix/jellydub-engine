# Optional background separation

JellyDub selects `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt`
through the local `audio_separator.separator.Separator` API. Install the existing
`separate` extra to enable it. The SDK downloads the checkpoint/config on first
use and caches them under the work directory's `separation-models/`; later
inference runs locally. Only its explicitly identified Instrumental output is
accepted, converted to 48 kHz stereo PCM WAV, and atomically cached. Cache identity
includes source path, size, modification time, model and device. A changed source
or model triggers separation again. `device="cpu"` explicitly selects CPU;
`auto` uses the SDK's hardware selection. Models load only inside this stage.

## Choice and limits

This is an integration/documentation evaluation, not an empirical listening or
speed benchmark: no model weights were downloaded during development. CPU
execution is supported by the SDK, but full-movie runtime and memory consumption
have not been measured. Expect expensive inference; voiceover is the practical
low-resource option. The SDK documents additional inference overhead for
RoFormer configurations with high overlap. There is no real-time speed claim.

Mel-Band Roformer is the simplest integration here: one SDK-supported checkpoint
and a two-stem output, without a separate inference framework. Its music-vocal
training is a limitation for films: dialogue can leak into the background and
music vocals or effects can be removed. Replacement quality requires listening
on representative movie clips before use.

MVSEP-CDX23 is better matched conceptually to film: its Demucs-based model
separates dialogue, music and effects. Its upstream release uses a standalone
inference script, and its strongest result uses three checkpoints. Upstream
reports mean SDR 9.62 for the single model and 10.16 for the ensemble on DnR,
with the high-quality option about three times slower. Those dataset results
are not JellyDub quality measurements. Integrating that separate inference
stack is outside this lightweight SDK adapter. Selecting an arbitrary custom
model that lacks an Instrumental/background output fails explicitly rather than
mistaking dialogue for background.

The `python-audio-separator` code and MVSEP-CDX23 repository are MIT licensed.
That does **not** establish the license of downloaded Mel-Band checkpoint weights:
a clear checkpoint-specific license was not established from the consulted
upstream material. No weights are bundled; check the model author's terms before
redistribution or commercial use. Keep this limitation in third-party notices.

Missing extras, inference failures, ambiguous stems and duration changes raise
`JellyDubError` suggesting `--mix voiceover`. This module never silently replaces
separation with another mixing mode. Real inference/quality validation remains
an optional integration check; unit tests use an in-process fake SDK.

## Mixing and Jellyfin output

Voiceover uses sidechain compression with 20 ms attack and 300 ms release. A
parallel bed floor bounds the attenuation at `duck_db`; actual attenuation depends
on speech amplitude. Replacement adds the background and dub without normalizing
away their configured levels. All mixes downmix inputs (including 5.1 E-AC3) to
48 kHz stereo. Very loud summed inputs can clip PCM output; listen and lower
`dub_gain_db` if needed.

External audio uses `<stem>.AI-Dub.vi.mp3`; subtitles use `<stem>.vi.srt`.
Jellyfin's documented `Film.English Commentary.en.mp3` example confirms the
`<stem>.<title>.<language>.<extension>` layout. Optional MKV export copies all
original streams, metadata and chapters, encoding only the appended dub to AAC
with `language=vie` and a Vietnamese title. It keeps the original 5.1 audio and
video codecs. An original stream unsupported by Matroska causes an explicit
FFmpeg error; no original stream is silently dropped. Exports use a temporary
sibling and rename only after success, and refuse input/output path collisions.

## Sources checked 2026-10-26

- [Audio Separator upstream API, CPU installation, license and performance notes](https://github.com/nomadkaraoke/python-audio-separator).
- [Audio Separator model catalog](https://github.com/nomadkaraoke/python-audio-separator/blob/main/audio_separator/models.json).
- [MVSEP-CDX23 architecture, metrics and MIT license](https://github.com/ZFTurbo/MVSEP-CDX23-Cinematic-Sound-Demixing).
- [Jellyfin external subtitle/audio naming](https://jellyfin.org/docs/general/server/media/movies/#external-subtitles-and-audio-tracks).
