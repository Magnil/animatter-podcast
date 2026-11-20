"""P1 一致性验收：DOM 舞台截图 vs `preview_still`，按用户 2026-11-19 拍板的口径判。

**为什么不能用「逐像素差异 > 2 % = 0」。** 舞台由 Chrome 合成，静帧由 ffmpeg 合成，两边
对同一张源图用的是**不同的缩放器**：Chrome 的 `object-fit` 与 swscale。同一张源帧、同样
1440×810 的输出，两者本身就不逐像素相同——实测 1.19× 放大差 **4.97 %**、1.69× 差 0.01 %。
把 raw 当判据，会在 1.19× 的素材上永远失败，且与图层对不对**无关**。

**三条一起看**：

1. **平坦区差 ≤ 2 %**（主判据）。平坦像素 = 3×3 邻域亮度极差 ≤ 4，边缘像素不计入分母。
   图层顺序错、fit 用错、选错帧都会改变平坦区；缩放器相位不会。
2. **全局位移 ≤ 0.5 px**（补 flat 的盲区）。flat 对「高对比元素整体偏 1 px」不敏感——损伤
   全落在边缘像素带上。但位移**必须量到亚像素**：实测 1 190 970 的真实偏移是 **+0.31 px**
   （变形像素素材 852×480 + SAR 640:639，Chrome 把 853.33 取整成 853，于是画 1439.44 px 宽
   而不是 1440），整数搜索只能在 0（16.96 %）和 1（12.19 %）之间选，选 1 就会被误判成
   「差一像素」。真位移会把差异打到接近 0；这里最好也只改善 28 %。
3. **raw 照实记录**，并注明该 fit 的缩放器下限。

**实测 20/20 PASS**。最大的残差是 1 473 980 ms 的 ÷16 = 4.75 %（平坦区 0.01 %）——按下面的口径通过，
但它经得起分辨率下降，说明不是纯相位差；同一段素材在相邻时刻只有 0.00–1.68 %，所以不是整段的
问题。**不要再把它当新 bug 重查**（§19.31.5）。

前置：先跑
`node scripts/cdp.mjs http://localhost:5173/ .tmp/p1/consistency/frame.png scripts/check-p1-consistency.mjs`
生成 `.tmp/p1/consistency/frames.json` 与 10 张 1920×1080 舞台截图。

用法（后端可以是任意已启动实例，但样例项目必须存在）：

    .venv\\Scripts\\python.exe scripts\\verify-p1-consistency.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / ".tmp/p1/consistency"

#: A channel difference above this (of 255) counts the pixel as different.
THRESHOLD = 24
#: Local contrast at or below this is "flat": not part of an edge.
FLAT_GRADIENT = 4
#: The main gate.
FLAT_GATE = 0.02
#: The displacement gate, in pixels. Sub-pixel, because the real residual at 1 190 970 is +0.26.
SHIFT_GATE_PX = 0.5

PROJECT_VERSION = "ver_1m6v2q5javqm1j82xsg"


def flat_mask(luminance: np.ndarray) -> np.ndarray:
    """Pixels whose 3×3 neighbourhood is nearly constant — i.e. not part of an edge."""
    image = Image.fromarray(luminance.astype(np.uint8))
    high = np.asarray(image.filter(ImageFilter.MaxFilter(3)), dtype=int)
    low = np.asarray(image.filter(ImageFilter.MinFilter(3)), dtype=int)
    return (high - low) <= FLAT_GRADIENT


def phase_shift(left: np.ndarray, right: np.ndarray) -> tuple[float, float]:
    """Sub-pixel global translation between two greyscale images, via phase correlation.

    A pure translation puts a sharp peak at exactly the shift. A *scale* difference — what a
    scaler mismatch produces — is not a translation at all, so its peak sits near 0 with a broad,
    weak maximum. The peak is refined by a parabola through its neighbours (standard sub-pixel
    refinement; ~0.05 px on this data).
    """
    window = np.outer(np.hanning(left.shape[0]), np.hanning(left.shape[1]))
    a = np.fft.rfft2((left - left.mean()) * window)
    b = np.fft.rfft2((right - right.mean()) * window)
    cross = b * np.conj(a)
    magnitude = np.abs(cross)
    magnitude[magnitude < 1e-9] = 1e-9
    correlation = np.fft.irfft2(cross / magnitude, s=left.shape)
    peak = np.unravel_index(int(np.argmax(correlation)), correlation.shape)

    def refine(centre: int, size: int, values: np.ndarray) -> float:
        before = values[(centre - 1) % size]
        at = values[centre]
        after = values[(centre + 1) % size]
        denominator = before - 2 * at + after
        if abs(denominator) < 1e-12:
            return float(centre)
        offset = 0.5 * (before - after) / denominator
        refined = centre + offset
        # A peak at index 0 refined towards -0.3 is really size - 0.3.
        return refined if refined < size / 2 else refined - size

    dx = refine(peak[1], correlation.shape[1], correlation[peak[0], :])
    dy = refine(peak[0], correlation.shape[0], correlation[:, peak[1]])
    return dx, dy


def main() -> None:
    sys.path.insert(0, str(ROOT / "apps/api/src"))
    from animatter.config import Settings
    from animatter.db import Database
    from animatter.models import TranscriptVersion
    from animatter.services import video_plan, video_render

    if not (CAPTURE / "frames.json").is_file():
        print(f"先跑采集脚本，{CAPTURE / 'frames.json'} 不存在")
        print(
            "node scripts/cdp.mjs http://localhost:5173/ .tmp/p1/consistency/frame.png "
            "scripts/check-p1-consistency.mjs"
        )
        raise SystemExit(2)

    settings = Settings()
    db = Database(settings)
    manifest = json.loads((CAPTURE / "frames.json").read_text(encoding="utf-8"))
    failures: list[str] = []

    print(
        f"{'时刻':>9} {'fit':<11} {'raw':>7} {'平坦区':>7} {'边缘':>7} "
        f"{'÷16':>6} {'位移px':>13} {'判据':>5}"
    )
    with db.session() as session:
        version = session.get(TranscriptVersion, PROJECT_VERSION)
        if version is None:
            print(f"样例项目版本 {PROJECT_VERSION} 不存在")
            raise SystemExit(2)
        plan = video_plan.build(session, settings, version)
        for item in manifest["frames"]:
            at = int(item["atMs"])
            stage_image = Image.open(ROOT / item["file"]).convert("RGB")
            still_image = Image.open(
                video_render.preview_still(
                    session, settings, version.project_id, version.id, at_ms=at
                )
            ).convert("RGB")
            left = np.asarray(stage_image, dtype=int)
            right = np.asarray(still_image, dtype=int)
            differs = np.abs(left - right).max(axis=2) > THRESHOLD
            flat = flat_mask(np.asarray(stage_image.convert("L"), dtype=int))
            flat_rate = float(differs[flat].mean()) if flat.any() else 0.0
            edge_rate = float(differs[~flat].mean()) if (~flat).any() else 0.0
            # ÷16 by box filter: texture decoheres, picture content survives. A residual that
            # survives this is not a resampling artefact.
            size = (stage_image.width // 16, stage_image.height // 16)
            coarse = float(
                (
                    np.abs(
                        np.asarray(stage_image.resize(size, Image.BOX), dtype=int)
                        - np.asarray(still_image.resize(size, Image.BOX), dtype=int)
                    ).max(axis=2)
                    > THRESHOLD
                ).mean()
            )
            dx, dy = phase_shift(
                np.asarray(stage_image.convert("L"), dtype=float),
                np.asarray(still_image.convert("L"), dtype=float),
            )
            segment = next(
                (seg for seg in plan.segments if seg.start_ms <= at < seg.end_ms), None
            )
            ok = flat_rate <= FLAT_GATE and abs(dx) <= SHIFT_GATE_PX and abs(dy) <= SHIFT_GATE_PX
            if not ok:
                failures.append(f"{at} flat={flat_rate:.2%} shift=({dx:+.2f},{dy:+.2f})")
            print(
                f"{at:>9} {(segment.fit if segment else '?'):<11} "
                f"{differs.mean():>6.2%} {flat_rate:>6.2%} {edge_rate:>6.2%} "
                f"{coarse:>5.2%} {f'({dx:+.2f},{dy:+.2f})':>13} {'PASS' if ok else 'FAIL':>5}"
            )

    print()
    print(f"判据：平坦区 ≤ {FLAT_GATE:.0%} 且 全局位移 |dx|,|dy| ≤ {SHIFT_GATE_PX} px")
    if failures:
        print(f"FAIL {len(failures)}/{len(manifest['frames'])}: " + "; ".join(failures))
        raise SystemExit(1)
    print(f"PASS {len(manifest['frames'])}/{len(manifest['frames'])}")


if __name__ == "__main__":
    main()
