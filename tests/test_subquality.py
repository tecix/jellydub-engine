"""Crafted subtitle quality fixtures: no files, models, or network required."""
from dataclasses import replace
import math
import unicodedata

import pytest

from jellydub.models import Cue
from jellydub.subquality import QualityReport, assess_subtitle, strip_junk


def good_cues():
    return [Cue(1, 0, 4, "Chúng ta đang khám phá những vì sao.", "TYSON"),
            Cue(2, 5, 9, "Đây là một thế giới rất đẹp."),
            Cue(3, 10, 14, "Hành tinh này chứa đựng nhiều bí ẩn.")]


def test_good_vietnamese_and_transparent_score():
    cues = good_cues()
    original = [replace(cue) for cue in cues]
    report = assess_subtitle(cues)
    assert isinstance(report, QualityReport)
    assert report.score == 1
    assert report.issues == []
    assert report.stats["vietnamese_diacritics_ratio"] == 1
    assert report.stats["english_ratio"] == 0
    assert report.stats["speech_seconds"] == 12
    assert cues == original
    penalties = [value for key, value in report.stats.items() if key.startswith("penalty_")]
    assert report.score == max(0, 1 - sum(penalties))


@pytest.mark.parametrize("credit", [
    "Downloaded from OpenSubtitles.org", "subscene.com", "www.example.com",
    "https://example.com", "Phụ đề: Nhóm A", "Phụ đề Việt bởi Nhóm A",
    "Dịch bởi Nguyễn Văn A", "Sync: someone", "Synced by somebody",
    "Translated by Alice", "Subtitles by Bob", "<i>Vietsub: nhóm A</i>",
])
def test_strip_junk_preserves_retained_cues_and_does_not_mutate(credit):
    cues = good_cues() + [Cue(9, 20, 24, credit)]
    original = [replace(cue) for cue in cues]
    kept = strip_junk(cues)
    assert kept == cues[:3]
    assert kept is not cues and kept[0] is cues[0]
    assert cues == original
    assert strip_junk(kept) == kept
    report = assess_subtitle(cues)
    assert report.stats["junk_cues"] == 1
    assert any("watermarks" in issue for issue in report.issues)


def test_strip_junk_keeps_dialogue_and_annotations():
    texts = ["Sync the clocks now.", "Phụ đề này có dễ đọc không?", "(music)", "♪", "[Nhạc]"]
    cues = [Cue(i + 1, i * 4, i * 4 + 3, text) for i, text in enumerate(texts)]
    assert strip_junk(cues) == cues


@pytest.mark.parametrize("bad", ["ChÃºng ta", "Ä‘áº¹p", "Æ°á»›c", "broken \ufffd text", "cafÃ©"])
def test_encoding_errors(bad):
    cues = good_cues()
    cues[0] = replace(cues[0], text=bad)
    report = assess_subtitle(cues)
    assert report.stats["encoding_error_cues"] == 1
    assert report.score < assess_subtitle(good_cues()).score
    assert any("encoding" in issue for issue in report.issues)


def test_untranslated_english_and_language_evidence():
    english = ["We are looking at the stars.", "This is the world we know.", "They were here in the beginning."]
    cues = [replace(cue, text=text) for cue, text in zip(good_cues(), english)]
    vi = assess_subtitle(cues)
    assert vi.stats["english_ratio"] == 1
    assert vi.stats["vietnamese_diacritics_ratio"] == 0
    assert vi.score <= 0.21
    assert any("untranslated English" in issue for issue in vi.issues)
    assert assess_subtitle(cues, lang="en").score == 1
    assert assess_subtitle(cues, lang="vie").score == vi.score


def test_names_short_lines_and_unicode_normalization():
    names = [replace(cue, text=text) for cue, text in zip(good_cues(), ["Carl Sagan", "NASA", "42"])]
    assert assess_subtitle(names).stats["english_cues"] == 0
    assert assess_subtitle(names).score == 1
    composed = good_cues()
    decomposed = [replace(cue, text=unicodedata.normalize("NFD", cue.text)) for cue in composed]
    assert assess_subtitle(decomposed) == assess_subtitle(composed)


def test_sanity_duplicates_overlap_and_reading_speed():
    cues = [Cue(1, -1, 2, "Chúng ta"), Cue(1, 0, 0, ""),
            Cue(0, 4, 5, "Chúng ta đang khám phá vũ trụ bao la vô tận."),
            Cue(4, 3, 6, "Chúng ta đang khám phá vũ trụ bao la vô tận."),
            Cue(5, math.nan, math.inf, "Thế giới")]
    report = assess_subtitle(cues)
    assert report.stats["invalid_timing_cues"] == 3
    assert report.stats["negative_timing_cues"] == 1
    assert report.stats["empty_cues"] == 1
    assert report.stats["duplicate_cues"] == 1
    assert report.stats["duplicate_indices"] == 1
    assert report.stats["invalid_indices"] == 1
    assert report.stats["out_of_order_cues"] == 1
    assert report.stats["overlapping_cues"] == 1
    assert report.stats["fast_cues"] == 1
    assert 0 <= report.score < 0.5
    assert any("characters/second" in issue for issue in report.issues)
    assert math.isfinite(report.stats["max_chars_per_second"])


def test_coverage_uses_union_and_excludes_sound_annotations():
    reference = [Cue(1, 0, 4, "This is the world."), Cue(2, 2, 6, "We are here."),
                 Cue(3, 7, 9, "(wind)"), Cue(4, 10, 12, "♪")]
    candidate = [Cue(1, 0, 3, "Chúng ta đang ở đây.")]
    report = assess_subtitle(candidate, reference=reference)
    assert report.stats["reference_speech_seconds"] == 6
    assert report.stats["cue_count_ratio"] == 0.25
    assert report.stats["speech_time_coverage"] == 0.5
    assert any("coverage" in issue for issue in report.issues)
    matching = [Cue(1, 0, 6, "Chúng ta đang ở đây.")]
    assert assess_subtitle(matching, reference=reference).stats["speech_time_coverage"] == 1
    shifted = [replace(candidate[0], start=30, end=34)]
    assert assess_subtitle(shifted, reference=reference).score < report.score


def test_good_subtitle_beats_broken_and_cleanup_improves_score():
    good = good_cues()
    broken = [replace(cue, text="This is the the world \ufffd", end=cue.start + 0.1) for cue in good]
    assert assess_subtitle(good).score > assess_subtitle(broken).score
    with_ads = good + [Cue(4, 16, 20, "Translated by Alice")]
    assert assess_subtitle(strip_junk(with_ads)).score > assess_subtitle(with_ads).score


def test_empty_and_silent_subtitles_and_empty_reference():
    assert assess_subtitle([]).score == 0
    assert assess_subtitle([]).issues
    silent = [Cue(1, 0, 2, "♪"), Cue(2, 3, 5, "[Nhạc]")]
    assert assess_subtitle(silent).score < 0.3
    report = assess_subtitle(good_cues(), reference=[])
    assert report.score == 1
    assert report.stats["cue_count_ratio"] is None
    assert report.stats["speech_time_coverage"] is None
