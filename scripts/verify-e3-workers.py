"""E2/E3 的常驻验收：导出并行度的**门限表**与**接线**（§19.34、§19.33 E2）。

常驻脚本（和 `verify-p1-consistency.py`、`verify-e1-plate.py` 同一类）：`.tmp/` 会被清空，
验收依据不能只活在临时目录里。

    .venv\\Scripts\\python.exe scripts\\verify-e3-workers.py formula   # 档位表，不跑 ffmpeg（< 1 s）
    .venv\\Scripts\\python.exe scripts\\verify-e3-workers.py wire      # 真开两个进程（约 1 min）

**`wire` 是关键的那一半。** 单测能证明 `plan_workers` 的函数是对的，不能证明它被导出路径调用
——这个仓库被「函数对、没人调」伤过一次（P2 的降级条件锁死在 `useMemo` 里）。所以这里把内存探针
桩成 12 GB、把 chunk 列表收窄到两块，跑真的 `_render_picture`，判据是：

1. `video.workers` 日志里 `workers=2`；
2. `render.note` 拿到一句带「2 个并行」的理由；
3. **成片帧数 = 两块之和**（4 232 = 2 259 + 1 973）——chunk 文件在 concat 之后会按设计删掉，
   所以只能量最终交付物。第一版脚本判据写成「chunk-*.mp4 存在」，跑对了也报 FAIL。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

PROJECT = "prj_1m6v2m8anfpyqq3w8qt"
VERSION = "ver_1m6v2q5javqm1j82xsg"
OUT = ROOT / ".tmp" / "verify-e3"
#: Chunks 3 and 13: 94.1 s / 2 259 frames and 82.2 s / 1 973 frames, both still-only.
WANTED = (3, 13)
FAKE_FREE_GB = 12.0


def _project_path() -> None:
    src = str(ROOT / "apps" / "api" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)


def formula() -> int:
    """Print the decision table. No ffmpeg, no database — pure function."""
    from animatter.core.video_workers import (
        DEFAULT_MAX_WORKERS,
        HEADROOM_GB,
        PEAK_FULL_GB,
        PEAK_FULL_UNBAKED_GB,
        PEAK_SMALL_GB,
        plan_workers,
    )

    print("峰值口径（第 132 轮逐块实测）")
    print(f"  满块（≥4 段）          {PEAK_FULL_GB:.1f} GB   实测 2.54–2.87")
    print(f"  满块但 plate 没烘成     {PEAK_FULL_UNBAKED_GB:.1f} GB   实测 3.58–4.14")
    print(f"  短块（1–3 段）          {PEAK_SMALL_GB:.1f} GB   实测 1.18–1.80")
    print(f"  余量                    {HEADROOM_GB:.1f} GB")
    print(f"  硬上限                  {DEFAULT_MAX_WORKERS}")
    print()

    header = f"{'空闲 GB':>8} | {'满块':>28} | {'短块':>28}"
    print(header)
    print("-" * len(header))
    for free in (2.0, 4.0, 6.0, 7.0, 8.3, 9.0, 12.0, 16.0):
        cells = []
        for segments in (6, 2):
            plan = plan_workers(
                free_gb=free, max_segments=segments, chunks=14, allow_parallel=True
            )
            cells.append(f"{plan.workers} 个  ({plan.peak_gb:.1f} GB/块)")
        print(f"{free:8.1f} | {cells[0]:>28} | {cells[1]:>28}")
    print()
    print("默认（不勾「加速」）恒为 1 个 worker；上表是勾选后的结果。")
    off = plan_workers(free_gb=12.0, max_segments=6, chunks=14, allow_parallel=False)
    print(f"示例理由（未勾选）：{off.reason}")
    on = plan_workers(free_gb=12.0, max_segments=6, chunks=14, allow_parallel=True)
    print(f"示例理由（已勾选）：{on.reason}")
    short = plan_workers(free_gb=5.0, max_segments=6, chunks=14, allow_parallel=True)
    print(f"示例理由（内存不够）：{short.reason}")
    return 0


def wire() -> int:
    """Run the real per-film encoder with two chunks and a stubbed 12 GB of free memory."""
    from animatter.config import Settings
    from animatter.db import Database
    from animatter.media import memory as memory_media
    from animatter.media import video_chunks
    from animatter.services import video_render as vr

    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.mp4"):
        stale.unlink()
    for stale in OUT.glob("*-chunks/*"):
        stale.unlink()

    notes: list[str] = []
    original_available = memory_media.available_gb
    original_split = video_chunks.split

    def fake_available() -> float:
        return FAKE_FREE_GB

    def narrow_split(segments: list[Any], **kwargs: Any) -> list[Any]:
        real = original_split(segments, **kwargs)
        return [real[index] for index in WANTED]

    memory_media.available_gb = fake_available  # type: ignore[assignment]
    vr.video_chunks.split = narrow_split  # type: ignore[assignment]
    target = OUT / "wire.mp4"
    try:
        settings = Settings()
        with Database(settings).session() as session:
            prepared = vr.prepare(session, settings, PROJECT, VERSION)
            plan = vr._box(
                session,
                settings=settings,
                template=prepared.template,
                box_rect=prepared.box_rect,
                items=prepared.items,
                edl=prepared.edl,
            )
            cues = vr._cues_for(session, VERSION, prepared.edl)
            vr._render_picture(
                None,
                settings,
                render_id="verify-workers",
                prepared=prepared,
                plan=plan,
                cues=cues,
                style=vr.ExportOptions().style(),
                burn_subtitles=True,
                work_dir=OUT,
                stem="wire",
                target=target,
                report=lambda _fraction: None,
                announce=notes.append,
                faster=True,
            )
    finally:
        memory_media.available_gb = original_available  # type: ignore[assignment]
        vr.video_chunks.split = original_split  # type: ignore[assignment]

    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of",
            "default=noprint_wrappers=1:nokey=1", str(target),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    frames = int(probe.stdout.strip() or 0)
    expected = 2259 + 1973
    print()
    print("announce:", notes)
    print(f"成片帧数: {frames}（期望 {expected} = 2 259 + 1 973）")
    ok = bool(notes) and "2 个并行" in notes[0] and frames == expected
    print("接线验收：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "formula"
    _project_path()
    if command == "formula":
        return formula()
    if command == "wire":
        return wire()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
