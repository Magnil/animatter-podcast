"""Outline → hotwords before a project exists, and archived saves that download.

Two behaviours that only exist because of how the UI is used:

* The new-project dialog has to produce热词 *before* the transcription runs, and
  at that moment there is no project and no media row. Keying extraction to a
  media id would have made the feature available everywhere except the one place
  it changes the audio.
* 保存项目 must both put a file in the user's Downloads and say where the server
  keeps the original. Returning only one of the two is what produced "I clicked
  save, nothing downloaded, so I don't know where the file is".
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.main import create_app
from animatter.providers.llm.bailian import BailianLlm, LlmReply


@pytest.fixture()
def keyed_client(tmp_path: Path):  # type: ignore[no-untyped-def]
    """A client whose settings carry a (fake) LLM key.

    Without it the suggest endpoint correctly refuses with 503, which is the
    other half of this file's contract and is asserted separately below.
    """
    settings = Settings(data_dir=tmp_path / "data", log_level="ERROR", dashscope_api_key="sk-test")
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture()
def fake_llm(monkeypatch):  # type: ignore[no-untyped-def]
    """Replace the network call, keep everything else (prompt, sanitising)."""
    captured: dict[str, str] = {}

    def chat_json(
        self: BailianLlm, *, system: str, user: str, client: object, max_tokens: int = 2000
    ) -> tuple[dict[str, object], LlmReply]:
        captured["system"] = system
        captured["user"] = user
        return (
            {
                "terms": [
                    {"text": "马小褂", "weight": 5, "kind": "person", "evidence": "嘉宾马小褂"},
                    {"text": "漫物质", "weight": 4, "kind": "product", "evidence": ""},
                    # Junk the sanitiser must drop: a pronoun and a bare number.
                    {"text": "我们", "weight": 3},
                    {"text": "2024", "weight": 3},
                ]
            },
            LlmReply(text="", prompt_tokens=120, completion_tokens=40, model="qwen-plus"),
        )

    monkeypatch.setattr(BailianLlm, "chat_json", chat_json)
    return captured


def test_suggest_needs_no_project_or_media(keyed_client: TestClient, fake_llm: dict[str, str]) -> None:
    response = keyed_client.post(
        "/api/hotwords/suggest",
        json={"content": "本期嘉宾：马小褂，聊《漫物质》", "meta": "S02E07"},
    )
    assert response.status_code == 200
    body = response.json()
    assert {term["text"] for term in body["terms"]} == {"马小褂", "漫物质"}
    # The outline really reached the prompt, rather than an empty string.
    assert "马小褂" in fake_llm["user"]
    assert "S02E07" in fake_llm["user"]


def test_suggest_filters_junk_before_it_ever_reaches_a_transcription(
    keyed_client: TestClient, fake_llm: dict[str, str]
) -> None:
    body = keyed_client.post("/api/hotwords/suggest", json={"content": "大纲"}).json()
    assert [term["text"] for term in body["terms"]] == ["马小褂", "漫物质"]
    assert len(body["rejected"]) == 2


def test_suggest_writes_nothing(keyed_client: TestClient, fake_llm: dict[str, str], session: Session) -> None:
    """Extraction is a proposal: no project, no episode list, no glossary row."""
    keyed_client.post("/api/hotwords/suggest", json={"content": "本期嘉宾：马小褂"})
    assert keyed_client.get("/api/hotwords").json() == []


def test_suggest_rejects_an_empty_outline(keyed_client: TestClient, fake_llm: dict[str, str]) -> None:
    response = keyed_client.post("/api/hotwords/suggest", json={"content": "   "})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "outline_empty"
    assert response.json()["error"]["hint"]


def test_suggest_reports_missing_api_key(tmp_path: Path) -> None:
    """Degrade with an actionable hint instead of a stack trace."""
    settings = Settings(data_dir=tmp_path / "data", log_level="ERROR", dashscope_api_key=None)
    with TestClient(create_app(settings)) as client:
        response = client.post("/api/hotwords/suggest", json={"content": "大纲"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "missing_api_key"


# --- archive download ---------------------------------------------------


@pytest.fixture()
def seeded(client: TestClient, session: Session):  # type: ignore[no-untyped-def]
    from tests.test_corrections import seed_version

    version = seed_version(session, text="今天我们来聊一聊播客剪辑。", sizes=[13])
    session.commit()
    return version


def test_download_is_an_attachment(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    response = client.get(f"/api/projects/{seeded.project_id}/archive/file")
    assert response.status_code == 200
    # Without this header the browser would render a zip, not save it.
    assert response.headers["content-disposition"].startswith("attachment")
    assert len(response.content) > 0


def test_download_says_where_the_server_kept_the_file(
    client: TestClient, session: Session, seeded  # type: ignore[no-untyped-def]
) -> None:
    """The notice names a path the 载入项目 dialog can read straight back."""
    response = client.get(f"/api/projects/{seeded.project_id}/archive/file")
    path = Path(unquote(response.headers["x-archive-path"]))

    assert path.is_absolute()
    assert path.exists()
    # Absolute alone is not enough: it must be the file the loader will accept.
    reloaded = client.post("/api/archive/load", json={"path": str(path), "name": "副本"})
    assert reloaded.status_code == 200
    assert unquote(response.headers["x-archive-filename"]) == path.name


def test_download_headers_survive_a_chinese_project_name(
    client: TestClient, session: Session, seeded  # type: ignore[no-untyped-def]
) -> None:
    """HTTP headers are latin-1; an unencoded 「第 07 期」 would raise on the way out."""
    from animatter.models import Project

    project = session.get(Project, seeded.project_id)
    assert project is not None
    project.name = "第 07 期：对话某某"
    session.commit()

    response = client.get(f"/api/projects/{seeded.project_id}/archive/file")
    assert response.status_code == 200
    filename = unquote(response.headers["x-archive-filename"])
    assert "第 07 期" in filename
    assert filename.endswith(".project")


def test_download_counts_match_the_saved_payload(client: TestClient, session: Session, seeded) -> None:  # type: ignore[no-untyped-def]
    response = client.get(f"/api/projects/{seeded.project_id}/archive/file")
    assert int(response.headers["x-archive-sentences"]) == 1
    assert int(response.headers["x-archive-deletions"]) == 0
    assert int(response.headers["x-archive-ops"]) >= 0
