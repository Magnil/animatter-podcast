"""CJK line breaking for burned-in subtitles (§19.6).

libass cannot do this and will not say so. ``WrapStyle: 0`` finds break opportunities
at spaces; Chinese has none, so a long cue is drawn as a single line that runs off both
edges of the frame. At the default 52 px that overflow is invisible for most sentences,
which is why it survived until a still was rendered at 88 px and the first and last
characters were simply gone.

Wrapping here rather than trusting libass also makes the font-size control honest: the
number of characters per line is a division by the em size, so raising the size re-wraps
instead of cropping.
"""

from __future__ import annotations

from animatter.media.subtitle_ass import (
    AssCue,
    SubtitleStyle,
    build_ass,
    chars_per_line,
    wrap_cjk,
)

LONG = "小广她不是一个旁观者的状态，她就是变化本身，外部世界不断在MV，外部世界是混乱的。"


def test_chars_per_line_is_the_usable_width_over_the_em_size() -> None:
    style = SubtitleStyle()
    # 1920 - 120 - 120 = 1680; 1680 // 52 = 32.
    assert chars_per_line(style, 1920) == 32


def test_chars_per_line_shrinks_as_the_font_grows() -> None:
    small = chars_per_line(SubtitleStyle(font_size=52), 1920)
    large = chars_per_line(SubtitleStyle(font_size=88), 1920)
    assert large < small
    assert large == 19


def test_chars_per_line_respects_wider_margins() -> None:
    narrow = SubtitleStyle(margin_l=200, margin_r=200)
    assert chars_per_line(narrow, 1920) < chars_per_line(SubtitleStyle(), 1920)


def test_chars_per_line_never_returns_zero() -> None:
    # A font bigger than the frame is nonsense, but a division by it must not be.
    assert chars_per_line(SubtitleStyle(font_size=4000), 1920) >= 1


def test_short_text_is_one_line() -> None:
    assert wrap_cjk("嗯嗯", style=SubtitleStyle(), play_w=1920) == ["嗯嗯"]


def test_empty_text_produces_no_lines() -> None:
    assert wrap_cjk("", style=SubtitleStyle(), play_w=1920) == []


def test_long_text_wraps_within_the_limit() -> None:
    lines = wrap_cjk(LONG, style=SubtitleStyle(), play_w=1920)
    assert len(lines) > 1
    assert max(len(line) for line in lines) <= 32


def test_wrapping_prefers_punctuation() -> None:
    """A break after 「，」 reads far better than one mid-phrase."""
    lines = wrap_cjk(LONG, style=SubtitleStyle(font_size=88), play_w=1920)
    # Every line but the last should end on punctuation, since this text has plenty.
    assert all(line[-1] in "。，、；：！？" for line in lines[:-1])


def test_wrapping_preserves_every_character() -> None:
    """Dropping a character to make a line fit is the bug this guards against."""
    lines = wrap_cjk(LONG, style=SubtitleStyle(font_size=88), play_w=1920)
    assert "".join(lines) == LONG


def test_wrapping_a_run_with_no_punctuation_still_breaks() -> None:
    """ASR output is not always punctuated; a 60-character run must still wrap."""
    run = "一" * 60
    lines = wrap_cjk(run, style=SubtitleStyle(), play_w=1920)
    assert len(lines) == 2
    assert all(len(line) <= 32 for line in lines)


def test_build_ass_writes_newlines_not_one_long_line() -> None:
    ass = build_ass([AssCue(0, 1000, LONG)], style=SubtitleStyle(font_size=88))
    dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
    assert "\\N" in dialogue
    # The raw text must not appear unbounded — that is the overflow.
    assert LONG not in dialogue


def test_build_ass_still_escapes_braces_per_line() -> None:
    """Wrapping must not bypass the escaping: braces swallow the rest of a cue."""
    ass = build_ass([AssCue(0, 1000, "看这个{笑}话")], style=SubtitleStyle())
    dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
    assert "\\{笑\\}" in dialogue


def test_build_ass_escapes_a_newline_inside_a_line() -> None:
    ass = build_ass([AssCue(0, 1000, "第一行\n第二行")], style=SubtitleStyle())
    dialogue = next(line for line in ass.splitlines() if line.startswith("Dialogue:"))
    assert "第一行\\N第二行" in dialogue


def test_a_larger_font_size_produces_more_lines() -> None:
    """The control has to reach the wrapping, not just the style header."""
    small = build_ass([AssCue(0, 1000, LONG)], style=SubtitleStyle(font_size=40))
    large = build_ass([AssCue(0, 1000, LONG)], style=SubtitleStyle(font_size=88))
    assert large.count("\\N") > small.count("\\N")
