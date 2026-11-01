# Third-party components and model terms

Checked against upstream sources on 2026-10-26. No model weights or third-party source are vendored in this repository. This inventory is not a replacement for the license files accompanying installed distributions or downloaded model revisions.

| Component | Upstream license / terms | Source |
| --- | --- | --- |
| edge-tts | LGPLv3; `srt_composer.py` alone is MIT | [license](https://github.com/rany2/edge-tts/blob/master/LICENSE) |
| Microsoft Edge speech endpoint | Online Microsoft service accessed by an unofficial client; not a locally licensed model, no offline service guarantee or voice cloning | [client README](https://github.com/rany2/edge-tts) |
| Helsinki-NLP OPUS-MT code | MIT | [code license](https://github.com/Helsinki-NLP/Opus-MT/blob/master/LICENSE) |
| Helsinki-NLP/opus-mt-en-vi weights | Specific current model card declares Apache-2.0; the broader OPUS-MT project describes public models as CC-BY-4.0. Preserve attribution to Helsinki-NLP / OPUS-MT and check the downloaded revision's terms | [model card](https://huggingface.co/Helsinki-NLP/opus-mt-en-vi), [project statement](https://github.com/Helsinki-NLP/Opus-MT) |
| VieNeu-TTS SDK | Apache-2.0 | [repository](https://github.com/pnnbao97/VieNeu-TTS) |
| VieNeu-TTS v3 Turbo weights | Apache-2.0; optional codec/phonemizer dependencies retain their own terms | [model card](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo) |
| python-audio-separator | MIT; credits to UVR and its developers for applicable UVR models | [license/credits](https://github.com/nomadkaraoke/python-audio-separator#license-) |
| Mel-Band Roformer weights | Exact checkpoint terms must be checked separately. The reference architecture repository does not expose a top-level license in its file listing; this is not evidence that every derived checkpoint is unrestricted | [reference repository](https://github.com/KimberleyJensen/Mel-Band-Roformer-Vocal-Model) |
| MVSEP-CDX23 cinematic demixing code | MIT; downloaded model and training data terms must be checked separately | [repository/license](https://github.com/ZFTurbo/MVSEP-CDX23-Cinematic-Sound-Demixing) |

The current optional separator selects `mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt` via audio-separator. Do not infer the weights' license from the MIT wrapper license. See [separation evaluation](docs/separation.md) for implementation details and model provenance; no separate license for that exact checkpoint was verified here.

FFmpeg is an external executable: its license depends on the build configuration (LGPL/GPL and enabled codecs). See [FFmpeg legal information](https://ffmpeg.org/legal.html). Python dependencies such as Typer, srt, Transformers, PyTorch, SentencePiece, and their transitive dependencies ship their own notices. Redistributors must preserve applicable notices and comply with the versions they distribute.
