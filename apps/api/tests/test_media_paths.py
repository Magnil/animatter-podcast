"""Non-ASCII media paths must survive the ffprobe round-trip.

Regression: ``subprocess.run(..., text=True)`` decodes with the *locale* codec,
which is cp936 on a Chinese Windows. ffprobe echoes the input filename inside its
JSON; for a Chinese path the reader thread raised ``UnicodeDecodeError``,
``subprocess.run`` swallowed it, ``stdout`` came back ``None`` with
``returncode == 0``, and ``probe()`` reported "文件里没有音轨" for a perfectly
good file. English filenames worked, so the bug looked like a mime/picker issue.

The failure mode is codec-dependent, so these tests prove the property rather
than the symptom: the pipeline never decodes with the locale codec.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from animatter.media import ffmpeg as ffmpeg_module
from animatter.media import render as render_module


def make_audio(path: Path, seconds: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(path),
        ],
        check=True,
    )
    return path


def test_probe_reads_chinese_filename(tmp_path: Path) -> None:
    source = make_audio(tmp_path / "测试中文 音频.wav", seconds=1.5)
    info = ffmpeg_module.probe("ffprobe", source)
    assert 1400 <= info.duration_ms <= 1700
    assert info.sample_rate == 16000


def test_probe_reads_chinese_directory(tmp_path: Path) -> None:
    source = make_audio(tmp_path / "播客素材" / "第 07 期.wav")
    assert ffmpeg_module.probe("ffprobe", source).channels == 1


def test_register_media_accepts_chinese_path(client: TestClient, tmp_path: Path) -> None:
    """The bug the user hit: upload succeeded in English, failed in Chinese."""
    source = make_audio(tmp_path / "漫物质 - 第 1 期.wav", seconds=2.0)

    project = client.post("/api/projects", json={"name": "中文路径"}).json()
    response = client.post(
        f"/api/projects/{project['id']}/media",
        json={"path": str(source)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert 1900 <= body["duration_ms"] <= 2200
    assert body["sample_rate"] == 16000
    assert body["codec"] == "pcm_s16le"


def test_transcode_to_chinese_destination(tmp_path: Path) -> None:
    source = make_audio(tmp_path / "源 文件.wav")
    target = tmp_path / "派生" / "识别 输入.mp3"
    produced = ffmpeg_module.transcode_for_asr("ffmpeg", source, target)
    assert produced.exists()
    assert ffmpeg_module.probe("ffprobe", produced).sample_rate == 16000


def test_no_caller_decodes_with_the_locale_codec() -> None:
    """`text=True` is the antipattern; catch it before it comes back.

    Parsed rather than grepped: the modules *mention* the trap in prose, and a
    substring search would fail on its own documentation.
    """
    roots = [Path(ffmpeg_module.__file__).parent, Path(render_module.__file__).parent]
    offenders: list[str] = []
    for root in roots:
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                callee = node.func
                name = getattr(callee, "attr", getattr(callee, "id", ""))
                if name not in {"run", "check_output", "Popen", "call"}:
                    continue
                keywords = {keyword.arg for keyword in node.keywords}
                if "text" in keywords or "universal_newlines" in keywords:
                    offenders.append(f"{path.name}:{node.lineno}: text=True")
                encoding = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "encoding"), None
                )
                # An explicit encoding is fine as long as it is not the locale
                # default; silence (bytes mode) is fine too — peaks.py reads raw
                # PCM and never decodes text.
                if encoding is not None and getattr(encoding, "value", None) != "utf-8":
                    offenders.append(f"{path.name}:{node.lineno}: encoding is not utf-8")
    assert offenders == [], offenders


def test_peaks_read_a_chinese_path(tmp_path: Path) -> None:
    """peaks.py stays in bytes mode; that must not become a filename problem."""
    from animatter.media import peaks as peaks_module

    source = make_audio(tmp_path / "波形 测试.wav", seconds=1.0)
    result = peaks_module.compute("ffmpeg", source)
    assert result.levels, "decoded no PCM for a Chinese filename"
    assert 900 <= result.duration_ms <= 1100


def test_render_reads_chinese_source(tmp_path: Path) -> None:
    from animatter.core.edl import compile_edl

    source = make_audio(tmp_path / "剪辑 输入.wav", seconds=3.0)
    target = tmp_path / "输出" / "结果 剪辑.m4a"
    target.parent.mkdir(parents=True, exist_ok=True)
    edl = compile_edl(original_duration_ms=3000, deletions=[], silences=[])
    render_module.render(
        "ffmpeg", source=source, edl=edl, target=target, sample_rate=16000, channels=1
    )
    assert target.exists()
    assert target.stat().st_size > 0
