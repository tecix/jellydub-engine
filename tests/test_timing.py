import shutil
import struct
import wave
from pathlib import Path

import pytest

from jellydub.ffmpeg import run_ffmpeg
from jellydub.models import Cue, JellyDubError, PlacedClip, SynthResult, SAMPLE_RATE
from jellydub.timing import align, plan_alignment, render_timeline


def result(index, length):
    return SynthResult(index, Path(f"{index}.wav"), length)


def test_plan_uses_next_start_and_preserves_order():
    cues = [Cue(1, 1, 1.2, "one"), Cue(2, 3, 4, "two")]
    clips = plan_alignment(cues, [result(1, 1.5), result(2, .5)])
    assert [c.start for c in clips] == [1, 3]
    assert clips[0].tempo == 1
    assert all(c.warning is None for c in clips)


def test_plan_caps_tempo_and_warns_without_truncation():
    cues = [Cue(1, 0, .5, "one"), Cue(2, .5, 1, "two"), Cue(3, .6, 1, "three")]
    clips = plan_alignment(cues, [result(i, 3) for i in range(1, 4)])
    for before, after in zip(clips, clips[1:]):
        assert after.start >= before.start + before.duration + .009999
    for clip in clips:
        assert clip.tempo <= 1.25
        assert clip.duration * clip.tempo == pytest.approx(3)
        assert "exceeds max_overhang" in clip.warning
    assert "shifted" in clips[1].warning


def test_plan_mild_speedup_and_missing_results():
    clips = plan_alignment([Cue(1, 0, .5, "a"), Cue(2, 1, 2, "b")], [result(1, 1.1)])
    assert len(clips) == 1
    assert 1 < clips[0].tempo < 1.25
    assert clips[0].start + clips[0].duration < 1


@pytest.mark.parametrize("kwargs", [{"max_tempo": .9}, {"max_tempo": float("nan")}, {"max_overhang": -1}])
def test_plan_rejects_bad_options(kwargs):
    with pytest.raises(JellyDubError):
        plan_alignment([], [], **kwargs)


def write_pcm(path, seconds=.1):
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
        stream.writeframes(struct.pack("<h", 1000) * round(seconds * SAMPLE_RATE))


def test_render_placement_and_exact_duration(tmp_path):
    source = tmp_path / "clip [1].wav"
    write_pcm(source)
    output = tmp_path / "timeline.wav"
    render_timeline([PlacedClip(1, source, .345, .1)], 1.12345, output)
    with wave.open(str(output), "rb") as stream:
        assert stream.getnframes() == round(1.12345 * SAMPLE_RATE)
        assert stream.getparams()[:3] == (1, 2, SAMPLE_RATE)
        samples = struct.unpack(f"<{stream.getnframes()}h", stream.readframes(stream.getnframes()))
    onset = next(i for i, value in enumerate(samples) if value)
    assert onset / SAMPLE_RATE == pytest.approx(.345, abs=.02)
    assert all(value == 0 for value in samples[round(.445 * SAMPLE_RATE):])


def test_render_many_clips_and_empty_timeline(tmp_path):
    source = tmp_path / "tiny.wav"
    write_pcm(source, .001)
    output = tmp_path / "many.wav"
    render_timeline([PlacedClip(i, source, i * .002, .001) for i in range(2000)], 4, output)
    with wave.open(str(output), "rb") as stream:
        assert stream.getnframes() == 4 * SAMPLE_RATE
    render_timeline([], .1, output)
    with wave.open(str(output), "rb") as stream:
        assert stream.readframes(SAMPLE_RATE) == bytes(9600)


def test_render_preserves_existing_output_on_overflow_and_overlap(tmp_path):
    source = tmp_path / "clip.wav"
    write_pcm(source)
    output = tmp_path / "existing.wav"
    output.write_bytes(b"existing")
    for clips in ([PlacedClip(1, source, .95, .1)],
                  [PlacedClip(1, source, 0, .1), PlacedClip(2, source, .05, .1)]):
        with pytest.raises(JellyDubError):
            render_timeline(clips, 1, output)
        assert output.read_bytes() == b"existing"
        assert not (tmp_path / "existing.part.wav").exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg required")
def test_align_tone_fades_and_cache(tmp_path, monkeypatch):
    raw = tmp_path / "speech [test].wav"
    run_ffmpeg(["-f", "lavfi", "-i", "sine=frequency=440:duration=1.3", "-ar", str(SAMPLE_RATE), "-ac", "1", str(raw)])
    cues = [Cue(1, .1, 1.1, "speech")]
    clips = align(cues, [SynthResult(1, raw, 1.3)], tmp_path / "aligned")
    assert clips[0].tempo == 1.25
    assert clips[0].duration == pytest.approx(1.3 / 1.25, abs=.04)
    with wave.open(str(clips[0].path), "rb") as stream:
        samples = struct.unpack(f"<{stream.getnframes()}h", stream.readframes(stream.getnframes()))
    assert abs(samples[0]) <= 1
    assert abs(samples[-1]) < 100
    monkeypatch.setattr("jellydub.timing.run_ffmpeg", lambda args: pytest.fail("cache miss"))
    assert align(cues, [SynthResult(1, raw, 1.3)], tmp_path / "aligned")[0].path == clips[0].path
    render_timeline(clips, 2, tmp_path / "rendered.wav")


@pytest.mark.parametrize("drift,threshold,warn", [
    (.005, .02, False), (.011, .02, False), (.02, .02, True),
    (.03, .02, True), (.011, .01, True),
])
def test_align_measured_drift_warning_threshold(tmp_path, monkeypatch, drift, threshold, warn):
    raw = tmp_path / "speech.wav"
    write_pcm(raw)
    monkeypatch.setattr("jellydub.timing.run_ffmpeg", lambda args: shutil.copyfile(raw, args[-1]))
    measured = iter([.99 + drift, .1])
    monkeypatch.setattr("jellydub.timing.duration", lambda path: next(measured))
    clips = align(
        [Cue(1, 0, 1, "a"), Cue(2, 1, 2, "b")],
        [SynthResult(1, raw, .99), SynthResult(2, raw, .1)],
        tmp_path / "aligned", warn_threshold=threshold,
    )
    assert clips[1].start == pytest.approx(1 + drift)
    assert bool(clips[1].warning) is warn
    if warn:
        assert "shifted" in clips[1].warning


def test_align_warns_on_total_shift_not_only_measured_increment(tmp_path, monkeypatch):
    raw = tmp_path / "speech.wav"
    write_pcm(raw)
    monkeypatch.setattr("jellydub.timing.run_ffmpeg", lambda args: shutil.copyfile(raw, args[-1]))
    measured = iter([1.015, .1])
    monkeypatch.setattr("jellydub.timing.duration", lambda path: next(measured))
    clips = align(
        [Cue(1, 0, 1, "a"), Cue(2, 1, 2, "b")],
        [SynthResult(1, raw, 1.005), SynthResult(2, raw, .1)],
        tmp_path / "aligned", max_tempo=1,
    )
    assert clips[0].warning is None  # Measured overflow is only 15 ms.
    assert clips[1].start == pytest.approx(1.025)
    assert "shifted by 0.025s" in clips[1].warning


@pytest.mark.parametrize("overflow,warn", [(.011, False), (.02, True), (.1, True)])
def test_plan_overflow_warning_threshold(overflow, warn):
    clip = plan_alignment([Cue(1, 0, 1, "a")], [result(1, 1 + overflow)], max_tempo=1)[0]
    assert bool(clip.warning) is warn
    if warn:
        assert "overflow" in clip.warning


@pytest.mark.parametrize("threshold", [-.01, float("nan"), float("inf")])
def test_plan_rejects_invalid_warning_threshold(threshold):
    with pytest.raises(JellyDubError, match="warn_threshold"):
        plan_alignment([], [], warn_threshold=threshold)
