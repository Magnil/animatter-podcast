"""Tests for the colour markup and the round-98 naming decisions."""

from __future__ import annotations

from animatter.core import textspan

BLUE = textspan.BLUE
RED = textspan.RED


# --- the parser ---------------------------------------------------------


def test_plain_text_is_one_segment() -> None:
    assert textspan.parse("EP.23", base=BLUE) == [textspan.Segment(text="EP.23", colour=BLUE)]


def test_the_sample_episode_number_round_trips() -> None:
    """``EP.**23**`` is exactly what the PSD draws: ``EP.`` blue, ``23`` red."""
    assert textspan.plain("EP.**23**") == "EP.23"
    assert textspan.parse("EP.**23**", base=BLUE) == [
        textspan.Segment(text="EP.", colour=BLUE),
        textspan.Segment(text="23", colour=RED),
    ]


def test_several_accent_stretches_are_all_marked() -> None:
    marked = "《罗小黑战记2》**独家专访**六年磨一剑背后的**苦与乐**"
    segments = textspan.parse(marked, base=BLUE)
    assert [segment.text for segment in segments] == [
        "《罗小黑战记2》",
        "独家专访",
        "六年磨一剑背后的",
        "苦与乐",
    ]
    assert [segment.colour for segment in segments] == [BLUE, RED, BLUE, RED]


def test_newlines_survive_inside_a_segment() -> None:
    """The renderer wraps per paragraph; losing the break here would join two of them."""
    segments = textspan.parse("第一行\n**第二行**", base=BLUE)
    assert "".join(segment.text for segment in segments) == "第一行\n第二行"


def test_an_unpaired_marker_is_literal_text() -> None:
    """Not a silent colour switch: a forgotten asterisk must not recolour the rest of a title."""
    assert textspan.is_marked("EP.**23") is False
    assert textspan.plain("EP.**23") == "EP.**23"
    assert textspan.parse("EP.**23", base=BLUE) == [
        textspan.Segment(text="EP.**23", colour=BLUE)
    ]


def test_a_doubled_marker_is_an_escaped_pair() -> None:
    assert textspan.is_marked("a****b") is False
    assert textspan.plain("a****b") == "ab"


def test_the_accent_is_the_other_brand_colour() -> None:
    """The design has exactly two colours; what varies is which one a slot is made of."""
    assert textspan.accent_for(BLUE) == RED
    assert textspan.accent_for(RED) == BLUE
    # The footer is red with no markup, and marking it must give the *blue* back, not red.
    assert textspan.parse("**看这里**", base=RED) == [
        textspan.Segment(text="看这里", colour=BLUE)
    ]


def test_adjacent_runs_of_one_colour_are_merged() -> None:
    """``a****b`` is two markers with nothing between them — one blue run, not three."""
    assert textspan.parse("a****b", base=BLUE) == [textspan.Segment(text="ab", colour=BLUE)]


def test_empty_text_has_no_segments() -> None:
    assert textspan.parse("", base=BLUE) == []
    assert textspan.plain("") == ""


# --- labels -------------------------------------------------------------


def test_footer_and_notes_get_role_names_not_their_own_text() -> None:
    """The form said 「欢迎大家关注、加群、一键三连！ 详情请见视频简介」 — that is the *value*.

    Photoshop names an un-renamed text layer after what was typed into it, so `label =
    layer.name` labelled the field with its own contents and went stale on the first edit.
    """
    from animatter.core import textslot

    canvas_w, canvas_h = 2560, 1440
    slots = textslot.classify(
        [
            textslot.Candidate(
                name="欢迎大家关注、加群、一键三连！ 详情请见视频简介",
                text="欢迎大家关注、加群、一键三连！",
                box=textslot.Box(x=1900, y=1374, w=340, h=48),
                font_size=25.0,
                colour=RED,
            )
        ],
        canvas_w=canvas_w,
        canvas_h=canvas_h,
    )
    assert slots[0].label == "页脚"
    assert slots[0].role == textslot.ROLE_NOTE


def test_avatar_labels_follow_the_side_not_the_psd_name() -> None:
    from animatter.core import textslot

    slots = textslot.classify(
        [
            textslot.Candidate(
                name="头像1 拷贝",
                text="小榕",
                box=textslot.Box(x=200, y=200, w=30, h=16),
                font_size=21.0,
                colour="#ffdf0b",
                avatar="头像1 拷贝",
            )
        ],
        canvas_w=2560,
        canvas_h=1440,
        avatar_labels={"头像1 拷贝": "嘉宾1"},
    )
    assert slots[0].label == "嘉宾1"
    # The key stays on the PSD's own name so an override survives a recompile.
    assert slots[0].key == "avatar:头像1 拷贝"
