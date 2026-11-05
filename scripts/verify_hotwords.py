"""End-to-end check that hotwords reach the recognition request.

The bug: the new-project dialog sent ``use_hotwords: true``, pydantic had no such
field, and every run recorded ``"vocabulary": {}``. The UI said hotwords were on;
the audio still came back as 漫无日 / 马小褂.

This script proves the fix against the *real* backend and the *real* provider:
create a project, register the sample, add an episode term, transcribe, then read
``transcribe_job.params_json`` back out of SQLite. A passing unit test says the
map was passed to the service; only this says it survived the whole chain.

Run it while `animatter serve` is up:

    .venv/Scripts/python.exe scripts/verify_hotwords.py samples/test.mp3 "马小褂"
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8317"


def call(method: str, path: str, payload: object | None = None) -> object:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=1800) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:  # surface the server's explanation
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"{method} {path} -> {error.code}: {detail}") from error
    return json.loads(body) if body else None


def job_params(db_path: Path, job_id: str) -> dict[str, object]:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("select params_json from transcribe_job where id = ?", (job_id,)).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit(f"no transcribe_job row for {job_id}")
    return dict(json.loads(row[0]))


def main() -> int:
    audio = Path(sys.argv[1] if len(sys.argv) > 1 else "samples/test.mp3").resolve()
    term = sys.argv[2] if len(sys.argv) > 2 else "马小褂"
    db_path = Path(sys.argv[3] if len(sys.argv) > 3 else "data/app.db")

    if not audio.exists():
        raise SystemExit(f"missing audio: {audio}")

    project = call("POST", "/api/projects", {"name": f"hotword verify {int(time.time())}"})
    assert isinstance(project, dict)
    project_id = str(project["id"])
    print("project:", project_id)

    media = call("POST", f"/api/projects/{project_id}/media", {"path": str(audio)})
    assert isinstance(media, dict)
    media_id = str(media["id"])
    print("media:", media_id, f'{media["duration_ms"]}ms')

    added = call(
        "POST",
        f"/api/projects/{project_id}/media/{media_id}/hotwords",
        [{"text": term, "weight": 3}],
    )
    print("episode hotwords:", json.dumps(added, ensure_ascii=False)[:200])

    preview = call("GET", f"/api/projects/{project_id}/media/{media_id}/vocabulary")
    print("vocabulary preview:", json.dumps(preview, ensure_ascii=False))
    assert isinstance(preview, dict) and str(term) in preview["vocabulary"], preview

    version = call(
        "POST",
        f"/api/projects/{project_id}/transcribe",
        {"media_id": media_id, "use_hotwords": True},
    )
    assert isinstance(version, dict)
    print("version:", version)

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "select id from transcribe_job where project_id = ? order by created_at desc limit 1",
            (project_id,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise SystemExit("no job row: is the CLI running against a different database?")

    params = job_params(db_path, str(row[0]))
    vocabulary = params.get("vocabulary") or {}
    print("params.vocabulary:", json.dumps(vocabulary, ensure_ascii=False))

    ok = isinstance(vocabulary, dict) and vocabulary.get(term) is not None
    print("PASS" if ok else "FAIL: the term never reached parameters.vocabulary")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
