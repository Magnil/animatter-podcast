"""Page grouping: which sentences become one slide (§19.6).

**Why the tests pass their own thresholds.** Each rule (char cap / length cap / pause) has to
be provable on its own, and proving one while the other two are live means writing 60-second
fixtures to get past a length cap that is not the subject. So the isolated tests pass the
threshold they are testing and neutralise the others; the last group then checks the
**shipped defaults** against a realistic episode, because that is the number the product is
judged on (「生成 20 页左右」 vs the 238 a naive rule produced).
"""

from __future__ import annotations

from itertools import pairwise

from animatter.core.deck import (
    MAX_PAGE_CHARS,
    MIN_PAGE_MS,
    PAGE_GAP_MS,
    SEAM_AFTER_MS,
    TARGET_PAGE_MS,
    PagePlan,
    page_spans,
    plan_pages,
)
from animatter.core.script import ScriptSentence

#: Neutral values for the rules a given test is not exercising. ``min_page_ms=0`` disables the
#: merge, ``max_page_chars`` large and ``target_page_ms`` large disable the two caps.
NO_MERGE = 0
NO_CAP = 10_000_000


def sentence(ord_: int, start: int, end: int, text: str, speaker: int | None = 0) -> ScriptSentence:
    return ScriptSentence(ord=ord_, start_ms=start, end_ms=end, speaker=speaker, text=text)


def stretch(
    count: int,
    *,
    first_ord: int = 0,
    start: int = 0,
    per_sentence: int = 10_000,
    gap: int = 0,
    text: str = "这是一句十秒钟的话，用来占位。",
) -> list[ScriptSentence]:
    """``count`` back-to-back sentences, ``per_sentence`` ms each with ``gap`` between them."""
    out: list[ScriptSentence] = []
    cursor = start
    for index in range(count):
        out.append(
            sentence(first_ord + index, cursor, cursor + per_sentence, text)
        )
        cursor += per_sentence + gap
    return out


# --- grouping -----------------------------------------------------------


def test_no_sentences_means_no_pages() -> None:
    """An unprocessed project is a real state, not an error: the UI says so."""
    assert plan_pages([]) == []


def test_only_blank_text_means_no_pages() -> None:
    assert plan_pages([sentence(0, 0, 1000, "   ")]) == []


def test_one_short_stretch_is_one_page() -> None:
    pages = plan_pages(
        [sentence(0, 0, 2000, "第一句。"), sentence(1, 2000, 4000, "第二句。")]
    )
    assert len(pages) == 1
    assert pages[0].body == "第一句。第二句。"


def test_a_pause_right_at_the_gap_threshold_breaks() -> None:
    """Half-open ``>=``: a pause of exactly ``gap_ms`` is a pause, not a hair short of one."""
    pages = plan_pages(
        stretch(6, gap=PAGE_GAP_MS),
        gap_ms=PAGE_GAP_MS,
        seam_after_ms=SEAM_AFTER_MS,
        min_page_ms=NO_MERGE,
    )
    assert len(pages) > 1


def test_a_pause_one_millisecond_short_does_not_break() -> None:
    pages = plan_pages(
        stretch(6, gap=PAGE_GAP_MS - 1),
        gap_ms=PAGE_GAP_MS,
        seam_after_ms=SEAM_AFTER_MS,
        min_page_ms=NO_MERGE,
    )
    assert len(pages) == 1


def test_a_pause_does_not_break_a_young_page() -> None:
    """This is the whole fix for "238 pages": a pause at 20 s is not a topic change.

    A page that breaks at its first real pause is a page every 15–20 seconds, which is a
    slideshow. ``seam_after_ms`` refuses the break until the page has earned it.
    """
    pages = plan_pages(
        stretch(9, gap=PAGE_GAP_MS, per_sentence=5000),
        gap_ms=PAGE_GAP_MS,
        seam_after_ms=SEAM_AFTER_MS,
        target_page_ms=NO_CAP,
        min_page_ms=NO_MERGE,
    )
    assert len(pages) == 1, "45 s of material must not become four pages just because it pauses"


def test_the_length_cap_breaks_a_page_that_never_pauses() -> None:
    """A monologue with no gaps still has to break somewhere."""
    pages = plan_pages(
        stretch(20, per_sentence=10_000),
        target_page_ms=60_000,
        min_page_ms=NO_MERGE,
        max_page_chars=NO_CAP,
    )
    assert len(pages) > 1


def test_a_pause_at_a_seam_is_preferred_over_waiting_for_the_cap() -> None:
    """The pause rule exists so the break lands on a seam instead of mid-sentence."""
    pages = plan_pages(
        stretch(8, per_sentence=10_000, gap=PAGE_GAP_MS),
        gap_ms=PAGE_GAP_MS,
        seam_after_ms=30_000,
        target_page_ms=120_000,
        min_page_ms=NO_MERGE,
        max_page_chars=NO_CAP,
    )
    spans = page_spans(pages, total_ms=200_000)
    for page, _start, _end in spans[:-1]:
        assert page.source_ord % 3 == 0, "a break at 30 s means every third sentence opens a page"


def test_the_char_cap_starts_a_new_page() -> None:
    text = "字" * 50
    pages = plan_pages(
        [sentence(0, 0, 5000, text), sentence(1, 5000, 10_000, text)],
        max_page_chars=80,
        target_page_ms=NO_CAP,
        min_page_ms=NO_MERGE,
    )
    assert len(pages) == 2


def test_a_page_body_never_exceeds_the_cap() -> None:
    """One long turn can overflow on its own; the body is truncated, not the page count."""
    pages = plan_pages([sentence(0, 0, 30_000, "字" * 500)], max_page_chars=90)
    assert len(pages) == 1
    assert len(pages[0].body) == 90


def test_a_page_too_short_to_read_is_merged_forward() -> None:
    """A 300 ms page flashes; a slightly long page reads fine.

    Force one page per sentence with the char cap, give the middle one 300 ms, and check it
    folds into its predecessor instead of becoming a one-frame flash.
    """
    pages = plan_pages(
        [
            sentence(0, 0, 20_000, "长长长长长"),
            sentence(1, 20_000, 20_300, "短短短短短"),
            sentence(2, 20_300, 40_000, "下下下下下"),
        ],
        max_page_chars=5,
        min_page_ms=MIN_PAGE_MS,
    )
    assert len(pages) == 2
    assert pages[0].last_ord == 1, "the 300 ms page must have folded into the previous one"
    assert "短短短短短" in pages[0].context


def test_pages_carry_the_source_sentence_ord() -> None:
    """The editor jumps to it and the video block snaps to it, so it must be the first one."""
    pages = plan_pages(
        [
            sentence(7, 0, 40_000, "甲。"),
            sentence(8, 60_000, 70_000, "乙。"),
        ],
        target_page_ms=30_000,
        seam_after_ms=NO_CAP,
        min_page_ms=NO_MERGE,
    )
    assert [page.source_ord for page in pages] == [7, 8]


def test_pages_carry_absolute_milliseconds() -> None:
    """``video_item`` stores absolute ms for a reason (§19.3): ord is not an identity."""
    pages = plan_pages([sentence(0, 12_345, 20_000, "甲。")])
    assert pages[0].start_ms == 12_345


def test_a_page_keeps_more_context_than_it_shows() -> None:
    """The body is trimmed for the slide; the proofreading pane shows the full stretch."""
    text = "字" * 3000
    pages = plan_pages([sentence(0, 0, 30_000, text)], max_page_chars=480)
    assert len(pages[0].body) == 480
    assert len(pages[0].context) > len(pages[0].body)


def test_newlines_in_the_source_do_not_survive() -> None:
    """Sentence segmentation newlines are an artefact, not meaning."""
    pages = plan_pages([sentence(0, 0, 3000, "第一行\n第二行")])
    assert "\n" not in pages[0].body


# --- titles -------------------------------------------------------------


def test_the_title_is_the_first_clause() -> None:
    pages = plan_pages([sentence(0, 0, 3000, "这期我们聊罗小黑，也聊了六年。")])
    assert pages[0].title == "这期我们聊罗小黑"


def test_the_earliest_clause_mark_wins() -> None:
    """Taking 「。」 first would return a whole sentence on every page."""
    pages = plan_pages([sentence(0, 0, 3000, "先说结论，后面还有很长的一段解释。")])
    assert pages[0].title == "先说结论"


def test_a_title_is_capped() -> None:
    pages = plan_pages([sentence(0, 0, 3000, "字" * 200)])
    assert len(pages[0].title) <= 26


def test_a_title_with_no_punctuation_is_cut_at_the_cap() -> None:
    pages = plan_pages([sentence(0, 0, 3000, "这是一个非常非常长而且完全没有标点的句子")])
    assert len(pages[0].title) <= 26
    assert pages[0].title


def test_a_very_short_clause_is_not_used_as_a_title() -> None:
    """「你好」 followed by the real content would make every page look titled 「你好」."""
    pages = plan_pages([sentence(0, 0, 3000, "好。这期我们聊一部做了六年的动画电影。")])
    assert pages[0].title != "好"


# --- spans --------------------------------------------------------------


def test_a_page_runs_until_the_next_one() -> None:
    pages = plan_pages(
        [
            sentence(0, 0, 40_000, "甲。"),
            sentence(1, 70_000, 80_000, "乙。"),
        ],
        target_page_ms=30_000,
        seam_after_ms=NO_CAP,
        min_page_ms=NO_MERGE,
    )
    assert len(pages) == 2
    spans = page_spans(pages, total_ms=200_000)
    assert [(start, end) for _page, start, end in spans] == [
        (pages[0].start_ms, pages[1].start_ms),
        (pages[1].start_ms, 200_000),
    ]


def test_the_last_page_runs_to_the_end_of_the_film() -> None:
    pages = plan_pages([sentence(0, 1000, 3000, "唯一一页。")])
    spans = page_spans(pages, total_ms=9000)
    assert spans[-1][2] == 9000


def test_pages_are_contiguous_and_ordered() -> None:
    """A gap between two pages is a stretch where nothing is drawn — a black hole in the film."""
    pages = plan_pages(stretch(30, per_sentence=10_000))
    spans = page_spans(pages, total_ms=300_000)
    assert len(spans) > 1
    for (_page, _start, end), (_next, next_start, _next_end) in pairwise(spans):
        assert end == next_start


def test_page_plan_is_frozen() -> None:
    page = PagePlan(
        source_ord=0, start_ms=0, last_ord=0, title="t", text="b", context="c", speaker=0
    )
    try:
        page.title = "changed"  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("PagePlan must be frozen: a page is what the row was built from")


# --- the shipped defaults -----------------------------------------------


def test_the_documented_defaults() -> None:
    assert (TARGET_PAGE_MS, SEAM_AFTER_MS, MAX_PAGE_CHARS) == (60_000, 45_000, 480)


def test_an_hour_of_speech_becomes_a_deck_not_a_slideshow() -> None:
    """The number this feature is judged on.

    The real episode is 61.5 min / 17,294 characters at 4.69 chars/s, with sentence gaps of
    p50 320 ms / p75 520 ms / p90 830 ms. Modelled here as 583 sentences of ~6.3 s with a
    320 ms gap, which is the measured shape. The naive "break at every pause over 1.5 s"
    rule gave **238 pages**; a deck wants tens.
    """
    sentences: list[ScriptSentence] = []
    cursor = 0
    for index in range(583):
        length = 6_340
        sentences.append(sentence(index, cursor, cursor + length, "这是一段口播内容，大约六个字一秒。"))
        cursor += length + 320

    pages = plan_pages(sentences)
    assert 40 <= len(pages) <= 80, f"{len(pages)} pages for an hour is not a deck"
    spans = page_spans(pages, total_ms=cursor)
    durations = sorted((end - start) / 1000 for _page, start, end in spans)
    assert durations[0] >= MIN_PAGE_MS / 1000, "no page may flash past"
    assert durations[len(durations) // 2] >= 45, "the median page must be readable"
    assert durations[-1] <= 200, "a page held for minutes is a stalled deck"


def test_a_two_minute_clip_is_a_few_pages() -> None:
    """A short clip must not get a page every fifteen seconds either."""
    pages = plan_pages(stretch(20, per_sentence=6_000))
    assert 1 <= len(pages) <= 4
