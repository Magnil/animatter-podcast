"""Settings storage and operation history.

Settings validation matters more than it looks: a rejected key must be *loud*.
A silently ignored setting looks exactly like a broken feature, and the user has
no way to tell the difference.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.services import settings_store

# --- settings -----------------------------------------------------------


def test_defaults_are_returned_for_a_fresh_database(session: Session) -> None:
    values = settings_store.load(session)
    assert values["hotword_weight"] == 3
    assert values["asr_model"] == "qwen-audio-3.1-asr-flash-filetrans"


def test_save_updates_only_the_given_keys(session: Session) -> None:
    settings_store.save(session, {"export_format": "wav"})
    values = settings_store.load(session)
    assert values["export_format"] == "wav"
    assert values["hotword_weight"] == 3  # untouched


def test_save_persists_across_reads(session: Session) -> None:
    settings_store.save(session, {"hotword_weight": 4})
    session.expire_all()
    assert settings_store.load(session)["hotword_weight"] == 4


def test_unknown_key_is_rejected(session: Session) -> None:
    # A typo must fail loudly; silently storing it makes the setting look broken.
    with pytest.raises(settings_store.SettingsError, match="未知的设置项"):
        settings_store.save(session, {"not_a_setting": 1})


def test_out_of_range_int_is_rejected(session: Session) -> None:
    with pytest.raises(settings_store.SettingsError, match="1–50"):
        settings_store.save(session, {"hotword_weight": 999})


def test_bool_setting_rejects_non_bool(session: Session) -> None:
    with pytest.raises(settings_store.SettingsError):
        settings_store.save(session, {"diarization": "yes"})


def test_enum_setting_rejects_unknown_value(session: Session) -> None:
    with pytest.raises(settings_store.SettingsError):
        settings_store.save(session, {"export_format": "ogg"})


def test_reset_restores_defaults(session: Session) -> None:
    settings_store.save(session, {"export_format": "wav", "hotword_weight": 9})
    settings_store.reset(session)
    values = settings_store.load(session)
    assert values["export_format"] == "m4a"
    assert values["hotword_weight"] == 3


def test_corrupt_payload_falls_back_to_defaults(session: Session) -> None:
    from animatter.models import Setting

    settings_store.load(session)  # creates the singleton row
    row = session.get(Setting, settings_store.SETTINGS_ID)
    assert row is not None
    row.payload_json = "{not json"
    session.flush()
    # A corrupt file must not take the whole app down.
    assert settings_store.load(session)["hotword_weight"] == 3


def test_models_report_capabilities(client: TestClient) -> None:
    payload = client.get("/api/settings/models").json()
    by_id = {item["id"]: item for item in payload["asr"]}
    # Choosing a model silently decides whether hotwords work at all, so the
    # flags have to come from the provider table rather than be guessed.
    assert by_id["paraformer-v2"]["instant_vocabulary"] is False
    assert by_id["qwen-audio-3.1-asr-flash-filetrans"]["instant_vocabulary"] is True


# --- history ------------------------------------------------------------


def test_describe_op_labels_boundary_action(client: TestClient) -> None:
    session: Session = client.app.state.db.session().__enter__()  # type: ignore[attr-defined]
    from tests.test_corrections import seed_version

    from animatter.services import editing

    version = seed_version(session, text="一二三四", sizes=[2, 2])
    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=4)
    ops = editing.list_ops(session, version.id)
    described = editing.describe_op(ops[0])
    assert described["op_type"] == "boundary"
    assert described["label"] == "调整句边界"
    assert described["undoable"] is True


def test_undo_specific_op_restores_sentence_count(client: TestClient) -> None:
    session: Session = client.app.state.db.session().__enter__()  # type: ignore[attr-defined]
    from tests.test_corrections import seed_version

    from animatter.services import editing

    version = seed_version(session, text="一二三四", sizes=[2, 2])
    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=4)
    op = editing.list_ops(session, version.id)[0]

    editing.undo_op(session, version.id, op.id)
    assert len(editing.list_ops(session, version.id)[0].payload_json) > 0
    # Two sentences again after undoing the merge.
    from animatter.services import transcribe as transcribe_service

    assert len(transcribe_service.load_sentences(session, version.id)) == 2


def test_undo_twice_is_rejected(client: TestClient) -> None:
    session: Session = client.app.state.db.session().__enter__()  # type: ignore[attr-defined]
    from tests.test_corrections import seed_version

    from animatter.services import editing

    version = seed_version(session, text="一二三四", sizes=[2, 2])
    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=4)
    op = editing.list_ops(session, version.id)[0]
    editing.undo_op(session, version.id, op.id)

    with pytest.raises(editing.EditError) as caught:
        editing.undo_op(session, version.id, op.id)
    assert "已经撤销" in str(caught.value)


def test_undo_refuses_out_of_order(client: TestClient) -> None:
    """Reverting an older op after a newer one would replay a stale snapshot."""
    session: Session = client.app.state.db.session().__enter__()  # type: ignore[attr-defined]
    from tests.test_corrections import seed_version

    from animatter.services import editing

    version = seed_version(session, text="一二三四五六", sizes=[2, 2, 2])
    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=4)
    first = editing.list_ops(session, version.id)[0]
    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=6)

    with pytest.raises(editing.EditError) as caught:
        editing.undo_op(session, version.id, first.id)
    assert "只能撤销最近" in str(caught.value)


def test_text_edit_is_not_directly_undoable(client: TestClient) -> None:
    session: Session = client.app.state.db.session().__enter__()  # type: ignore[attr-defined]
    from tests.test_corrections import seed_version

    from animatter.services import editing

    version = seed_version(session, text="句级标签", sizes=[4])
    editing.apply_text_edit(session, version.id, token_start=0, token_end=2, proposed="句集")
    op = editing.list_ops(session, version.id)[0]
    assert editing.describe_op(op)["undoable"] is False

    with pytest.raises(editing.EditError) as caught:
        editing.undo_op(session, version.id, op.id)
    assert "不支持直接撤销" in str(caught.value)


def test_history_endpoint_shape(client: TestClient) -> None:
    session: Session = client.app.state.db.session().__enter__()  # type: ignore[attr-defined]
    from tests.test_corrections import seed_version

    from animatter.services import editing

    version = seed_version(session, text="一二三四", sizes=[2, 2])
    editing.apply_boundary(session, version.id, action="merge", from_token=0, to_token=4)
    session.commit()  # the HTTP client reads through a separate session

    payload = client.get(f"/api/versions/{version.id}/history").json()
    assert len(payload) == 1
    assert set(payload[0]) >= {"id", "label", "detail", "undone", "undoable", "created_at"}
