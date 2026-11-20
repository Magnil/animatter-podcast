"""导出并行度：按**当前可用内存**和本片的 chunk 形状决定开几个 ffmpeg（§19.34）。

为什么这是一个纯模块
====================
这个决定只由数字决定——空闲内存、单个 chunk 的峰值、片子分几块——但每一条错法都产出一个
*看着正常*的导出：多开一个 worker 会让机器在 46 % 处 OOM，少开一个只是慢 6 %。两种都不报错，
所以判据必须能单测，而不是埋在 `_encode` 的循环里。

两个杠杆，第 132 轮实测（`docs/video-preview-and-export.md` §19.34）
====================================================================
1. **`-filter_complex_threads` 4 → 12**：逐 chunk 交错 A/B（A,B,B,A）实测纯静帧块 **−15.0 %**、
   4K 块 **−11.0 %**、591 s 单视频块 **−8.6 %**，全片串行 **−6.6 %**，**峰值不变**。这条先做：
   它不复制解码缓冲，而第二个 worker 会把内存翻倍。
2. **第二个 worker**：ft=12 下仍有 1.09–1.23×（两个静帧块 1.23×，「4K 块 + 静帧块」1.09×），
   但并行峰值 3.40 GB、最坏情况是两个 6 段块同时到顶（2 × 2.9 GB）。所以默认 1，用户显式
   打开「加速」才允许 2，且必须通过内存门限。

峰值口径（这条推翻了 D7 的旧结论，见 §19.34.6）
==============================================
逐块实测 14 个 chunk（`-f null`，chunk 8 另用**真写文件**复核）：峰值跟着**段数**走，不跟素材
分辨率走——6 段块 2.54–2.87 GB（纯静帧 2.83、含 1080p 2.64、含 4K 2.84 无差别），1–3 段块
1.18–1.80 GB。「4K 块吃 5.52 GB」是**烘焙 plate 之前**的形状（本轮用旧图复测是 3.58–4.14 GB），
烘焙把每帧 5 次全画布 overlay 削成 1 次之后，4K 不再是一个独立档位。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

#: 单个 chunk 的峰值工作集，按**段数**和**图里有几层全画布 overlay**分档。
#:
#: 全部是第 132 轮逐块实测（14 个 chunk，`-f null`；chunk 8 另用真写文件复核，2.84 vs 2.83 GB）：
#:
#: * 烘焙 plate 之后，6 段块 **2.54–2.87 GB**（纯静帧 2.83、含 1080p 2.64、含 4K 2.84——**没有区别**）
#: * 烘焙 plate 之后，1–3 段块 **1.18–1.80 GB**
#: * 退回旧图（plate 前提不满足，`text` + `layers` 各占一次全画布 overlay），6 段 4K 块
#:   **3.58–4.14 GB** —— 也就是说**峰值几乎全部来自那两次多余的全画布混合**，不是 4K 解码
#:
#: 最后一条很重要：它解释了为什么第 126 轮报的 5.52 GB 复现不出来（那版连静帧预合成都没有），
#: 也说明「按素材分辨率分档」是错的判据。
PEAK_FULL_GB: Final[float] = 2.9
PEAK_SMALL_GB: Final[float] = 1.9
#: 未烘焙时同一块的上界。整片 14 块里最胖的一块（6 段 + 两个 4K 源）实测 4.14 GB。
PEAK_FULL_UNBAKED_GB: Final[float] = 4.2

#: 段数到这个值以上就按「满块」算。
FULL_CHUNK_SEGMENTS: Final[int] = 4

#: 系统 + uvicorn + 浏览器要留的余量。历史上两次 OOM 都是把这块吃掉了。
HEADROOM_GB: Final[float] = 2.5

#: 硬上限的默认值。3 并行在 16 GB 机器上从没成功过（第 121/123 轮各 OOM 一次），所以是 2。
#: 调用方会传 `settings.video_chunk_workers_max`，这里只是「没有配置时的答案」。
DEFAULT_MAX_WORKERS: Final[int] = 2


@dataclass(frozen=True)
class WorkerPlan:
    """开几个 worker，以及**为什么**——后者要显示在导出面板上（D6 的原话要求）。"""

    workers: int
    #: 「本片 14 块，1 个并行（空闲 4.2 GB < 需要的 8.3 GB）」。
    reason: str
    #: 本次估算的单块峰值，GB。
    peak_gb: float
    #: 空闲内存，GB。
    free_gb: float
    #: 本片最大的块有几段。
    max_segments: int


def peak_for(max_segments: int, *, plate_baked: bool = True) -> float:
    """一个 chunk 的估算峰值。

    ``plate_baked=False`` is the fallback graph: the plate preconditions did not hold, so the
    text and the stroke stay as their own full-canvas layers and the peak nearly doubles.
    """
    if max_segments < FULL_CHUNK_SEGMENTS:
        return PEAK_SMALL_GB
    return PEAK_FULL_GB if plate_baked else PEAK_FULL_UNBAKED_GB


def plan_workers(
    *,
    free_gb: float,
    max_segments: int,
    chunks: int,
    allow_parallel: bool,
    plate_baked: bool = True,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> WorkerPlan:
    """The one place that decides ``workers``.

    ``allow_parallel`` is the user's switch (导出菜单的「加速」). Everything else is derived: a
    user who turns it on still gets 1 worker when the machine cannot afford 2, and the reason
    string says which number fell short.
    """
    peak = peak_for(max_segments, plate_baked=plate_baked)
    # Two workers must be able to hold two chunks at their peak *at the same time*: the sampler
    # measured 3.40 GB for a 4K + still pair, but sampling can miss the instant both are at
    # their own maximum, so the budget is 2 x peak rather than the observed sum.
    need_two = HEADROOM_GB + 2 * peak
    hard_cap = max(1, min(max_workers, DEFAULT_MAX_WORKERS)) if max_workers > 0 else 1
    cap = hard_cap if allow_parallel else 1
    # ``inf`` means "the probe failed" (`media.memory.available_gb`). It must not be treated as
    # 0 — that would silently serialise a machine with plenty of room — and it must not go
    # through the arithmetic either: ``inf // peak`` is **nan**, and ``int(nan)`` raises.
    budget = cap if not math.isfinite(free_gb) else int((free_gb - HEADROOM_GB) // peak)
    workers = max(1, min(cap, max(1, budget)))

    if chunks <= 1:
        return WorkerPlan(
            workers=1,
            reason="只有一块，不需要并行",
            peak_gb=peak,
            free_gb=free_gb,
            max_segments=max_segments,
        )
    if not allow_parallel:
        return WorkerPlan(
            workers=1,
            reason=(
                f"{chunks} 块串行编码（导出菜单可打开「加速」；"
                f"2 个并行需要空闲 {need_two:.1f} GB）"
            ),
            peak_gb=peak,
            free_gb=free_gb,
            max_segments=max_segments,
        )
    if workers < 2:
        return WorkerPlan(
            workers=1,
            reason=(
                f"{chunks} 块串行编码：空闲 {free_gb:.1f} GB < 2 个并行需要的 {need_two:.1f} GB"
                f"（单块峰值 {peak:.1f} GB + 余量 {HEADROOM_GB:.1f} GB）"
            ),
            peak_gb=peak,
            free_gb=free_gb,
            max_segments=max_segments,
        )
    return WorkerPlan(
        workers=workers,
        reason=f"{chunks} 块，{workers} 个并行（空闲 {free_gb:.1f} GB，单块峰值 {peak:.1f} GB）",
        peak_gb=peak,
        free_gb=free_gb,
        max_segments=max_segments,
    )
