import math
import random

import pytest

from jellydub.models import Cue, JellyDubError
from jellydub.subsync import SyncResult, apply_sync, estimate_sync


def cues(starts):
    return [Cue(i + 1, start, start + 1, f"line {i}", "speaker") for i, start in enumerate(starts)]


def irregular_starts(count=500):
    rng = random.Random(82)
    time = 2.0
    values = []
    for _ in range(count):
        time += rng.uniform(2, 8)
        values.append(time)
    return values


@pytest.mark.parametrize("offset", [-.36, .45, 12.5, -25])
def test_pure_offset(offset):
    source = cues(irregular_starts())
    reference = apply_sync(source, SyncResult(offset, 1, 1, 0))
    result = estimate_sync(source, reference)
    assert result.offset == pytest.approx(offset, abs=.001)
    assert result.scale == pytest.approx(1, abs=1e-6)
    assert result.matched_ratio > .98
    assert result.residual < .001


@pytest.mark.parametrize("scale", [25 / (24000 / 1001), (24000 / 1001) / 25, 25 / 24, 1.002])
def test_fps_drift_over_long_movie(scale):
    source = cues(irregular_starts())
    reference = apply_sync(source, SyncResult(-.42, scale, 1, 0))
    result = estimate_sync(source, reference)
    assert result.scale == pytest.approx(scale, abs=1e-6)
    assert result.offset == pytest.approx(-.42, abs=.001)
    assert result.matched_ratio == 1
    assert result.residual < .001


def test_noise_missing_and_extra_sdh_cues():
    rng = random.Random(7)
    starts = irregular_starts()
    candidate = cues([start for i, start in enumerate(starts) if i % 7])
    reference = []
    for i, start in enumerate(starts):
        if i % 9:
            reference.append(Cue(i, start * 25 / (24000 / 1001) - .4 + rng.uniform(-.06, .06), start * 25 / (24000 / 1001) + 1, "English dialogue"))
        if i % 3 == 0:
            reference.append(Cue(1000 + i, start + .8, start + 1.1, "(wind) ♪"))
    result = estimate_sync(candidate, sorted(reference, key=lambda c: c.start))
    assert result.scale == pytest.approx(25 / (24000 / 1001), abs=.0001)
    assert result.offset == pytest.approx(-.4, abs=.03)
    assert .8 < result.matched_ratio <= 1
    assert result.residual < .06


def test_unrelated_subtitles_have_low_match_ratio():
    source = cues(irregular_starts())
    rng = random.Random(915)
    reference = cues(sorted(rng.uniform(0, 2700) for _ in range(450)))
    result = estimate_sync(source, reference)
    assert result.matched_ratio < .5


def test_matches_are_unique_with_duplicate_candidate_starts():
    result = estimate_sync(cues([1, 1, 1, 4, 8]), cues([1, 4, 8]))
    assert result.matched_ratio == pytest.approx(3 / 5)


def test_apply_sync_preserves_metadata_order_and_source():
    source = [Cue(7, .1, .3, "first", "A"), Cue(3, 1, 2, "second", "B")]
    output = apply_sync(source, SyncResult(-.5, 2, 1, 0))
    assert [(c.index, c.start, c.end, c.text, c.speaker) for c in output] == [
        (7, 0, pytest.approx(.1), "first", "A"), (3, 1.5, 3.5, "second", "B")]
    assert source[0].start == .1
    assert output[0] is not source[0]


def test_empty_and_no_matches():
    for source, reference in [([], cues([1])), (cues([1]), []), (cues([1]), cues([1000]))]:
        result = estimate_sync(source, reference)
        assert result == SyncResult(0, 1, 0, math.inf)


@pytest.mark.parametrize("value", [-1, math.inf, math.nan])
def test_invalid_offset_limit(value):
    with pytest.raises(JellyDubError):
        estimate_sync([], [], value)


@pytest.mark.parametrize("scale,offset", [(0, 0), (-1, 0), (math.nan, 0), (1, math.inf)])
def test_invalid_transform(scale, offset):
    with pytest.raises(JellyDubError):
        apply_sync(cues([1]), SyncResult(offset, scale, 0, 0))


def test_zero_offset_limit_allows_identity():
    source = cues(irregular_starts(20))
    result = estimate_sync(source, source, max_offset=0)
    assert result == SyncResult(0, 1, 1, 0)


def test_offset_limit_rejects_distant_subtitles():
    result = estimate_sync(cues([1, 5, 11]), cues([101, 105, 111]), max_offset=2)
    assert result.matched_ratio == 0


@pytest.mark.parametrize("source", [
    [Cue(1, math.nan, 2, "invalid")], [Cue(1, 2, 1, "invalid")],
])
def test_invalid_cue_times(source):
    with pytest.raises(JellyDubError):
        estimate_sync(source, cues([1]))
    with pytest.raises(JellyDubError):
        apply_sync(source, SyncResult(0, 1, 0, 0))
