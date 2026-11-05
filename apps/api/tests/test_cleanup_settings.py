"""Cleanup settings: defaults, validation, and the "no retroactive rewrite" rule.

The last one is the important one. A slider that silently re-cuts an episode the
user already cleaned is indistinguishable from data loss: they move a threshold,
the audio changes, and nothing says why. Settings therefore only feed the *next*
plan; the plan snapshots what it used.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.core.fillers import DEFAULT_FILLERS, parse_word_list
from animatter.services import settings_store


def test_defaults_exist_for_every_cleanup_key() -> None:
    for key in (
        "cleanup_fillers",
        "cleanup_min_silence_ms",
        "cleanup_silence_pad_ms",
        "cleanup_min_disfluency_ms",
        "silence_noise_db",
        "silence_min_duration_ms",
    ):
        assert key in settings_store.DEFAULTS, key


def test_default_filler_list_is_the_curated_one() -> None:
    """The settings value and the core list must not drift apart."""
    assert parse_word_list(settings_store.DEFAULTS["cleanup_fillers"]) == list(DEFAULT_FILLERS)


def test_defaults_survive_their_own_validation() -> None:
    """A default the validator rejects would make the settings page unsaveable."""
    for key, value in settings_store.DEFAULTS.items():
        settings_store._validate(key, value)


def test_noise_db_accepts_quiet_values() -> None:
    assert settings_store._validate("silence_noise_db", -35) == -35
    assert settings_store._validate("silence_noise_db", -60) == -60


def test_noise_db_rejects_positive_values() -> None:
    with pytest.raises(settings_store.SettingsError):
        settings_store._validate("silence_noise_db", 5)


def test_silence_threshold_bounds() -> None:
    assert settings_store._validate("cleanup_min_silence_ms", 100) == 100
    assert settings_store._validate("cleanup_min_silence_ms", 3000) == 3000
    with pytest.raises(settings_store.SettingsError):
        settings_store._validate("cleanup_min_silence_ms", 50)


def test_pad_can_be_zero() -> None:
    """Zero pad is a legitimate choice: cut flush against the word."""
    assert settings_store._validate("cleanup_silence_pad_ms", 0) == 0


def test_filler_list_must_be_text() -> None:
    with pytest.raises(settings_store.SettingsError):
        settings_store._validate("cleanup_fillers", ["嗯", "呃"])


def test_unknown_cleanup_key_is_rejected(session: Session) -> None:
    with pytest.raises(settings_store.SettingsError):
        settings_store.save(session, {"cleanup_min_silence": 300})


def test_round_trip_through_the_api(client: TestClient) -> None:
    patched = client.patch(
        "/api/settings",
        json={"values": {"cleanup_min_silence_ms": 450, "cleanup_fillers": "嗯\n就是说"}},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["cleanup_min_silence_ms"] == 450
    assert parse_word_list(body["cleanup_fillers"]) == ["嗯", "就是说"]

    reloaded = client.get("/api/settings").json()
    assert reloaded["cleanup_min_silence_ms"] == 450


def test_invalid_patch_is_rejected_without_changing_anything(client: TestClient) -> None:
    client.patch("/api/settings", json={"values": {"cleanup_min_silence_ms": 400}})
    bad = client.patch("/api/settings", json={"values": {"cleanup_min_silence_ms": 10}})
    assert bad.status_code >= 400
    assert client.get("/api/settings").json()["cleanup_min_silence_ms"] == 400


def test_reset_restores_the_curated_defaults(client: TestClient) -> None:
    client.patch("/api/settings", json={"values": {"cleanup_fillers": "只有这一个"}})
    reset = client.post("/api/settings/reset").json()
    assert parse_word_list(reset["cleanup_fillers"]) == list(DEFAULT_FILLERS)
