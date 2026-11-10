"""Burned-in subtitles: ASS serialisation and the font guard (§19.5, §19.11.2).

Two libass behaviours are silent and both ruin an export:

* missing ``PlayResX``/``PlayResY`` → the script is treated as 384×288, so at 1080p
  every cue is ~5.6× too large and the film is unusable;
* a missing font family → libass substitutes without a single log line, so the only
  symptom is a finished film that "looks wrong".

The tests below pin the parts that make those two impossible to hit by accident:
the header always carries the resolution, and the font check runs **before** any
encode rather than after.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from animatter.errors import AppError
from animatter.media.subtitle_ass import (
    AssCue,
    SubtitleStyle,
    ass_timestamp,
    build_ass,
    ensure_font,
    escape_text,
    find_font,
    first_available_font,
    write_ass,
)

# --- timestamps ---------------------------------------------------------


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        (0, "0:00:00.00"),
        (10, "0:00:00.01"),
        (1000, "0:00:01.00"),
        (61_500, "0:01:01.50"),
        (3_600_000, "1:00:00.00"),
        (3_695_928, "1:01:35.92"),
    ],
)
def test_ass_timestamp_uses_centiseconds(ms: int, expected: str) -> None:
    assert ass_timestamp(ms) == expected


def test_ass_timestamp_never_goes_negative() -> None:
    # A negative time would serialise as ``-1:59:59.99`` and libass drops the line.
    assert ass_timestamp(-500) == "0:00:00.00"


# --- escaping -----------------------------------------------------------


def test_braces_are_escaped() -> None:
    """``{`` starts an override block: unescaped, it swallows the rest of the line."""
    assert escape_text("他说{笑}") == "他说\\{笑\\}"


def test_backslashes_are_escaped_before_braces() -> None:
    # Order matters: escaping braces first would then double the newly added ``\``.
    assert escape_text("a\\b") == "a\\\\b"


def test_newlines_become_the_ass_line_break() -> None:
    assert escape_text("第一行\n第二行") == "第一行\\N第二行"
    assert escape_text("第一行\r\n第二行") == "第一行\\N第二行"


# --- structure ----------------------------------------------------------


def test_header_always_declares_the_play_resolution() -> None:
    """Without it libass assumes 384×288 and scales the style by ~5.6× at 1080p."""
    text = build_ass([], style=SubtitleStyle())
    assert "PlayResX: 1920" in text
    assert "PlayResY: 1080" in text


def test_header_has_the_three_required_sections() -> None:
    text = build_ass([], style=SubtitleStyle())
    assert "[Script Info]" in text
    assert "[V4+ Styles]" in text
    assert "[Events]" in text


def test_style_line_carries_the_font_and_size() -> None:
    text = build_ass([], style=SubtitleStyle(font_name="SimHei", font_size=44))
    style_line = next(line for line in text.splitlines() if line.startswith("Style: Default,"))
    assert "SimHei" in style_line
    assert ",44," in style_line


def test_bold_is_written_in_ass_convention() -> None:
    """ASS uses ``-1`` for bold, not ``1`` — the opposite of every CSS habit."""
    bold = next(
        line
        for line in build_ass([], style=SubtitleStyle(bold=True)).splitlines()
        if line.startswith("Style:")
    )
    plain = next(
        line
        for line in build_ass([], style=SubtitleStyle(bold=False)).splitlines()
        if line.startswith("Style:")
    )
    assert ",-1," in bold
    assert ",0," in plain


def test_cues_come_out_as_dialogue_lines_in_order() -> None:
    cues = [AssCue(0, 1000, "第一句"), AssCue(1200, 2500, "第二句")]
    text = build_ass(cues, style=SubtitleStyle())
    dialogues = [line for line in text.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogues) == 2
    assert "0:00:00.00,0:00:01.00" in dialogues[0]
    assert "第一句" in dialogues[0]
    assert "第二句" in dialogues[1]


def test_zero_length_cues_are_dropped() -> None:
    # libass renders an empty span as a one-frame flash, which reads as a glitch.
    text = build_ass([AssCue(1000, 1000, "空的")], style=SubtitleStyle())
    assert not any(line.startswith("Dialogue:") for line in text.splitlines())


def test_written_file_is_utf8_with_bom(tmp_path: Path) -> None:
    """A BOM is required: without it libass on Windows guesses the codepage."""
    path = write_ass(tmp_path / "x.ass", [AssCue(0, 500, "中文")], style=SubtitleStyle())
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert "中文" in raw.decode("utf-8-sig")


# --- the font guard -----------------------------------------------------


def test_ensure_font_accepts_an_installed_family() -> None:
    family = first_available_font()
    if family is None:
        pytest.skip("这台机器上没有可用的中文字体")
    ensure_font(SubtitleStyle(font_name=family))  # must not raise


def test_ensure_font_rejects_a_family_that_is_not_installed() -> None:
    """libass substitutes silently, so this has to fail *before* the encode."""
    if first_available_font() is None:
        pytest.skip("这台机器上没有可用的中文字体")
    with pytest.raises(AppError) as excinfo:
        ensure_font(SubtitleStyle(font_name="Absolutely Not A Font 12345"))
    assert excinfo.value.code == "subtitle_font_missing"
    # The hint must name a family that *does* exist, or it is not actionable.
    assert first_available_font() in excinfo.value.hint


def test_find_font_returns_a_real_file_for_a_known_family() -> None:
    family = first_available_font()
    if family is None:
        pytest.skip("这台机器上没有可用的中文字体")
    path = find_font(family)
    assert path is not None
    assert path.is_file()
    assert path.suffix.lower() in {".ttf", ".ttc", ".otf"}


def test_find_font_is_case_and_space_insensitive() -> None:
    family = first_available_font()
    if family is None:
        pytest.skip("这台机器上没有可用的中文字体")
    assert find_font(family.upper()) is not None
    assert find_font(family.replace(" ", "")) is not None


def test_find_font_maps_known_windows_family_names() -> None:
    """「Microsoft YaHei」 is ``msyh.ttc``; substring matching cannot derive that.

    The first version of this guard only did a substring match, so it reported a
    perfectly installed font as missing and told the user to install it — a worse
    failure than staying quiet, because the advice is wrong.
    """
    if find_font("SimHei") is None:
        pytest.skip("这台机器上没有中文 Windows 字体")
    # Either the table resolves it or the machine genuinely lacks it; what must not
    # happen is "installed but reported missing".
    yahei = find_font("Microsoft YaHei")
    if yahei is None:
        pytest.skip("这台机器上没有微软雅黑")
    assert yahei.is_file()
    assert "msyh" in yahei.stem.lower()


def test_ensure_font_accepts_microsoft_yahei_when_installed() -> None:
    if find_font("Microsoft YaHei") is None:
        pytest.skip("这台机器上没有微软雅黑")
    ensure_font(SubtitleStyle())  # the default style is Microsoft YaHei


def test_find_font_returns_none_for_nonsense() -> None:
    assert find_font("zzzz-not-a-font-zzzz") is None


# --- style invariants ---------------------------------------------------


def test_style_headroom_keeps_text_inside_the_frame() -> None:
    """A huge font with default margins would overflow; the style has to be coherent."""
    style = SubtitleStyle()
    assert 0 < style.margin_v < 150
    assert 0 < style.font_size < 120
    assert style.margin_l + style.margin_r < 1920
