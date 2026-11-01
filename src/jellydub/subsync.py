"""Pure subtitle timing synchronization, independent of language and providers."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from .models import Cue, JellyDubError


@dataclass
class SyncResult:
    offset: float
    scale: float
    matched_ratio: float
    residual: float


_TOLERANCE = 0.25
_BIN = 0.1


def _starts(cues: list[Cue]) -> list[float]:
    if any(not math.isfinite(c.start) or not math.isfinite(c.end) or c.end < c.start for c in cues):
        raise JellyDubError("Subtitle synchronization requires finite, ordered cue timestamps")
    return sorted(c.start for c in cues)


def _matches(candidate, reference, scale, offset):
    """Select unique, order-preserving nearest pairs within the inlier tolerance."""
    possible = []
    for i, value in enumerate(candidate):
        target = value * scale + offset
        lo = bisect_left(reference, target - _TOLERANCE)
        hi = bisect_right(reference, target + _TOLERANCE)
        for j in range(lo, hi):
            possible.append((abs(reference[j] - target), i, j))
    used_c, used_r, pairs = set(), set(), []
    for _, i, j in sorted(possible):
        if i not in used_c and j not in used_r:
            used_c.add(i)
            used_r.add(j)
            pairs.append((i, j))
    ordered = []
    last_j = -1
    for i, j in sorted(pairs):
        if j > last_j:
            ordered.append((candidate[i], reference[j]))
            last_j = j
    return ordered


def _fit(pairs):
    mean_x = sum(x for x, _ in pairs) / len(pairs)
    mean_y = sum(y for _, y in pairs) / len(pairs)
    variance = sum((x - mean_x) ** 2 for x, _ in pairs)
    scale = sum((x - mean_x) * (y - mean_y) for x, y in pairs) / variance if variance else 1.0
    return scale, mean_y - scale * mean_x


def estimate_sync(candidate: list[Cue], reference: list[Cue], max_offset: float = 30.0) -> SyncResult:
    """Vote on start differences, then fit scale/offset using only timing inliers.

    Common 23.976/24/25 fps hypotheses allow long-film drift to be found before
    least-squares refinement. Pairs are unique so extra SDH cues cannot inflate
    matched_ratio. No text comparison is needed across languages. Empty/unmatched
    inputs return identity, ratio zero and infinite residual (not a usable match).
    max_offset bounds the fitted intercept, not accumulated frame-rate drift.
    """
    if not math.isfinite(max_offset) or max_offset < 0:
        raise JellyDubError("max_offset must be finite and nonnegative")
    source, target = _starts(candidate), _starts(reference)
    best = SyncResult(0.0, 1.0, 0.0, math.inf)
    if not source or not target:
        return best
    fps_ratio = 25 / (24000 / 1001)
    scales = (1.0, fps_ratio, 1 / fps_ratio, 25 / 24, 24 / 25, 1001 / 1000, 1000 / 1001)
    best_rank = (0, -math.inf, -math.inf)
    for initial_scale in scales:
        votes = defaultdict(list)
        for value in source:
            scaled = value * initial_scale
            lo = bisect_left(target, scaled - max_offset)
            hi = bisect_right(target, scaled + max_offset)
            for other in target[lo:hi]:
                delta = other - scaled
                votes[round(delta / _BIN)].append(delta)
        # Adjacent bins compensate for quantization at bin boundaries.
        peaks = sorted(votes, key=lambda b: sum(len(votes.get(b + d, ())) for d in (-1, 0, 1)), reverse=True)[:8]
        for peak in peaks:
            offset = median([v for d in (-1, 0, 1) for v in votes.get(peak + d, ())])
            scale = initial_scale
            for _ in range(5):
                pairs = _matches(source, target, scale, offset)
                if len(pairs) < 3:
                    break
                # Discard the largest residuals before fitting; accidental SDH
                # correspondences should not pull the fit away from real speech.
                errors = [abs(y - (scale * x + offset)) for x, y in pairs]
                cutoff = min(_TOLERANCE, max(.04, 3 * median(errors)))
                inliers = [p for p, error in zip(pairs, errors) if error <= cutoff]
                if len(inliers) < 3:
                    break
                fitted_scale, fitted_offset = _fit(inliers)
                if not .9 <= fitted_scale <= 1.1 or abs(fitted_offset) > max_offset + 1e-9:
                    break
                scale, offset = fitted_scale, fitted_offset
            pairs = _matches(source, target, scale, offset)
            if not pairs or abs(offset) > max_offset + 1e-9:
                continue
            residual = median(abs(y - (scale * x + offset)) for x, y in pairs)
            rank = (len(pairs), -residual, -abs(scale - 1))
            if rank > best_rank:
                best_rank = rank
                best = SyncResult(offset, scale, len(pairs) / len(source), residual)
    return best


def apply_sync(cues: list[Cue], result: SyncResult) -> list[Cue]:
    """Return fresh cues with scaled/offset timestamps clamped to zero.

    Input sequence, indices, text and speakers are preserved; positive scale
    preserves chronological order and overlap relationships except zero clamping.
    """
    if not math.isfinite(result.offset) or not math.isfinite(result.scale) or result.scale <= 0:
        raise JellyDubError("Subtitle synchronization requires a finite offset and positive scale")
    _starts(cues)
    return [Cue(c.index, max(0.0, c.start * result.scale + result.offset),
                max(0.0, c.end * result.scale + result.offset), c.text, c.speaker) for c in cues]
