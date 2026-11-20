"""导出并行度的决定与 `-refs 1 -bf 0` 的开关（§19.34 / §19.34.5）。

这个模块存在的理由就是「错法看起来都正常」：多开一个 worker 在 46 % 处 OOM，少开一个只是慢
6 %，两种都不报错。所以判据必须能单测——这里逐条钉住门限、硬上限、以及**理由文案里必须出现
那个真正不够的数字**（用户看到的就是那句话）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from animatter.core.video_workers import (
    DEFAULT_MAX_WORKERS,
    FULL_CHUNK_SEGMENTS,
    HEADROOM_GB,
    PEAK_FULL_GB,
    PEAK_FULL_UNBAKED_GB,
    PEAK_SMALL_GB,
    peak_for,
    plan_workers,
)


def _plan(**kwargs: object):
    defaults = {
        "free_gb": 9.0,
        "max_segments": 6,
        "chunks": 14,
        "allow_parallel": True,
    }
    defaults.update(kwargs)
    return plan_workers(**defaults)  # type: ignore[arg-type]


# --- the peak table ----------------------------------------------------


def test_a_full_chunk_costs_the_baked_peak() -> None:
    """6 段块实测 2.54–2.87 GB（14 块逐块量，§19.34）。"""
    assert peak_for(FULL_CHUNK_SEGMENTS) == PEAK_FULL_GB
    assert peak_for(6) == PEAK_FULL_GB


def test_a_short_chunk_costs_less() -> None:
    """1–3 段块实测 1.18–1.80 GB：591 s 的单视频块只有 1.31 GB。"""
    assert peak_for(FULL_CHUNK_SEGMENTS - 1) == PEAK_SMALL_GB
    assert peak_for(1) == PEAK_SMALL_GB


def test_the_fallback_graph_costs_more() -> None:
    """未烘焙时同一块实测 3.58–4.14 GB：多出来的不是 4K 解码，是两次全画布混合。

    这条同时是「为什么不能按素材分辨率分档」的证据：烘焙之后 4K 块（2.84 GB）和纯静帧块
    （2.83 GB）**没有区别**。
    """
    assert peak_for(6, plate_baked=False) == PEAK_FULL_UNBAKED_GB
    assert PEAK_FULL_UNBAKED_GB > PEAK_FULL_GB


# --- the decision ------------------------------------------------------


def test_parallel_is_off_by_default() -> None:
    """默认 1 个 worker，哪怕机器空得很。D6 的原话是「默认保守」。"""
    plan = _plan(allow_parallel=False)
    assert plan.workers == 1


def test_two_workers_need_room_for_two_peaks_plus_headroom() -> None:
    need = HEADROOM_GB + 2 * PEAK_FULL_GB
    assert _plan(free_gb=need + 0.1).workers == 2
    short = _plan(free_gb=need - 0.1)
    assert short.workers == 1
    # The reason must name the number that fell short — that sentence is what the user reads.
    assert f"{need:.1f}" in short.reason


def test_a_short_chunk_brings_the_threshold_down() -> None:
    """只有短块时门限更低（单块 1.9 GB 而不是 2.9 GB）。"""
    need_short = HEADROOM_GB + 2 * PEAK_SMALL_GB
    need_full = HEADROOM_GB + 2 * PEAK_FULL_GB
    assert _plan(free_gb=need_short + 0.1, max_segments=2).workers == 2
    assert _plan(free_gb=need_short + 0.1, max_segments=6).workers == 1
    assert need_short < need_full


def test_the_fallback_graph_raises_the_threshold() -> None:
    need_baked = HEADROOM_GB + 2 * PEAK_FULL_GB
    assert _plan(free_gb=need_baked + 0.1, plate_baked=True).workers == 2
    assert _plan(free_gb=need_baked + 0.1, plate_baked=False).workers == 1


def test_a_single_chunk_never_parallelises() -> None:
    assert _plan(chunks=1, free_gb=64.0).workers == 1


def test_the_hard_cap_holds_even_with_64_gb_free() -> None:
    """3 并行在这台机器上从没成功过（第 121/123 轮各 OOM 一次）。"""
    assert _plan(free_gb=64.0).workers == DEFAULT_MAX_WORKERS
    assert DEFAULT_MAX_WORKERS == 2


def test_the_operator_can_force_serial() -> None:
    plan = _plan(free_gb=64.0, max_workers=1)
    assert plan.workers == 1
    assert plan.reason


def test_an_unreadable_free_memory_figure_does_not_silently_serialise() -> None:
    """``memory.available_gb()`` returns ``inf`` when the probe fails — not 0.

    A 0 would force 1 worker on a machine with plenty of room, and nothing would ever say so.
    Here ``inf`` must reach the hard cap instead.
    """
    assert _plan(free_gb=float("inf")).workers == DEFAULT_MAX_WORKERS


@pytest.mark.parametrize("free", [0.0, 1.0, 2.0, 3.0])
def test_no_memory_never_yields_zero_workers(free: float) -> None:
    """0 would make ``ThreadPoolExecutor`` raise; one is always the floor."""
    assert _plan(free_gb=free).workers == 1


# --- the sentence the user reads --------------------------------------


def test_every_reason_is_a_sentence_with_the_number_that_decided_it() -> None:
    cases = [
        _plan(allow_parallel=False),
        _plan(free_gb=4.0),
        _plan(free_gb=9.0),
        _plan(chunks=1),
        _plan(free_gb=4.0, allow_parallel=False),
    ]
    for plan in cases:
        assert plan.reason.strip()
        assert not plan.reason.startswith(" ")
        assert "块" in plan.reason or "并行" in plan.reason


def test_the_serial_reason_mentions_the_switch() -> None:
    """The user must be able to find the thing that would make it faster."""
    assert "加速" in _plan(allow_parallel=False).reason


# --- the wiring -------------------------------------------------------

def test_the_plan_is_reached_from_the_export_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`core.video_workers` is only useful if `_render_picture` actually asks it.

    This repository has been bitten by "the function is right and nobody calls it" before
    (P2's degrade poll was locked inside a `useMemo`), so the call site is pinned: the plan is
    computed **after** the plate is baked — it needs to know whether the cheap graph was used —
    and its reason is handed to the `announce` callback that writes the render row.
    """
    import inspect

    from animatter.services import video_render as vr

    source = inspect.getsource(vr._render_picture)
    assert "plan_workers(" in source
    assert "video.workers" in source
    # The plate decides the peak tier, so the order matters and is not an accident.
    assert source.index("plate = _plate_for(") < source.index("plan_workers(")
    assert "announce(worker_plan.reason)" in source


def test_the_reason_reaches_the_render_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """`announce` must write `render.note`, not just log the sentence (§19.34)."""
    import inspect

    from animatter.services import video_render as vr

    source = inspect.getsource(vr._encode)
    assert "def announce(" in source
    assert "row.note = note" in source
    assert "announce=announce" in source


# --- the encoder switch ------------------------------------------------


def test_fast_encode_reaches_the_command_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-refs 1 -bf 0` is an option, not a default: it costs 8 % size and is not bit-identical.

    Pins both halves — the flags appear when asked for, and they are absent otherwise. A test
    that only checked the positive case would pass with the flag hard-wired on.
    """
    import animatter.media.video as video_media

    captured: list[list[str]] = []

    def fake_run(command: list[str], *, timeout: int = 7200, cancel: object = None) -> None:
        captured.append(list(command))
        Path(command[-1]).write_bytes(b"\x00")

    monkeypatch.setattr(video_media, "_run", fake_run)
    background = tmp_path / "bg.png"
    background.write_bytes(b"\x00")

    def render(**kwargs: object) -> list[str]:
        video_media.render(
            "ffmpeg",
            target=tmp_path / "out.mp4",
            duration_ms=1000,
            width=192,
            height=108,
            background=background,
            layers=[],
            **kwargs,  # type: ignore[arg-type]
        )
        return captured[-1]

    plain = render()
    assert "-refs" not in plain and "-bf" not in plain
    fast = render(fast_encode=True)
    assert "-refs" in fast and fast[fast.index("-refs") + 1] == "1"
    assert "-bf" in fast and fast[fast.index("-bf") + 1] == "0"
