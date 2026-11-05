"""Hotword sanitising (AGENT.md §2.1, §6.1).

Every rejection rule here maps to a documented provider constraint or a measured
failure mode. The weight cap in particular is not a style preference: `weight=5`
was measured to turn a *correct* transcription into a hotword.
"""

from __future__ import annotations

import pytest

from animatter.core.hotwords import (
    DEFAULT_WEIGHT,
    MAX_NON_ASCII_CHARS,
    HotwordCandidate,
    clamp_weight,
    merge_vocabularies,
    sanitize,
    text_length_ok,
    to_instant_vocabulary,
    truncate_to_limit,
)

# --- length rules -------------------------------------------------------


def test_chinese_word_within_limit() -> None:
    assert text_length_ok("厄洛替尼盐酸盐") is True  # 7 chars


def test_chinese_word_at_limit() -> None:
    assert text_length_ok("字" * MAX_NON_ASCII_CHARS) is True


def test_chinese_word_over_limit() -> None:
    assert text_length_ok("字" * (MAX_NON_ASCII_CHARS + 1)) is False


def test_mixed_ascii_counts_toward_char_limit() -> None:
    assert text_length_ok("EGFR抑制剂") is True  # 7 chars total
    # Non-ASCII input is measured in characters, ASCII included.
    assert text_length_ok("A" + "字" * MAX_NON_ASCII_CHARS) is False


def test_pure_ascii_uses_segment_rule_not_char_count() -> None:
    # A single long ASCII token is legal: the limit counts space-separated
    # segments, so 16 characters without spaces is one segment.
    assert text_length_ok("A" * 16) is True


def test_english_segment_limit() -> None:
    assert text_length_ok("Human immunodeficiency virus type 1") is True  # 6 segments
    assert text_length_ok("one two three four five six seven") is True  # exactly 7
    assert text_length_ok("one two three four five six seven eight") is False


def test_empty_and_whitespace_rejected() -> None:
    assert text_length_ok("") is False
    assert text_length_ok("   ") is False


# --- rejection rules ----------------------------------------------------


def test_stopwords_rejected() -> None:
    report = sanitize([HotwordCandidate("我们"), HotwordCandidate("而且")])
    assert report.accepted == []
    assert {text for text, _ in report.rejected} == {"我们", "而且"}


def test_punctuation_rejected() -> None:
    report = sanitize([HotwordCandidate("你好，世界")])
    assert report.accepted == []
    assert "句读" in report.rejected[0][1]


def test_pure_number_rejected() -> None:
    report = sanitize([HotwordCandidate("2024")])
    assert report.accepted == []


def test_single_ascii_letter_rejected() -> None:
    report = sanitize([HotwordCandidate("A")])
    assert report.accepted == []


def test_single_chinese_char_accepted() -> None:
    # A single CJK char can be a meaningful term (e.g. a surname).
    report = sanitize([HotwordCandidate("祺")])
    assert len(report.accepted) == 1


def test_term_without_alnum_rejected() -> None:
    report = sanitize([HotwordCandidate("···")])
    assert report.accepted == []


# --- weight handling ----------------------------------------------------


def test_default_weight_is_three() -> None:
    assert DEFAULT_WEIGHT == 3
    assert HotwordCandidate("某产品").weight == 3


@pytest.mark.parametrize(("raw", "expected"), [(0, 1), (1, 1), (3, 3), (5, 5), (99, 5), (-4, 1)])
def test_weights_clamped(raw: int, expected: int) -> None:
    assert clamp_weight(raw) == expected


def test_super_weight_preserved() -> None:
    # 50 is the documented "super hotword" weight; clamping it to 5 would
    # silently disable a feature the caller asked for.
    assert clamp_weight(50) == 50


def test_generic_terms_downweighted() -> None:
    report = sanitize([HotwordCandidate("人工智能", weight=5)])
    assert report.accepted[0].weight == 2
    assert report.downweighted == [("人工智能", 5, 2)]


def test_generic_term_already_low_not_flagged() -> None:
    report = sanitize([HotwordCandidate("人工智能", weight=2)])
    assert report.downweighted == []


# --- deduplication ------------------------------------------------------


def test_duplicate_keeps_highest_weight() -> None:
    report = sanitize(
        [
            HotwordCandidate("张三", weight=2, evidence="第一次出现"),
            HotwordCandidate("张三", weight=4, evidence="第二次出现"),
        ]
    )
    assert len(report.accepted) == 1
    assert report.accepted[0].weight == 4


def test_duplicate_keeps_evidence_from_first() -> None:
    report = sanitize(
        [
            HotwordCandidate("张三", weight=4, evidence="大纲原句"),
            HotwordCandidate("张三", weight=2, evidence=""),
        ]
    )
    assert report.accepted[0].evidence == "大纲原句"


def test_duplicate_whitespace_normalised() -> None:
    report = sanitize([HotwordCandidate(" 张三 "), HotwordCandidate("张三")])
    assert len(report.accepted) == 1
    assert report.accepted[0].text == "张三"


# --- ordering and truncation -------------------------------------------


def test_truncation_keeps_highest_weights() -> None:
    candidates = [HotwordCandidate(f"词{i}", weight=1) for i in range(10)]
    candidates.append(HotwordCandidate("重要词条", weight=5))
    report = sanitize(candidates, limit=3)
    assert len(report.accepted) == 3
    assert report.accepted[0].text == "重要词条"
    assert any("超出上限" in reason for _, reason in report.rejected)


def test_truncation_is_deterministic() -> None:
    candidates = [HotwordCandidate(f"词{i}", weight=3) for i in range(5)]
    first = [c.text for c in sanitize(candidates, limit=3).accepted]
    second = [c.text for c in sanitize(candidates, limit=3).accepted]
    assert first == second


def test_accepted_sorted_by_weight_desc() -> None:
    report = sanitize([HotwordCandidate("低权重", weight=1), HotwordCandidate("高权重", weight=5)])
    assert [c.text for c in report.accepted] == ["高权重", "低权重"]


def test_empty_input_yields_empty_report() -> None:
    report = sanitize([])
    assert report.accepted == []
    assert report.rejected == []


# --- vocabulary assembly ------------------------------------------------


def test_to_instant_vocabulary() -> None:
    vocabulary = to_instant_vocabulary(
        [HotwordCandidate("张三", weight=4), HotwordCandidate("李四", weight=2)]
    )
    assert vocabulary == {"张三": 4, "李四": 2}


def test_merge_keeps_max_weight() -> None:
    merged = merge_vocabularies({"张三": 2}, {"张三": 5, "李四": 3})
    assert merged == {"张三": 5, "李四": 3}


def test_merge_three_sources() -> None:
    merged = merge_vocabularies({"a": 1}, {"b": 2}, {"a": 4, "c": 3})
    assert merged == {"a": 4, "b": 2, "c": 3}


def test_truncate_below_limit_is_noop() -> None:
    kept, dropped = truncate_to_limit({"a": 1}, limit=10)
    assert kept == {"a": 1}
    assert dropped == 0


def test_truncate_reports_count() -> None:
    vocab = {f"词{i}": 1 for i in range(10)}
    kept, dropped = truncate_to_limit(vocab, limit=4)
    assert len(kept) == 4
    assert dropped == 6


def test_truncate_prefers_higher_weight() -> None:
    vocab = {"低频词": 1, "高频词": 5, "中频词": 3}
    kept, _ = truncate_to_limit(vocab, limit=2)
    assert "高频词" in kept
    assert "中频词" in kept
    assert "低频词" not in kept
