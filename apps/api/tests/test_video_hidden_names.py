"""An unbound avatar slot takes its name with it.

Round 97 added a static photo for unbound slots; round 98 reverses that (未绑定的槽就应该消失) and
adds the other half: the **name plate** must go too. Both are driven by the same set — the slots
that have a track — so the picture and the text cannot disagree.
"""

from __future__ import annotations

from animatter.core import textspan
from animatter.services import video_edits


class _Row:
    """The two columns ``video_edits`` reads, without a database."""

    def __init__(self, slots: dict, edits: dict | None = None) -> None:
        import json

        self.id = "vtpl_test"
        self.source_path = "C:/tmp/video/templates/source.psd"
        self.background_path = "C:/tmp/video/templates/build/background.png"
        self.slots_json = json.dumps(slots, ensure_ascii=False)
        self.edits_json = json.dumps(edits or {}, ensure_ascii=False)
        self.bindings_json = "{}"


def _slots() -> dict:
    return {
        "avatars": [],
        "texts": [
            {
                "key": "title",
                "role": "title",
                "label": "当期标题",
                "rect": {"x": 100, "y": 50, "w": 400, "h": 100},
                "text": "标题",
                "font_size": 40,
                "colour": textspan.BLUE,
                "font": "Alimama ShuHeiTi",
                "name": "标题层",
            },
            {
                "key": "avatar:头像1",
                "role": "avatar_name",
                "label": "主播1",
                "rect": {"x": 20, "y": 100, "w": 40, "h": 20},
                "text": "马小褂",
                "font_size": 16,
                "colour": textspan.RED,
                "font": "Alimama ShuHeiTi",
                "name": "马小褂",
            },
            {
                "key": "avatar:头像2",
                "role": "avatar_name",
                "label": "主播2",
                "rect": {"x": 20, "y": 300, "w": 40, "h": 20},
                "text": "彼方",
                "font_size": 16,
                "colour": textspan.RED,
                "font": "Alimama ShuHeiTi",
                "name": "彼方",
            },
        ],
    }


def test_a_bound_slot_keeps_its_name() -> None:
    row = _Row(_slots())
    drawn = video_edits.draw_slots(row, visible_avatars={"头像1"})
    texts = [slot.text for slot in drawn]
    assert "马小褂" in texts
    assert "彼方" not in texts, "the unbound slot's name is still being drawn"


def test_no_bindings_means_every_name_is_drawn() -> None:
    """``None`` is not "hide everything": with nothing bound the full poster is used, so every
    avatar — and therefore every name — is visible."""
    row = _Row(_slots())
    texts = [slot.text for slot in video_edits.draw_slots(row, visible_avatars=None)]
    assert "马小褂" in texts and "彼方" in texts


def test_non_avatar_slots_are_never_hidden() -> None:
    row = _Row(_slots())
    drawn = video_edits.draw_slots(row, visible_avatars=set())
    assert [slot.text for slot in drawn] == ["标题"]


def test_the_signature_sees_a_text_edit() -> None:
    """The signature is the still's file name and part of the export key.

    If it ignored the edits, a re-render would hand back the cached picture with the old title —
    the exact class of bug §19.11 trap 24 is about, and the one that cost a round already.
    """
    a = _Row(_slots())
    b = _Row(_slots(), {"texts": {"title": {"text": "换了标题"}}})
    assert video_edits.signature(a) != video_edits.signature(b)
    # Stable for the same edits: two identical rows must not produce two different files.
    assert video_edits.signature(a) == video_edits.signature(_Row(_slots()))


def test_a_coloured_default_survives_the_edit_round_trip() -> None:
    """``EP.**23**`` is the compile's default; storing it back must not escape the markers."""
    row = _Row(_slots())
    entry = video_edits.resolved_texts(row)
    assert entry[0]["text"] == "标题"
    assert textspan.parse("EP.**23**", base=textspan.BLUE)[0].text == "EP."
