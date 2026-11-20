"""P1's two HTTP surfaces: the plan, and the pixels it bakes (§19.31.2, §19.31.4).

The rendering is covered by ``test_video_plan.py``; what is pinned here is the **contract**,
because the browser depends on it and every mistake in it looks like a UI bug:

* the plan must carry only URLs — a path in the payload hands the browser the file system (§8);
* the file route must refuse anything the plan did not name, or it becomes a general file
  server over the bake directory;
* a cue index from a stale plan must 404 with the real count instead of drawing somebody
  else's subtitle;
* the style query parameters must produce a *different* digest, since that is what makes the
  baked names change when the user drags the font-size slider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from tests.test_corrections import seed_version
from tests.test_video_plan import make_asset, make_item, make_template, with_speaker

from animatter.services import video_plan

#: The real binary, not a stub: the band routes are the one place where a wrong filter string
#: still produces a file, so the test has to actually run ffmpeg.
FFMPEG = __import__("shutil").which("ffmpeg")


def seed(client: TestClient, session: Session, tmp_path: Path) -> tuple[str, str]:
    """A committed project with a template, one box block and a bound speaker."""
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={"头像1": 0})
    asset = make_asset(session, version.project_id, tmp_path)
    make_item(session, version.project_id, start_ms=0, end_ms=4500, asset_id=asset.id)
    session.commit()
    return version.project_id, version.id


# --- the plan -----------------------------------------------------------


def test_the_plan_describes_the_whole_film(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    _project, version_id = seed(client, session, tmp_path)
    response = client.get(f"/api/versions/{version_id}/video/plan")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["segments"], "a project with one block must describe that block"
    assert data["canvas"] == {"w": 1920, "h": 1080}
    assert data["box"] == {"x": 240, "y": 209, "w": 1440, "h": 810}
    assert data["output_duration_ms"] > 0
    assert data["digest"]
    # Every segment covers a positive stretch and they are ordered.
    starts = [segment["start_ms"] for segment in data["segments"]]
    assert starts == sorted(starts)
    assert all(segment["end_ms"] > segment["start_ms"] for segment in data["segments"])


def test_the_payload_contains_no_file_paths(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """A path would be a file-read primitive; the browser must only ever see URLs (§8)."""
    _project, version_id = seed(client, session, tmp_path)
    blob = json.dumps(client.get(f"/api/versions/{version_id}/video/plan").json())
    assert "C:\\\\" not in blob and "C:/" not in blob
    assert str(tmp_path).replace("\\", "\\\\") not in blob


def test_the_style_is_part_of_the_digest(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """Changing the font size must yield new artefact names, or the browser keeps the old ones.

    This is §19.11 trap 24 in the one place it is easiest to reintroduce: the baked cue band
    carries no font information of its own, so only the name can distinguish two sizes.
    """
    _project, version_id = seed(client, session, tmp_path)
    first = client.get(f"/api/versions/{version_id}/video/plan").json()
    second = client.get(
        f"/api/versions/{version_id}/video/plan", params={"font_size": 44}
    ).json()
    assert first["digest"] != second["digest"]
    assert {cue["url"] for cue in first["cues"]} != {cue["url"] for cue in second["cues"]}


def test_burning_subtitles_can_be_turned_off(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """The preview must not show subtitles the export was told not to burn."""
    _project, version_id = seed(client, session, tmp_path)
    data = client.get(
        f"/api/versions/{version_id}/video/plan", params={"burn_subtitles": False}
    ).json()
    assert data["cues"] == []


def test_a_project_without_a_template_says_so(
    client: TestClient, session: Session
) -> None:
    """The error must name the missing thing: "import a template", not "not found"."""
    version = seed_version(session)
    session.commit()
    response = client.get(f"/api/versions/{version.id}/video/plan")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "video_template_missing"
    assert "模板" in error["hint"]


def test_an_unknown_version_is_404(client: TestClient) -> None:
    response = client.get("/api/versions/ver_nope/video/plan")
    assert response.status_code == 404


# --- the baked files ----------------------------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg on PATH")
def test_a_cue_url_from_the_plan_serves_the_baked_band(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """The URL the plan hands the browser must be servable as-is, with no second round trip."""
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    data = client.get(f"/api/versions/{version.id}/video/plan").json()
    assert data["cues"], "the fixture's transcript has sentences"

    url = data["cues"][0]["url"]
    response = client.get(url)
    assert response.status_code == 200, response.text
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    # `immutable` is what makes scrubbing cheap: without it the browser revalidates every band
    # a minute of playback walks through, which is hundreds of requests.
    assert response.headers["cache-control"].endswith("immutable")


@pytest.mark.parametrize(
    "name",
    [
        "background.png",  # a template artefact, not a plan bake
        "cue-.png",  # right prefix, no digest
        "notes.txt",  # wrong prefix
        "backdrop-xyz.jpg",  # right prefix, no digest
    ],
)
def test_the_file_route_refuses_anything_but_a_bake_name(
    client: TestClient, session: Session, tmp_path: Path, name: str
) -> None:
    """The guard is the *shape* of the name, not just ``Path(name).name``.

    A route that accepted any existing file in the bake directory would still be safe today and
    would stop being safe the moment something else is written there.
    """
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    response = client.get(
        f"/api/video/plan/file/{name}", params={"project_id": version.project_id}
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "plan_file_invalid"


def test_a_name_that_is_well_formed_but_absent_is_a_404_with_a_hint(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    response = client.get(
        "/api/video/plan/file/cue-0123456789abcdef-00042.png",
        params={"project_id": version.project_id},
    )
    assert response.status_code == 404
    assert response.json()["error"]["hint"], "every error carries an actionable hint (§8)"


# --- the cue bake -------------------------------------------------------


@pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg on PATH")
def test_the_second_request_for_a_cue_band_reuses_the_file(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """Baking twice for the same URL is 77 ms wasted on every re-request.

    The browser caches, but a reload does not: this asserts the server does not re-run ffmpeg
    when the file is already on disk.
    """
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    plan = client.get(f"/api/versions/{version.id}/video/plan").json()
    url = plan["cues"][0]["url"]
    settings = client.app.state.settings  # type: ignore[attr-defined]
    target = _cue_file(settings, version.project_id, plan["digest"], 0)

    first = client.get(url)
    assert first.status_code == 200, first.text
    assert target.is_file()
    mtime = target.stat().st_mtime_ns

    second = client.get(url)
    assert second.status_code == 200
    assert second.content == first.content
    assert target.stat().st_mtime_ns == mtime, "the band was re-baked instead of reused"


def _cue_file(settings: object, project_id: str, digest: str, index: int) -> Path:
    """Where the cue route writes its band, derived the same way the route derives it.

    The settings must be the **test app's**, not a fresh ``Settings()``: the latter points at the
    real data directory, and the assertion would then be about a file the test never wrote.
    """
    return video_plan.bake_dir(settings, project_id) / video_plan.cue_name(digest, index)  # type: ignore[arg-type]


def test_a_cue_index_past_the_end_reports_the_real_count(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """An index from a stale plan must fail loudly, not draw somebody else's subtitle."""
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    digest = client.get(f"/api/versions/{version.id}/video/plan").json()["digest"]
    response = client.get(f"/api/versions/{version.id}/video/cue/{digest}/99999.png")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "cue_out_of_range"
    assert "条字幕" in error["hint"]


def test_a_stale_digest_is_refused_rather_than_baked_under_the_wrong_name(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """The digest in the path is checked, or `immutable` would be a lie.

    A plan fetched before an edit names bands with the old digest. Serving them from the new film
    would put the wrong subtitle under a URL the browser has been told never to revalidate.
    """
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    response = client.get(f"/api/versions/{version.id}/video/cue/0123456789abcdef/0.png")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "plan_digest_stale"


def test_a_negative_cue_index_is_rejected(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    digest = client.get(f"/api/versions/{version.id}/video/plan").json()["digest"]
    response = client.get(f"/api/versions/{version.id}/video/cue/{digest}/-1.png")
    assert response.status_code == 404


def test_the_cue_route_and_the_plan_agree_on_the_digest(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """The plan builds the URL by formatting a constant; the route parses it.

    If the two spellings drifted, every band would 404 or — worse — `plan_digest_stale`, which
    reads like a data problem rather than a routing one. This pins them to each other via the
    same `video_plan.build` call the route makes.
    """
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    plan = client.get(f"/api/versions/{version.id}/video/plan").json()
    assert plan["digest"] == video_plan.build(
        session, __import__("animatter.config", fromlist=["Settings"]).Settings(),
        version,
    ).digest
    for cue in plan["cues"][:3]:
        assert video_plan.CUE_ROUTE.format(
            version_id=version.id, digest=plan["digest"], index=cue["index"]
        ) == cue["url"]


def test_a_different_style_gets_a_different_cue_url(
    client: TestClient, session: Session, tmp_path: Path
) -> None:
    """Two sizes must not share a URL: the digest carries the style, and the URL the digest."""
    version = with_speaker(session)
    make_template(session, version.project_id, tmp_path, bindings={})
    session.commit()
    small = client.get(
        f"/api/versions/{version.id}/video/plan", params={"font_size": 40}
    ).json()
    large = client.get(
        f"/api/versions/{version.id}/video/plan", params={"font_size": 72}
    ).json()
    assert small["cues"][0]["url"] != large["cues"][0]["url"]
