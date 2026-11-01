"""Cheap, deterministic subtitle quality heuristics; no models or network.

Scores flag mechanical problems, not translation accuracy. Penalties are additive
and reported in ``stats['penalty_*']`` so callers can explain a ranking.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
import unicodedata

from .models import Cue


@dataclass
class QualityReport:
    score: float
    issues: list[str]
    stats: dict


_TAGS = re.compile(r"<[^>]*>|\{\\[^}]*\}")
_WORDS = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_VI = frozenset(
    "ăâđêôơưàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ"
)
_ENGLISH = frozenset(
    "the and is are was were this that these those with from for of to in on at "
    "we you they he she it my your our their his her have has had will would "
    "could should can not don't didn't it's i'm you're there here what where "
    "when why how who which but because if then than into about been being".split()
)
_JUNK = re.compile(
    r"opensubtitles|subscene|\bwww\.|https?://|"
    r"^\s*(?:translated\s+by|translation\s+by|subtitles?\s+by|"
    r"dịch\s+bởi|biên\s+dịch\s*[:：-]|"
    r"(?:phụ\s+đề|vietsub)(?:\s*$|\s*[:：-]|\s+(?:việt|bởi|thực\s+hiện|tiếng))|"
    r"sync(?:ed|hroni[sz](?:ed|ation))?\s*(?:$|[:：&-]|by\b|bởi\b))",
    re.IGNORECASE,
)
_MOJIBAKE = re.compile(
    r"\ufffd|[ÃÂ][\u0080-\u00bf€‚ƒ„…†‡ˆ‰Š‹ŒŽ‘’“”•–—˜™š›œžŸ]|"
    r"Ä[\u0080-\u00bf‘’]|Æ[\u0080-\u00bf°¡]|á[º»]"
)
_ANNOTATION = re.compile(r"(?:(?:\[[^\[\]]*\]|\([^()]*\))\s*)+")


def _text(text: str) -> str:
    return unicodedata.normalize("NFC", _TAGS.sub("", text)).strip()


def _junk(text: str) -> bool:
    return bool(_JUNK.search(_text(text)))


def strip_junk(cues: list[Cue]) -> list[Cue]:
    """Return a new list without ad/credit/watermark cues; never mutate cues.

    Keep timing, numbering, and speakers of retained cues. A cue containing a
    recognized credit or URL is removed as a whole; ordinary film dialogue and
    sound annotations remain untouched.
    """
    return [cue for cue in cues if not _junk(cue.text)]


def _speech(text: str) -> bool:
    for line in text.splitlines():
        line = line.strip(" \t*♪♫#~−–—-")
        if not _ANNOTATION.fullmatch(line) and any(c.isalnum() for c in line):
            return True
    return False


def _valid_time(cue: Cue) -> bool:
    return (math.isfinite(cue.start) and math.isfinite(cue.end)
            and cue.start >= 0 and cue.end > cue.start)


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _intersection_duration(left, right):
    i = j = 0
    total = 0.0
    while i < len(left) and j < len(right):
        total += max(0.0, min(left[i][1], right[j][1]) - max(left[i][0], right[j][0]))
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return total


def assess_subtitle(
    cues: list[Cue], lang: str = "vi", reference: list[Cue] | None = None,
) -> QualityReport:
    """Assess parsed cues in O(n log n), without editing them.

    Reading speeds above 25 characters/second are outliers. English detection
    requires at least three words, at least two common English words, and no
    Vietnamese diacritics. Coverage compares unions of speech intervals (sound
    annotations/ads excluded), avoiding double counting overlaps. It assumes
    candidate and reference already use the same timeline.
    """
    n = len(cues)
    issues = []
    stats = {"cue_count": n}
    penalties = []

    def penalize(name, amount, issue):
        amount = min(1.0, max(0.0, amount))
        stats[f"penalty_{name}"] = amount
        if amount:
            penalties.append(amount)
            issues.append(issue)

    if not cues:
        stats.update(empty_cues=0, speech_cues=0, penalty_empty_file=1.0)
        return QualityReport(0.0, ["No subtitle cues"], stats)

    texts = [_text(cue.text) for cue in cues]
    valid = [_valid_time(cue) for cue in cues]
    junk_flags = [bool(_JUNK.search(text)) for text in texts]
    speech = [not ad and _speech(text) for text, ad in zip(texts, junk_flags)]
    empty = sum(not text for text in texts)
    junk = sum(junk_flags)
    encoding = sum(bool(_MOJIBAKE.search(text)) for text in texts)
    invalid = sum(not value for value in valid)
    negative = sum(cue.start < 0 or cue.end < 0 for cue in cues)
    bad_indices = sum(not isinstance(cue.index, int) or cue.index < 1 for cue in cues)
    repeated_indices = n - len({cue.index for cue in cues})
    out_of_order = sum(cues[i].start < cues[i - 1].start for i in range(1, n))
    normalized = [" ".join(text.casefold().split()) for text in texts if text]
    duplicates = sum(count - 1 for count in Counter(normalized).values())
    overlap = 0
    furthest_end = -math.inf
    for cue in sorted((cue for cue, good in zip(cues, valid) if good), key=lambda c: c.start):
        if cue.start < furthest_end - 0.02:
            overlap += 1
        furthest_end = max(furthest_end, cue.end)

    speech_texts = [text for text, spoken in zip(texts, speech) if spoken]
    speech_count = len(speech_texts)
    accented = sum(any(c.casefold() in _VI for c in text) for text in speech_texts)
    letters = sum(c.isalpha() for text in speech_texts for c in text)
    accented_letters = sum(c.casefold() in _VI for text in speech_texts for c in text if c.isalpha())
    english = 0
    for text in speech_texts:
        words = _WORDS.findall(text.casefold())
        hits = sum(word in _ENGLISH for word in words)
        if len(words) >= 3 and hits >= 2 and hits / len(words) >= 0.3 and not any(c.casefold() in _VI for c in text):
            english += 1
    speeds = [sum(not c.isspace() for c in text) / (cue.end - cue.start)
              for cue, text, good, spoken in zip(cues, texts, valid, speech) if good and spoken]
    fast = sum(speed > 25 for speed in speeds)
    english_ratio = english / max(1, speech_count)
    diacritics_ratio = accented / max(1, speech_count)
    stats.update(
        empty_cues=empty, duplicate_cues=duplicates, junk_cues=junk,
        encoding_error_cues=encoding, invalid_timing_cues=invalid,
        negative_timing_cues=negative, overlapping_cues=overlap,
        out_of_order_cues=out_of_order, invalid_indices=bad_indices,
        duplicate_indices=repeated_indices, speech_cues=speech_count,
        vietnamese_diacritics_ratio=diacritics_ratio,
        diacritic_letter_ratio=accented_letters / max(1, letters),
        english_cues=english, english_ratio=english_ratio,
        fast_cues=fast, max_chars_per_second=max(speeds, default=0.0),
    )
    penalize("empty", 0.5 * empty / n, f"{empty}/{n} cues are empty")
    penalize("encoding", 0.8 * encoding / n, f"{encoding}/{n} cues contain mojibake or encoding errors")
    penalize("timing", 0.8 * invalid / n, f"{invalid}/{n} cues have invalid or negative timings")
    penalize("indices", 0.1 * min(1, (bad_indices + repeated_indices) / n),
             f"{bad_indices} invalid and {repeated_indices} duplicate cue indices")
    penalize("order", 0.1 * out_of_order / n, f"{out_of_order} cues are out of chronological order")
    penalize("overlap", 0.15 * overlap / n, f"{overlap}/{n} cues overlap earlier cues")
    penalize("duplicates", 0.2 * duplicates / n, f"{duplicates}/{n} cues repeat earlier text")
    penalize("reading_speed", 0.3 * fast / max(1, len(speeds)),
             f"{fast}/{len(speeds)} spoken cues exceed 25 characters/second")
    penalize("junk", 0.3 * junk / n, f"{junk}/{n} cues contain ads, credits, or watermarks")
    if lang.casefold().replace("_", "-").split("-")[0] in {"vi", "vie", "vietnamese"}:
        penalize("english", 0.6 * english_ratio,
                 f"{english_ratio:.0%} of spoken cues look like untranslated English")
        # Short/name-only files offer insufficient evidence to judge language.
        low_diacritics = speech_count >= 3 and letters >= 30 and diacritics_ratio < 0.3
        penalize("language", 0.2 * (1 - diacritics_ratio / 0.3) if low_diacritics else 0,
                 f"Only {diacritics_ratio:.0%} of spoken cues contain Vietnamese diacritics")
    penalize("no_speech", 0.8 if speech_count == 0 else 0, "No speakable dialogue cues")

    intervals = _union([(cue.start, cue.end) for cue, good, spoken in zip(cues, valid, speech) if good and spoken])
    speech_seconds = sum(end - start for start, end in intervals)
    stats["speech_seconds"] = speech_seconds
    if reference is not None:
        ref_intervals = []
        for cue in reference:
            if _valid_time(cue):
                text = _text(cue.text)
                if not _JUNK.search(text) and _speech(text):
                    ref_intervals.append((cue.start, cue.end))
        ref_intervals = _union(ref_intervals)
        ref_seconds = sum(end - start for start, end in ref_intervals)
        stats["reference_cue_count"] = len(reference)
        stats["cue_count_ratio"] = n / len(reference) if reference else None
        stats["reference_speech_seconds"] = ref_seconds
        coverage = _intersection_duration(intervals, ref_intervals) / ref_seconds if ref_seconds else None
        stats["speech_time_coverage"] = coverage
        ratio = stats["cue_count_ratio"]
        count_penalty = (0.15 * (1 - ratio / 0.5) if ratio is not None and ratio < 0.5 else
                         0.1 * min(1, (ratio - 2) / 2) if ratio is not None and ratio > 2 else 0)
        penalize("cue_coverage", count_penalty,
                 f"Cue count is {ratio:.0%} of reference" if ratio is not None else "Reference has no cues")
        penalize("speech_coverage", 0.35 * (1 - coverage / 0.7) if coverage is not None and coverage < 0.7 else 0,
                 f"Speech-time coverage is {coverage:.0%} of reference" if coverage is not None else "Reference has no speech")
    return QualityReport(max(0.0, min(1.0, 1.0 - sum(penalties))), issues, stats)
