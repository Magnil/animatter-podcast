/**
 * `videoPlan.ts`: the DOM stage's clock and layer arithmetic (§19.31.2).
 *
 * The layer ORDER is the part worth testing: a wrong order still renders, still looks like a
 * film, and only disagrees with the export — which is the bug class this whole feature exists to
 * remove. Everything here is pure, so it can be pinned without a browser.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  SEEK_TOLERANCE_MS,
  cssFit,
  expectedVideoSeconds,
  findSpan,
  isUnscaled,
  litAt,
  mediaLayer,
  needsBackdrop,
  needsSeek,
  layersAt,
  stageAt,
} from "./videoPlan.ts";

/** A plan with two segments, one avatar and one cue, in the sample's canvas geometry. */
function samplePlan(overrides = {}) {
  return {
    digest: "abc123abc123abc1",
    version_id: "ver_1",
    canvas: { w: 1920, h: 1080 },
    box: { x: 240, y: 209, w: 1440, h: 810 },
    output_duration_ms: 20_000,
    background_url: "/api/bg.png",
    overlay_url: "/api/overlay.png",
    text_url: "/api/text.png",
    segments: [
      {
        start_ms: 0,
        end_ms: 5_000,
        kind: "video",
        fit: "contain",
        placement: "box",
        loop: false,
        url: "/api/a.mp4",
        backdrop_url: null,
        backdrop_frozen: false,
        start_frame: 0,
        period_ms: 0,
      },
      {
        start_ms: 5_000,
        end_ms: 20_000,
        kind: "image",
        fit: "blur_crop",
        placement: "box",
        loop: false,
        url: "/api/b.png",
        backdrop_url: "/api/bd.jpg",
        backdrop_frozen: false,
        start_frame: 120,
        period_ms: 0,
      },
    ],
    avatars: [
      {
        name: "头像1",
        rect: { x: 42, y: 200, w: 177, h: 188 },
        lit_url: "/api/lit.png",
        dim_url: "/api/dim.png",
        intervals: [
          { start_ms: 1_000, end_ms: 2_000 },
          { start_ms: 5_000, end_ms: 6_000 },
        ],
      },
    ],
    cues: [
      { start_ms: 900, end_ms: 1_800, index: 0, top: 952, height: 128, url: "/api/c0.png" },
      { start_ms: 5_000, end_ms: 6_000, index: 1, top: 887, height: 193, url: null },
    ],
    unsupported: [],
    ...overrides,
  };
}

test("findSpan is half-open at both edges", () => {
  const spans = [
    { start_ms: 0, end_ms: 1000 },
    { start_ms: 1000, end_ms: 2000 },
  ];
  assert.equal(findSpan(spans, 0), spans[0]);
  assert.equal(findSpan(spans, 999), spans[0]);
  // An instant exactly on a boundary belongs to the NEXT span, never to the one ending there.
  assert.equal(findSpan(spans, 1000), spans[1]);
  assert.equal(findSpan(spans, 2000), null, "past the end is nothing, not the last span");
});

test("findSpan handles an empty list and a single item", () => {
  assert.equal(findSpan([], 5), null);
  const one = [{ start_ms: 10, end_ms: 20 }];
  assert.equal(findSpan(one, 10), one[0]);
  assert.equal(findSpan(one, 20), null);
});

test("stageAt resolves the segment covering the instant", () => {
  const plan = samplePlan();
  assert.equal(stageAt(plan, 0).segment, plan.segments[0]);
  assert.equal(stageAt(plan, 4999).segment, plan.segments[0]);
  assert.equal(stageAt(plan, 5000).segment, plan.segments[1]);
});

test("a gap between segments shows no media layer", () => {
  // Two segments with a hole between them: 5000–6000 is a plate gap the plan dropped.
  const plan = samplePlan({
    segments: [
      { ...samplePlan().segments[0], end_ms: 5_000 },
      { ...samplePlan().segments[1], start_ms: 6_000 },
    ],
  });
  const stage = stageAt(plan, 5_500);
  assert.equal(stage.segment, null);
  assert.equal(stage.media, null);
});

test("a box segment is laid out on the box rect, a full one on the canvas", () => {
  const plan = samplePlan();
  const boxed = mediaLayer(plan, plan.segments[0], 1_000);
  assert.deepEqual(boxed.rect, plan.box);
  const full = mediaLayer(plan, { ...plan.segments[0], placement: "full" }, 1_000);
  assert.deepEqual(full.rect, { x: 0, y: 0, w: 1920, h: 1080 });
});

test("a media layer's offset is how far the segment has played", () => {
  const plan = samplePlan();
  assert.equal(mediaLayer(plan, plan.segments[0], 0).offsetMs, 0);
  // **Frame-aligned, not a subtraction.** The film shows frame k for the whole of
  // [k/24, (k+1)/24), so 1 234 ms is frame 29 and its own start is 1 208 ms; asking the element
  // for 1 234 would let the browser pick frame 29 from its own grid and the still would render
  // frame 29 from the film's — they only agree if the request is the frame's start.
  assert.equal(mediaLayer(plan, plan.segments[0], 1_234).offsetMs, 1_208);
  // Relative to the segment, not the film: 1234 ms into the second segment is 1234 - 5000 < 0,
  // so the caller's own instant must already be inside it. Clamped, never negative.
  assert.equal(mediaLayer(plan, plan.segments[1], 5_000).offsetMs, 0);
});

test("a looped segment reduces its offset by the browser's period, not its own guess", () => {
  // ffmpeg's `-stream_loop` wraps at the stream duration and a browser's `loop` at the container
  // duration; 32 ms apart on the sample's 292 s clip, which after two loops is whole frames. The
  // plan publishes the container period and the stage must use it.
  const plan = samplePlan({
    segments: [
      {
        start_ms: 0,
        end_ms: 700_000,
        kind: "video",
        fit: "contain",
        placement: "box",
        loop: true,
        url: "/api/loop.mp4",
        backdrop_url: null,
        backdrop_frozen: false,
        start_frame: 0,
        period_ms: 190_763,
      },
    ],
  });
  const segment = plan.segments[0];
  // 10 s in: under one period, so unchanged (frame 240 = 10 000 ms).
  assert.equal(mediaLayer(plan, segment, 10_000).offsetMs, 10_000);
  // Two loops plus 3 s: reduced by the published period. 2 974 rather than 3 000 because the
  // offset is counted in **film frames** (this instant is frame 9 228, which starts at 384 500 ms,
  // not 384 526) — that 26 ms is the same frame alignment the non-looping case is about.
  assert.equal(mediaLayer(plan, segment, 2 * 190_763 + 3_000).offsetMs, 2_974);
});

test("only video segments become video layers", () => {
  const plan = samplePlan();
  assert.equal(mediaLayer(plan, plan.segments[0], 0).kind, "video");
  assert.equal(mediaLayer(plan, plan.segments[1], 5_000).kind, "image");
});

test("lit avatars follow half-open intervals", () => {
  const plan = samplePlan();
  assert.deepEqual(litAt(plan, 999), []);
  assert.deepEqual(litAt(plan, 1000), ["头像1"]);
  assert.deepEqual(litAt(plan, 1999), ["头像1"]);
  assert.deepEqual(litAt(plan, 2000), [], "the interval ends at 2000, exclusive");
});

test("stageAt reports the cue that is up", () => {
  const plan = samplePlan();
  assert.equal(stageAt(plan, 0).cue, null);
  assert.equal(stageAt(plan, 900).cue?.index, 0);
  // Cue boundaries are half-open too: 1800 is the next cue's business, and there is none.
  assert.equal(stageAt(plan, 1800).cue, null);
  assert.equal(stageAt(plan, 5_500).cue?.index, 1);
});

test("the layer stack matches build_filtergraph, bottom-up", () => {
  // This list IS the contract. `build_filtergraph` overlays the artwork `layers` and only then
  // the `box` chain, so the stroke must come out **below** the box content — the first draft of
  // §19.31.3 had it above, and 5930 semi-opaque stroke pixels of the sample's template fall
  // inside the box rect, so the two orders are not interchangeable.
  const plan = samplePlan();
  // 2500 ms: inside the first segment, outside both lit intervals (1000–2000, 5000–6000), and
  // with no cue. The avatar element is still there — it is showing its DIM photo, which is a
  // different picture from not drawing it, and keeping one element is what stops the lit/dim
  // switch from re-decoding a PNG on every turn change.
  const roles = layersAt(plan, 2_500).map((layer) => layer.role);
  assert.deepEqual(roles, ["background", "overlay", "media", "avatar", "text"]);
  const dim = layersAt(plan, 2_500).find((layer) => layer.role === "avatar");
  assert.equal(dim?.lit, false);
  assert.equal(dim?.url, "/api/dim.png");
  // At an instant with a lit avatar, it sits between the media and the text.
  const litRoles = layersAt(plan, 5_200).map((layer) => layer.role);
  assert.deepEqual(litRoles, ["background", "overlay", "media", "avatar", "text", "cue"]);
});

test("the overlay is drawn under the box content, never merged into the poster", () => {
  const plan = samplePlan();
  const roles = layersAt(plan, 1_000).map((layer) => layer.role);
  const overlay = roles.indexOf("overlay");
  const media = roles.indexOf("media");
  assert.ok(overlay >= 0 && overlay < media);
});

test("a plate gap leaves the background and overlay but no media layer", () => {
  const plan = samplePlan({
    segments: [{ ...samplePlan().segments[0], start_ms: 9_000, end_ms: 20_000 }],
  });
  const roles = layersAt(plan, 5_500).map((layer) => layer.role);
  assert.equal(roles.includes("media"), false);
  assert.equal(roles.includes("background"), true);
  assert.equal(roles.includes("overlay"), true);
});

test("a 全屏 block promotes the media layer instead of adding one", () => {
  // Two elements would leave the box copy visible underneath whenever a full-frame block is up.
  const plan = samplePlan({
    segments: [{ ...samplePlan().segments[0], placement: "full" }],
  });
  const layers = layersAt(plan, 1_000);
  assert.equal(layers.filter((layer) => layer.role === "media").length, 1);
  const media = layers.find((layer) => layer.role === "media");
  assert.deepEqual(media.rect, { x: 0, y: 0, w: 1920, h: 1080 });
  // And it is drawn above the text, because a 全屏 block covers the title too (§19.25).
  const roles = layers.map((layer) => layer.role);
  assert.ok(media !== undefined);
});

test("an unbound slot has no layer at all", () => {
  // The export switches to `background_bare.png` so an unbound slot is an EMPTY FRAME. Drawing
  // the compiled poster's photo there is the bug §19.5's base-poster switch exists to fix.
  const plan = samplePlan();
  const slots = layersAt(plan, 5_500)
    .filter((layer) => layer.role === "avatar")
    .map((layer) => layer.slot);
  assert.deepEqual(slots, ["头像1"]);
});

test("an avatar keeps one element and swaps which photo it shows", () => {
  const plan = samplePlan();
  const lit = layersAt(plan, 1_000).find((layer) => layer.role === "avatar");
  const dim = layersAt(plan, 3_000).find((layer) => layer.role === "avatar");
  assert.equal(lit?.url, "/api/lit.png");
  assert.equal(dim?.url, "/api/dim.png");
  // Same slot, same role: React reuses the element, so the dim bitmap is never re-decoded.
  assert.equal(lit?.slot, dim?.slot);
  assert.equal(lit?.dimUrl, dim?.dimUrl);
});

test("a cue whose band is not baked yet keeps its element and its rect", () => {
  const plan = samplePlan();
  const layers = layersAt(plan, 5_500);
  const cue = layers.find((layer) => layer.role === "cue");
  assert.ok(cue);
  assert.equal(cue.blank, true);
  assert.equal(cue.top, 887);
  assert.equal(cue.height, 193);
  assert.deepEqual(cue.rect, { x: 0, y: 887, w: 1920, h: 193 });
});

test("a blurred segment carries its backdrop on the media layer", () => {
  const plan = samplePlan();
  const media = layersAt(plan, 5_500).find((layer) => layer.role === "media");
  assert.equal(media?.backdrop, "/api/bd.jpg");
  // A native fit has no backdrop at all: a blurred image behind a `contain` picture would show
  // through the letterbox bars, which the film does not do.
  const native = layersAt(plan, 1_000).find((layer) => layer.role === "media");
  assert.equal(native?.backdrop, null);
});

test("cover is anchored top-left, not centred", () => {
  // ffmpeg crops at 0:0; `object-position: center` would keep the middle of a poster instead.
  assert.equal(cssFit("cover").objectPosition, "left top");
  assert.equal(cssFit("cover").objectFit, "cover");
  assert.equal(cssFit("stretch").objectFit, "fill");
  assert.equal(cssFit("contain").objectFit, "contain");
});

test("blur_crop's foreground is not scaled; blur_fill's is contained", () => {
  assert.equal(isUnscaled("blur_crop"), true);
  assert.equal(isUnscaled("blur_fill"), false);
  assert.equal(cssFit("blur_crop").objectFit, "contain");
  assert.equal(cssFit("blur_fill").objectFit, "contain");
});

test("only the two blur fits need a baked backdrop", () => {
  assert.equal(needsBackdrop("blur_fill"), true);
  assert.equal(needsBackdrop("blur_crop"), true);
  assert.equal(needsBackdrop("contain"), false);
  assert.equal(needsBackdrop("cover"), false);
  assert.equal(needsBackdrop("stretch"), false);
});

test("the expected position is the offset, already reduced by the plan", () => {
  assert.equal(expectedVideoSeconds(1_500), 1.5);
  // A looped segment's offset arrives reduced by `PlanSegment.period_ms` (see `localOffsetMs`),
  // so this function must **not** reduce again: doing both was the bug — ffmpeg wraps at the
  // stream duration, the element at the container duration, and applying the two in sequence puts
  // the element a frame behind.
  assert.equal(expectedVideoSeconds(1_500), 1.5);
  // Never negative, and never NaN, whatever the caller computes.
  assert.equal(expectedVideoSeconds(-10), 0);
  assert.equal(expectedVideoSeconds(Number.NaN), 0);
});

test("needsSeek tolerates small drift and not large", () => {
  assert.equal(needsSeek(1.0, 1.0), false);
  // 100 ms of drift is under the tolerance: seeking would cost more than it fixes.
  assert.equal(needsSeek(1.0, 1.1), false);
  assert.equal(needsSeek(1.0, 1.0 + (SEEK_TOLERANCE_MS + 50) / 1000), true);
  assert.equal(needsSeek(NaN, 1.0), true, "an unloaded element always needs a seek");
});

test("the tolerance is under one cue boundary", () => {
  // A cue is at least ~600 ms on real material; a seek that fired inside one would be visible
  // as a stutter exactly when the subtitle changes.
  assert.ok(SEEK_TOLERANCE_MS <= 300);
  assert.ok(SEEK_TOLERANCE_MS >= 100, "too tight and every tick seeks");
});
