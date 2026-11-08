"""Warnings that catch the two failures the prompt cannot prevent.

Both are things a model produces confidently and a human cannot spot without
cross-checking against the transcript:

* a year or a play count that reads naturally and is simply not in the audio;
* a chapter list that stops at 60 % of the episode, which looks like a choice.
"""

from __future__ import annotations

import pytest

from animatter.core.script import unverified_numbers


def test_a_number_absent_from_the_transcript_is_reported() -> None:
    # The real failure: 「今年入围费那奇」 became 「2023 年入围费那奇」.
    assert unverified_numbers("她 2023 年入围费那奇", "今年入围了费那奇动画周") == ["2023"]


def test_a_number_present_in_the_transcript_is_not_reported() -> None:
    transcript = "今年是在11月13日到16日在北京举办"
    assert unverified_numbers("11 月 13 日到 16 日", transcript) == []


def test_each_invented_number_is_reported_once() -> None:
    # 2023 appears twice but is one thing to go and check.
    assert unverified_numbers("2023 年，2023 年", "没有数字") == ["2023"]


def test_decimals_are_matched_as_a_whole() -> None:
    # "1.5" must not be reported as "1" plus "5".
    assert unverified_numbers("1.5 倍", "没有") == ["1.5"]
    assert unverified_numbers("1.5 倍", "1.5 倍") == []


def test_text_without_digits_produces_nothing() -> None:
    assert unverified_numbers("没有任何数字", "也没有") == []


@pytest.mark.parametrize("text", ["2023", "100万", "11月13日"])
def test_reported_tokens_are_the_literals_that_were_searched_for(text: str) -> None:
    # The warning quotes these back to the user, so they must be the exact
    # strings to look for — not normalised or reformatted.
    for token in unverified_numbers(text, ""):
        assert token in text
