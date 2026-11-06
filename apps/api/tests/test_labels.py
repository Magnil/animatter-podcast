"""Audacity label export: format, tags, and the two failure modes.

The format is the whole feature. A label line with anything other than three
tab-separated fields is *silently dropped* by Audacity on import — no error, no
partial track, just fewer labels than the user expected. So these tests pin the
byte shape, not just the parsed values.
"""

from __future__ import annotations

from pathlib import Path

from animatter.core import labels as label_core

TOKENS = [
    (0, "你", 0, 240),
    (1, "好", 240, 480),
    (2, "嗯", 480, 800),
    (3, "community", 800, 1400),
]


def test_line_shape_is_exactly_three_tab_fields() -> None:
    lines = label_core.build_labels(TOKENS)
    body = label_core.render_audacity(lines).decode("utf-8")
    first = body.splitlines()[0]
    assert len(first.split("\t")) == 3
    assert first == "0.000\t0.240\t你"


def test_seconds_have_three_decimals_and_crlf() -> None:
    lines = label_core.build_labels([(0, "你", 7800, 7940)])
    raw = label_core.render_audacity(lines)
    assert raw == "7.800\t7.940\t你\r\n".encode()
    # No BOM: Audacity shows it as part of the first label text.
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_filler_tag_is_prefixed_and_glued() -> None:
    lines = label_core.build_labels(TOKENS, fillers={"嗯"})
    tagged = [line for line in lines if line.text.startswith(label_core.TAG_FILLER)]
    assert [line.text for line in tagged] == ["[FILLER]嗯"]


def test_filler_tag_can_be_switched_off() -> None:
    lines = label_core.build_labels(TOKENS, fillers={"嗯"}, include_filler=False)
    assert all("[" not in line.text for line in lines)


def test_silence_lines_carry_no_text_and_sort_in_place() -> None:
    lines = label_core.build_labels(TOKENS, silences=[(1400, 2000)])
    assert lines[-1].text == label_core.TAG_SILENCE
    assert lines[-1].start_ms == 1400
    # The tag has no trailing text, so the line ends in a tab. Audacity reads
    # that as an empty label, which is what we want: the region is the message.
    body = label_core.render_audacity(lines).decode("utf-8")
    assert "1.400\t2.000\t[SILENCE]\r\n" in body


def test_tabs_and_newlines_in_text_cannot_break_the_line() -> None:
    # A pasted hotword or a model hiccup can put a tab in a token. Unescaped it
    # would split the label into four fields and the whole line would vanish.
    lines = label_core.build_labels([(0, "a\tb\nc", 0, 500)])
    assert lines[0].text == "a b c"
    assert len(label_core.render_audacity(lines).decode().strip().split("\t")) == 3


def test_empty_token_text_is_dropped_not_written_as_blank() -> None:
    lines = label_core.build_labels([(0, "", 0, 500), (1, "你", 500, 900)])
    assert [line.text for line in lines] == ["你"]


def test_skip_deleted_removes_only_listed_tokens() -> None:
    lines = label_core.build_labels(TOKENS, deleted={2}, skip_deleted=True)
    assert "嗯" not in [line.text for line in lines]
    assert len(lines) == 3


def test_deleted_tokens_stay_when_not_skipping() -> None:
    # Cancelling a cut in Audacity is easier than re-exporting because one word
    # was missing, so the option exists.
    lines = label_core.build_labels(TOKENS, deleted={2}, skip_deleted=False)
    assert len(lines) == 4


def test_zero_length_silence_is_ignored() -> None:
    lines = label_core.build_labels([(0, "你", 0, 100)], silences=[(500, 500)])
    assert [line.text for line in lines] == ["你"]


def test_filename_uses_project_name_and_strips_windows_characters() -> None:
    assert label_core.suggest_filename("漫物质") == "漫物质_audacity_labels.txt"
    assert label_core.suggest_filename("a/b:c*d") == "a_b_c_d_audacity_labels.txt"
    assert label_core.suggest_filename("") == "labels_audacity_labels.txt"
    assert label_core.suggest_filename("漫物质", version=3) == "漫物质_v3_audacity_labels.txt"


def test_reference_file_shape_round_trips() -> None:
    """The committed reference file must parse as three fields on every line.

    This is the file the user's old script produced and the shape they expect
    back. It ships with the tests so the format has a live witness, not only the
    manual's prose — and so a future refactor that changes the separator fails
    here rather than in Audacity.
    """
    reference = Path(__file__).parent / "fixtures" / "audacity_full_labels.txt"
    assert reference.exists(), "fixture missing; the format test would silently pass"
    raw = reference.read_bytes()
    assert raw.endswith(bytes([13, 10]))
    assert not raw.startswith(bytes([0xEF, 0xBB, 0xBF]))
    for line in raw.decode("utf-8").splitlines():
        parts = line.split("	")
        assert len(parts) == 3, line
        float(parts[0])
        float(parts[1])
