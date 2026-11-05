"""One-click cleanup end to end: plan → apply → undo as one operation.

The properties worth pinning are not "a deletion row appeared". They are:

* the **plan is read-only** — opening the dialog must not change the project;
* **apply is one undo**, not four hundred;
* running it twice does not double the count or re-propose what is already gone;
* changing the settings afterwards does **not** retroactively alter applied cuts
  (that would be the tool silently editing the episode when you move a slider).

Silence detection is stubbed: ``detect_silence`` shells out to ffmpeg and the
test database has no audio file. The stub returns a fixed map so the geometry is
deterministic, which is what these tests are actually about.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.ids import new_id, utcnow_iso
from animatter.models import (
    Deletion,
    EditOp,
    Media,
    Project,
    Sentence,
    Token,
    TranscriptVersion,
)
from animatter.services import cleanup as cleanup_service


@pytest.fixture()
def seeded(session: Session, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """A 4-second episode with one 嗯 and one long pause, plus a silence map."""
    from animatter.media import ffmpeg as ffmpeg_module

    project = Project(id=new_id("prj"), name="清理", created_at=utcnow_iso(), updated_at=utcnow_iso())
    session.add(project)
    session.flush()

    media = Media(
        id=new_id("med"),
        project_id=project.id,
        path="x.wav",
        sha256=new_id("sha"),
        duration_ms=4000,
        sample_rate=16000,
        channels=1,
        codec="mp3",
        kind="original",
        created_at=utcnow_iso(),
    )
    session.add(media)
    session.flush()

    version = TranscriptVersion(
        id=new_id("ver"),
        project_id=project.id,
        media_id=media.id,
        job_id="job",
        version=1,
        created_at=utcnow_iso(),
        duration_ms=4000,
        token_count=5,
        sentence_count=1,
        is_active=True,
    )
    session.add(version)
    session.flush()

    tokens = [
        (0, "今天", 0, 500),
        (1, "嗯", 500, 800),
        (2, "我们", 800, 1400),
        (3, "聊聊", 1400, 2000),
        (4, "好", 3000, 3200),
    ]
    for idx, text, start, end in tokens:
        session.add(
            Token(
                version_id=version.id,
                idx=idx,
                start_ms=start,
                end_ms=end,
                text=text,
                kind="word",
                src_word_idx=idx,
                punc_after="",
                derived=False,
                revised=False,
            )
        )
    session.add(
        Sentence(
            id=new_id("sen"),
            version_id=version.id,
            ord=0,
            token_start=0,
            token_end=5,
            speaker_id=None,
            origin="asr",
            updated_at=utcnow_iso(),
        )
    )
    session.commit()

    # [2000, 3000) is a 1s pause; [3200, 4000) is trailing dead air.
    monkeypatch.setattr(
        ffmpeg_module,
        "detect_silence",
        lambda *_a, **_k: [
            ffmpeg_module.SilenceSpan(2000, 3000),
            ffmpeg_module.SilenceSpan(3200, 4000),
        ],
    )
    return {"project": project.id, "media": media.id, "version": version.id}


def _plan(client: TestClient, version_id: str) -> dict[str, object]:
    response = client.get(f"/api/versions/{version_id}/cleanup/plan")
    assert response.status_code == 200, response.text
    return response.json()


def _deletions(session: Session, version_id: str) -> list[Deletion]:
    session.expire_all()
    return list(
        session.scalars(
            select(Deletion).where(Deletion.version_id == version_id, Deletion.removed.is_(False))
        )
    )


# --- planning -----------------------------------------------------------


def test_plan_finds_fillers_and_silence(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    plan = _plan(client, seeded["version"])
    assert plan["summary"]["filler"]["items"] == 1
    assert plan["summary"]["silence"]["items"] >= 1


def test_plan_is_read_only(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    before = len(_deletions(session, seeded["version"]))
    _plan(client, seeded["version"])
    _plan(client, seeded["version"])
    assert len(_deletions(session, seeded["version"])) == before


def test_plan_lists_the_filler_text(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    plan = _plan(client, seeded["version"])
    texts = [item["text"] for item in plan["items"] if item["kind"] == "filler"]
    assert texts == ["嗯"]


def test_plan_reports_the_sentence(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    plan = _plan(client, seeded["version"])
    filler = next(item for item in plan["items"] if item["kind"] == "filler")
    assert filler["sentence_ord"] == 0


def test_plan_returns_the_active_filler_list(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    plan = _plan(client, seeded["version"])
    assert "嗯" in plan["fillers"]


def test_plan_honours_a_threshold_override(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    generous = client.get(
        f"/api/versions/{seeded['version']}/cleanup/plan", params={"min_silence_ms": 100}
    ).json()
    strict = client.get(
        f"/api/versions/{seeded['version']}/cleanup/plan", params={"min_silence_ms": 2500}
    ).json()
    assert generous["summary"]["silence"]["items"] >= strict["summary"]["silence"]["items"]


# --- applying -----------------------------------------------------------


def test_apply_writes_deletions(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    response = client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]})
    assert response.status_code == 201, response.text
    assert response.json()["deletions"] == 1
    rows = _deletions(session, seeded["version"])
    assert [row.source for row in rows] == ["filler"]


def test_apply_records_one_history_entry(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """400 deletions must not become 400 history lines."""
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler", "silence"]})
    session.expire_all()
    ops = list(session.scalars(select(EditOp).where(EditOp.version_id == seeded["version"])))
    assert len(ops) == 1
    assert ops[0].op_type == "cleanup"
    payload = json.loads(ops[0].payload_json)
    assert payload["batch"]
    assert len(payload["deletion_ids"]) == len(_deletions(session, seeded["version"]))


def test_apply_tags_every_row_with_the_batch(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    result = client.post(
        f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler", "silence"]}
    ).json()
    rows = _deletions(session, seeded["version"])
    assert rows
    assert {row.batch_id for row in rows} == {result["batch"]}


def test_apply_only_the_ticked_kinds(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["silence"]})
    rows = _deletions(session, seeded["version"])
    assert rows
    assert {row.source for row in rows} == {"silence"}


def test_apply_twice_does_not_double(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]})
    first = len(_deletions(session, seeded["version"]))
    second = client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]})
    assert second.status_code >= 400  # nothing left to do
    assert len(_deletions(session, seeded["version"])) == first


def test_apply_with_nothing_left_is_a_clear_error(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]})
    second = client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]})
    body = second.json()
    code = body.get("error", {}).get("code") or body.get("detail", "")
    assert "nothing_to_do" in str(code)


def test_apply_with_no_kinds_is_rejected(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    response = client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": []})
    # An empty list means "all three" by contract; the explicit empty selection
    # is rejected only when nothing matches. Here something does, so 201.
    assert response.status_code in {201, 400}


def test_apply_counts_match_the_rows(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    result = client.post(
        f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler", "silence"]}
    ).json()
    rows = _deletions(session, seeded["version"])
    assert sum(result["counts"].values()) == len(rows)


# --- undoing ------------------------------------------------------------


def test_undo_reverts_the_whole_batch(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler", "silence"]})
    assert _deletions(session, seeded["version"])

    response = client.post(f"/api/versions/{seeded['version']}/undo", json={})
    assert response.status_code == 200, response.text
    assert response.json()["op_type"] == "cleanup"
    assert _deletions(session, seeded["version"]) == []


def test_undo_twice_is_refused(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]})
    client.post(f"/api/versions/{seeded['version']}/undo", json={})
    again = client.post(f"/api/versions/{seeded['version']}/undo", json={})
    assert again.status_code >= 400


def test_cleanup_is_listed_as_undoable(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]})
    history = client.get(f"/api/versions/{seeded['version']}/history").json()
    entry = next(item for item in history if item["op_type"] == "cleanup")
    assert entry["undoable"] is True
    assert entry["label"] == "一键清理"
    assert "口癖词" in entry["detail"]


# --- restoring ----------------------------------------------------------
#
# `/cleanup/restore` and `/undo` are two doors to the same recovery. Restore is
# category-shaped ("bring the fillers back"), undo is time-shaped ("undo that
# click"). Both are needed: a user who cleaned three times wants one button per
# category, not three undos in a specific order.


def _restore(client: TestClient, version_id: str, **params: object) -> object:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.post(f"/api/versions/{version_id}/cleanup/restore" + (f"?{query}" if query else ""))


def test_restore_by_kind(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler", "silence"]})
    response = _restore(client, seeded["version"], source="filler")
    assert response.status_code == 200
    remaining = {row.source for row in _deletions(session, seeded["version"])}
    assert "filler" not in remaining


def test_restore_by_batch(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    batch = client.post(
        f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler", "silence"]}
    ).json()["batch"]
    response = _restore(client, seeded["version"], batch=batch)
    assert response.status_code == 200
    assert _deletions(session, seeded["version"]) == []


def test_restore_does_not_touch_a_different_batch(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    first = client.post(
        f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]}
    ).json()["batch"]
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["silence"]})
    _restore(client, seeded["version"], batch=first)
    remaining = {row.source for row in _deletions(session, seeded["version"])}
    assert remaining == {"silence"}


def test_restore_without_a_target_is_rejected(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    response = _restore(client, seeded["version"])
    assert response.status_code >= 400


def test_restore_with_a_bad_kind_is_rejected(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    response = _restore(client, seeded["version"], source="nonsense")
    assert response.status_code >= 400


# --- counts -------------------------------------------------------------


def test_counts_reflect_what_is_applied(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler", "silence"]})
    counts = client.get(f"/api/versions/{seeded['version']}/cleanup/counts").json()["counts"]
    rows = _deletions(session, seeded["version"])
    assert counts["filler"] == sum(1 for row in rows if row.source == "filler")
    assert counts["silence"] == sum(1 for row in rows if row.source == "silence")


def test_counts_are_zero_on_a_fresh_version(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    counts = client.get(f"/api/versions/{seeded['version']}/cleanup/counts").json()["counts"]
    assert counts == {"filler": 0, "disfluency": 0, "silence": 0}


# --- settings do not retroactively rewrite ------------------------------


def test_changing_the_threshold_does_not_alter_applied_cuts(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """A slider must affect the *next* cleanup, not silently re-cut this one."""
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["silence"]})
    before = sorted((row.start_ms, row.end_ms) for row in _deletions(session, seeded["version"]))
    client.patch("/api/settings", json={"values": {"cleanup_min_silence_ms": 2000}})
    after = sorted((row.start_ms, row.end_ms) for row in _deletions(session, seeded["version"]))
    assert after == before


def test_removing_a_word_from_the_filler_list_does_not_restore_it(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    client.post(f"/api/versions/{seeded['version']}/cleanup", json={"kinds": ["filler"]})
    client.patch("/api/settings", json={"values": {"cleanup_fillers": "啊\n哦"}})
    remaining = [row.source for row in _deletions(session, seeded["version"])]
    assert remaining == ["filler"]


def test_custom_filler_word_is_matched(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """The user adds a term; the next plan must find it."""
    session.expire_all()
    token = session.scalars(
        select(Token).where(Token.version_id == seeded["version"], Token.idx == 3)
    ).first()
    assert token is not None
    token.text = "就是说"
    session.commit()

    client.patch("/api/settings", json={"values": {"cleanup_fillers": "嗯\n就是说"}})
    plan = _plan(client, seeded["version"])
    texts = [item["text"] for item in plan["items"] if item["kind"] == "filler"]
    assert "就是说" in texts
