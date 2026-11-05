"""End-to-end transcription against the real Bailian API.

Deselected by default (``-m live``): it uploads a file, spends quota, and needs
network. Run explicitly when changing anything in the upload/submit/download
path — no mock can prove the ``oss://`` + header combination actually works.

    pytest -m live tests/test_live_transcribe.py -s

Requires ``DASHSCOPE_API_KEY`` in the environment or ``.env``.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

import pytest

from animatter.config import load_settings
from animatter.db import Database
from animatter.migrate import upgrade_to_head
from animatter.services import transcribe as transcribe_service

pytestmark = pytest.mark.live

SPOKEN = "今天我们来聊一聊播客剪辑"


def _make_speech(path: Path) -> bool:
    """Synthesise a short Chinese sample with Windows SAPI (no network)."""
    if os.name != "nt":
        return False
    script = path.with_suffix(".ps1")
    script.write_text(
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        f"$s.SetOutputToWaveFile('{path}')\n"
        f"$s.Speak('{SPOKEN}')\n"
        "$s.Dispose()\n",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and path.exists() and path.stat().st_size > 10_000


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    base = load_settings()
    if not base.dashscope_api_key:
        pytest.skip("DASHSCOPE_API_KEY not configured")
    return base.model_copy(update={"data_dir": tmp_path_factory.mktemp("live_data")})


def test_full_chain_produces_valid_token_stream(settings) -> None:  # type: ignore[no-untyped-def]
    audio = settings.data_dir / "speech.wav"
    if not _make_speech(audio):
        pytest.skip("no local TTS available to synthesise a sample")

    upgrade_to_head(settings)
    db = Database(settings)
    try:
        with db.session() as session:
            from animatter.services import projects as project_service

            project = project_service.create_project(session, settings, "live e2e")
            media = transcribe_service.register_media(session, settings, project.id, audio)
            version = transcribe_service.transcribe_media(
                session,
                settings,
                project_id=project.id,
                media=media,
                options=transcribe_service.TranscribeOptions(diarization=False),
            )

            assert version.token_count > 10
            assert version.sentence_count >= 1

            tokens = transcribe_service.load_tokens(session, version.id)
            sentences = transcribe_service.load_sentences(session, version.id)

            # Timeline must be monotonic — this is what every later stage assumes.
            for previous, current in itertools.pairwise(tokens):
                assert previous.end_ms <= current.start_ms, (
                    f"token {previous.idx} ends at {previous.end_ms} but "
                    f"token {current.idx} starts at {current.start_ms}"
                )

            # Sentences must cover the stream exactly (INV-1/INV-2).
            assert sentences[0].token_start == 0
            assert sentences[-1].token_end == len(tokens)
            for previous, current in itertools.pairwise(sentences):
                assert previous.token_end == current.token_start

            text = "".join(token.text for token in tokens)
            assert any("\u4e00" <= ch <= "\u9fff" for ch in text), "expected Chinese characters"

            # The sample was synthesised from known text; ASR should recover most of it.
            overlap = len(set(text) & set(SPOKEN)) / len(set(SPOKEN))
            assert overlap > 0.6, f"only {overlap:.0%} character overlap: {text}"
    finally:
        db.dispose()
