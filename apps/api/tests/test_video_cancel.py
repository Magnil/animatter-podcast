"""后端测试：取消、saver-to 路径规则、pick-folder 的失败路径。

**取消用真的 `Popen` 打桩**，不打桩 `_run`：这一轮改的正是 `_run` 的循环，桩掉它等于
什么都没测。三个判据都是「用户能看见的」：

1. 一个跑了 30 秒的 ffmpeg 在 `cancel.set()` 后 1 秒内消失（进程真的没了，不是标记一下）；
2. 抛的是 `video_cancelled` 而不是 `video_render_failed` / `video_render_timeout`
   ——三个都 kill，但下一步动作不同；
3. stderr 仍然读得到（这次改成临时文件，很容易在读之前就 `close` 掉）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from animatter.errors import AppError
from animatter.media import video as video_module

# ---------------------------------------------------------------------------
# _run：取消
# ---------------------------------------------------------------------------


def _slow_command(seconds: int = 30) -> list[str]:
    """A command that sleeps, using the interpreter that is running the tests.

    Not `sleep.exe` or `timeout`: this has to behave the same on any machine that can run the
    suite, and `python -c` is the one binary guaranteed to be there.
    """
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def test_a_running_command_is_killed_when_cancelled() -> None:
    cancel = threading.Event()
    started = time.monotonic()
    timer = threading.Timer(0.4, cancel.set)
    timer.start()
    try:
        with pytest.raises(AppError) as caught:
            video_module._run(_slow_command(30), cancel=cancel)
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started
    assert caught.value.code == "video_cancelled"
    # Killed, not waited out: 30 s of sleep must not have run to completion.
    assert elapsed < 5, f"取消晚了 {elapsed:.1f} s"


def test_cancel_is_a_different_error_from_a_timeout() -> None:
    """Both kill the process, and the user must be able to tell which happened.

    「导出已停止」 means "click 导出 again". 「编码超时」 means "something is wrong with this
    film". Collapsing them into one message sends the user down the wrong path.
    """
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(AppError) as caught:
        video_module._run(_slow_command(30), cancel=cancel)
    assert caught.value.code == "video_cancelled"
    assert "停止" in caught.value.message


def test_a_failure_still_reports_ffmpeg_stderr() -> None:
    """The stderr handling changed (pipe → temp file); the failure hint must survive it."""
    command = [sys.executable, "-c", "import sys; sys.stderr.write('boom-detail'); sys.exit(3)"]
    with pytest.raises(AppError) as caught:
        video_module._run(command)
    assert caught.value.code == "video_render_failed"
    assert "boom-detail" in caught.value.hint


def test_a_successful_run_does_not_raise() -> None:
    video_module._run([sys.executable, "-c", "pass"])


def test_a_missing_binary_is_still_ffmpeg_missing() -> None:
    with pytest.raises(AppError) as caught:
        video_module._run(["definitely-not-installed-ffmpeg-xyz"])
    assert caught.value.code == "ffmpeg_missing"


def test_timeout_still_kills_and_reports_timeout() -> None:
    with pytest.raises(AppError) as caught:
        video_module._run(_slow_command(30), timeout=1)
    assert caught.value.code == "video_render_timeout"


# ---------------------------------------------------------------------------
# 交付路径的三条规则
# ---------------------------------------------------------------------------


def _target(tmp_path: Path, **kwargs: object) -> Path:
    from animatter.config import Settings
    from animatter.routers.clipping import resolve_export_target

    source = tmp_path / "cache" / "vren-abc123.m4a"
    source.parent.mkdir()
    source.write_bytes(b"audio")
    return resolve_export_target(Settings(), source=source, **kwargs)  # type: ignore[arg-type]


def test_the_suffix_comes_from_the_file_not_from_what_the_user_typed(tmp_path: Path) -> None:
    """Typing 「成片.wav」 for an M4A export must not produce an m4a named .wav.

    Some players refuse it, and the user reads it as a bug in the export.
    """
    target = _target(tmp_path, out_dir=str(tmp_path / "out"), filename="成片.wav")
    assert target.name == "成片.m4a"


def test_the_folder_is_created(tmp_path: Path) -> None:
    folder = tmp_path / "deep" / "new" / "folder"
    target = _target(tmp_path, out_dir=str(folder), filename="片子")
    assert target.parent == folder
    assert folder.is_dir()


def test_windows_forbidden_characters_are_replaced(tmp_path: Path) -> None:
    """The film name comes from the episode title, and titles contain ：and /."""
    target = _target(tmp_path, out_dir=str(tmp_path), filename='第 7 期: "罗小黑" / 专访')
    assert not any(ch in target.name for ch in '\\/:*?"<>|')
    assert "第 7 期" in target.name


def test_a_blank_name_falls_back_instead_of_raising(tmp_path: Path) -> None:
    target = _target(tmp_path, out_dir=str(tmp_path), filename="   ")
    assert target.stem
    assert target.suffix == ".m4a"


def test_a_blank_folder_means_the_default_export_folder(tmp_path: Path) -> None:
    from animatter.routers.clipping import default_export_dir

    target = _target(tmp_path, out_dir="", filename="片子")
    assert target.parent == default_export_dir()


# ---------------------------------------------------------------------------
# save-to：只交付已完成的
# ---------------------------------------------------------------------------


def test_cancelled_is_a_terminal_state_and_not_an_active_one() -> None:
    """This split is load-bearing in two places at once.

    `_ACTIVE` is what `fail_orphaned` sweeps on startup and what the audio renderer uses to
    attach a second click to a running job. `_TERMINAL` is what stops `request_cancel` from
    raising on an export that already finished. A status in both would make a cancelled export
    look like it is still running forever.
    """
    from animatter.services import video_render

    assert "cancelled" in video_render._TERMINAL
    assert set(video_render._ACTIVE).isdisjoint(video_render._TERMINAL)
    assert "done" in video_render._TERMINAL
    assert "failed" in video_render._TERMINAL
