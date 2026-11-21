/**
 * The DOM stage: the real-time preview (§19.31.1–§19.31.3, P1).
 *
 * **What this replaces.** Until round 129 the preview was a still: one HTTP request per playhead
 * stop, each costing 0.63–0.93 s of ffmpeg. Moving the playhead meant waiting for a render.
 * This component draws the same frame from a *declarative description* of the whole film
 * (`videoPlan.ts`), so moving the playhead is a binary search and a few attribute flips.
 *
 * **Why the layer order is not decided here.** `layersAt` returns the stack bottom-up, and this
 * component draws that array in order. The order is `build_filtergraph`'s (background → 描边 →
 * box → avatars → text → 全屏 → subtitles), and putting it in one pure function is what makes it
 * testable — a mis-stack still renders a plausible picture.
 *
 * **Scale, not percentages.** The stage is an inner box of exactly `canvas.w × canvas.h` CSS
 * pixels, scaled with a transform. Percentages would look identical for the full-canvas layers
 * but break the one fit that must NOT be scaled: `blur_crop` draws its foreground at the
 * material's own pixel size (`overlay=(W-w)/2:0` in the chain), so "natural size" has to mean
 * canvas pixels. Inside a scaled canvas box, `width: auto` means exactly that.
 *
 * **Video sync.** A `<video>` layer is not seeked per tick — a seek stalls the decoder, and at
 * 50 ms per tick that is a permanent stutter. Instead: while the audio plays, the element plays
 * and is only corrected when it drifts past `SEEK_TOLERANCE_MS`; while the audio is paused, it is
 * paused and seeked to the exact instant. Measured 144 fps with 0 dropped frames on the sample
 * project, whose stack includes a 4K clip (§19.31.1).
 */

import { Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { VideoPlan } from "./videoPlan";
import {
  SEEK_TOLERANCE_MS,
  cssFit,
  expectedVideoSeconds,
  frameStartMs,
  layersAt,
  needsSeek,
  isUnscaled,
  stageScale,
  type DrawLayer,
} from "./videoPlan";
import {
  deckLayout,
  readExportRunning,
  readMemoryOf,
  underHeapPressure,
} from "./mediaDeck";
import type { DeckLayout, ResidentSlot } from "./mediaDeck";

interface Props {
  plan: VideoPlan;
  /** The instant to draw, on the **output** timeline. */
  atMs: number;
  /** Whether the podcast audio is playing — which decides whether video layers play too. */
  playing: boolean;
  /** Shown over the stage when a layer cannot be decoded (§19.32). */
  onNotice?: (message: string) => void;
}

/**
 * How many neighbouring cue bands to warm ahead of the playhead.
 *
 * A band costs ~77 ms to bake. Warming three on each side means a normal playback never waits for
 * one, while a long drag does not queue thirty bakes for frames the user has already passed.
 */
const CUE_WARM_RADIUS = 3;

/**
 * How often the stage re-checks whether it must degrade (§19.32).
 *
 * An export runs for minutes and heap pressure is not frame-accurate, so a second is plenty; polling
 * per animation frame would spend the CPU this degradation exists to save.
 */
const DEGRADE_POLL_MS = 1_000;

/** What the stage should degrade for, and the numbers that decided it. */
interface StageDegrade {
  reason: "export" | "memory" | null;
  memory: { usedJSHeapSize?: number; jsHeapSizeLimit?: number } | undefined;
}

/**
 * Read the two out-of-React inputs once.
 *
 * `memory` is carried through so the deck judges pressure from **the same reading** the badge was
 * decided on; sampling twice would let the badge say 「内存紧张」 while the deck disagreed.
 */
function readDegrade(): StageDegrade {
  if (readExportRunning()) {
    return { reason: "export", memory: readMemoryOf() };
  }
  const memory = readMemoryOf();
  return { reason: underHeapPressure(memory) ? "memory" : null, memory };
}

export function DomStage({ plan, atMs, playing, onNotice }: Props): React.JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [fitScale, setFitScale] = useState(1);
  /**
   * Whether the stage must degrade (§19.32), **polled**.
   *
   * Both inputs are outside React: an export flag set by a menu in another subtree, and
   * `performance.memory`. Neither fires an event, so they are sampled once a second into state —
   * and state is the point. The first version read them inside the deck's `useMemo`, whose deps
   * were `[plan, frameMs, …]`; while the audio is paused `frameMs` never changes, so the value was
   * latched forever and the degradation never appeared at all. Measured by
   * `scripts/check-p2-degrade.mjs`: badge null, stage 584 px instead of 444 px, on a project whose
   * heap was faked full.
   *
   * One second is the right period for both: an export runs for minutes, and heap pressure is not
   * a frame-accurate signal. Sampling per animation frame would only burn CPU in the thing that is
   * supposed to be saving it.
   */
  const [degrade, setDegrade] = useState<StageDegrade>(() => readDegrade());
  useEffect(() => {
    const id = window.setInterval(() => setDegrade(readDegrade()), DEGRADE_POLL_MS);
    return () => window.clearInterval(id);
  }, []);
  const exportRunning = degrade.reason === "export";

  // Fit the canvas into the panel. `ResizeObserver` rather than a window listener: the panel is
  // inside a collapsible section and its width changes without the window doing anything.
  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const fit = (): void => {
      const width = host.clientWidth;
      if (width > 0) setFitScale(width / plan.canvas.w);
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(host);
    return () => observer.disconnect();
  }, [plan.canvas.w]);

  // Evaluate the plan at the **film frame's** start, not at the raw playhead. See `frameStartMs`:
  // a cue that begins 27 ms into a frame is shown at the next frame, and the still draws it that
  // way too — evaluating at the raw instant made the two halves of the preview disagree about
  // which cue is up, which the consistency check scored as a 6.5 % layer mismatch.
  const frameMs = frameStartMs(atMs);
  const layers = useMemo(() => layersAt(plan, frameMs), [plan, frameMs]);

  /**
   * The two media slots (§19.32).
   *
   * Computed here, once per rendered frame, and threaded into the layer stack at the media
   * position. The two elements are **always** mounted while the plan has any video — that is what
   * makes the prewarm survive the image segments between two videos, which on the sample plan is
   * most of the cuts (51 of 64 segments are images).
   */
  const deck = useMemo(
    () =>
      deckLayout(plan.segments, { canvas: plan.canvas, box: plan.box }, frameMs, {
        exportRunning,
        // Passed from the polled state rather than read here: reading it in the memo latched the
        // value until the playhead moved, which is exactly when it must not be stale.
        memory: degrade.memory,
      }),
    [plan, frameMs, exportRunning, degrade],
  );
  // Where the box content belongs: after the poster, the 底板 and the 描边, before
  // avatars/text/全屏/subtitles — the same boundary `build_filtergraph` uses.
  const ARTWORK_ROLES: ReadonlySet<string> = new Set(["background", "plate", "stroke"]);
  const insertionIndex = layers.findIndex((layer) => !ARTWORK_ROLES.has(layer.role));
  /**
   * The scale to draw at.
   *
   * §19.32's export row: while a 14-minute x264 encode runs, the preview must not compete for CPU,
   * and the cheapest honest degradation is to composite fewer pixels — 444 px tall instead of
   * whatever the panel fits. The layout, the cue and the segment stay correct; the picture is just
   * smaller. `stageScale` never enlarges, so a small panel keeps its own scale.
   */
  const scale = stageScale(fitScale, plan.canvas.h, deck.degraded);
  // A plan with no video at all (a slideshow) has nothing to prewarm and nothing to switch.
  const hasVideo = useMemo(
    () => plan.segments.some((segment) => segment.kind === "video"),
    [plan.segments],
  );

  // Warm the cue bands around the playhead. The band's own <img> would fetch it anyway; doing it
  // here means the fetch starts before the layer is mounted, so the common case never shows an
  // empty band.
  const cueIndex = layers.find((layer) => layer.role === "cue")?.index;
  useEffect(() => {
    for (let offset = -CUE_WARM_RADIUS; offset <= CUE_WARM_RADIUS; offset += 1) {
      const target = plan.cues[(cueIndex ?? -1) + offset];
      if (target?.url) warm(target.url);
    }
  }, [plan, cueIndex]);

  return (
    <div className="dom-stage-host" ref={hostRef}>
      {/*
        §19.32 wants the degradation **visible**, not silent: a preview that quietly became
        half-resolution, or that quietly lost its prewarm, is a bug report waiting to happen. The
        badge sits in host coordinates (unscaled), so it stays legible at 444 px.
      */}
      {deck.degraded ? (
        <div className="stage-degraded" role="status">
          {deck.reason === "export" ? "导出中：预览降级" : "内存紧张：预览降级"}
        </div>
      ) : null}
      <div
        className="dom-stage"
        style={{
          width: plan.canvas.w,
          height: plan.canvas.h,
          transform: `scale(${scale})`,
        }}
        /*
          Rounded, not raw. `atMs` comes from `audio.currentTime * 1000`, so it is a float with
          binary noise (257350.00000000003); the plan is an integer-millisecond structure and the
          consistency check compares against `preview_still`, whose `at_ms` is an integer. A
          float here would make the two halves of the check look at instants 0.00000003 apart —
          harmless today, but it would also put a non-integer in the URL of any future
          instant-keyed request.
        */
        data-at-ms={Math.round(atMs)}
        data-frame-ms={Math.round(frameMs)}
      >
        {/*
          The deck is drawn **at the media layer's position in the stack**, not before it.

          This is not cosmetic: every layer here is `position: absolute` with no `z-index`, so a
          later sibling paints on top. Rendering the deck first put it *under* the full-canvas
          background, which measured as 20/20 failures of the consistency gate — a completely
          different picture, with zero pixel shift, which is exactly what "wrong layer order" looks
          like in that report.

          It is emitted even when the current segment is an image: unmounting the two elements
          between videos would make every cut back into a video pay the 116 ms decode again.
        */}
        {layers.map((layer, index) =>
          layer.role === "media" ? (
            /*
              The media position holds **two** things, in this order: the deck (which owns the
              video elements) and, when the current segment is a still, the still itself.

              Drawing only the deck here is the bug this shape fixes: 51 of the sample's 64 segments
              are images, so an image segment would show whatever video the deck last held — measured
              as 11 of 20 consistency failures with **zero** pixel shift, i.e. right geometry, wrong
              picture.
            */
            <Fragment key="media">
              <MediaDeckView
                deck={deck}
                layer={layer.kind === "video" ? layer : null}
                playing={playing}
                onNotice={onNotice}
              />
              {layer.kind === "image" ? <StillForeground layer={layer} /> : null}
            </Fragment>
          ) : (
            <Fragment key={layerKey(layer)}>
              {/*
                No media layer at all (a plate gap): the deck still has to exist, in the same stack
                position, or the elements holding the neighbouring videos unmount.
              */}
              {hasVideo && index === insertionIndex ? (
                <MediaDeckView deck={deck} layer={null} playing={playing} onNotice={onNotice} />
              ) : null}
              <StageLayerView layer={layer} />
            </Fragment>
          ),
        )}
      </div>
    </div>
  );
}

/**
 * A stable key per layer **role**, not per URL.
 *
 * Reusing the element across a `src` change is the point: an `<img>` that unmounts and remounts
 * re-decodes its PNG, and a video element that remounts reloads its bytes. Only the media layer
 * legitimately needs a new element when the segment changes, and even then the Vite/React key has
 * to include the segment so React does not hand the new source to the old element mid-seek.
 */
function layerKey(layer: DrawLayer): string {
  // **Stable, and that is the feature.** A key carrying the URL unmounts the element at every
  // block boundary, and a freshly mounted `<video>` needs **116 ms** to present a frame
  // (`scripts/check-p2-switch.mjs`, measured per animation frame) — during which `.layer-media`'s
  // black background is what the viewer sees. Keeping the container lets `VideoDeck` flip between
  // two already-decoded elements instead.
  if (layer.role === "media") return "media";
  if (layer.role === "avatar") return `avatar-${layer.slot}`;
  if (layer.role === "cue") return "cue";
  return layer.role;
}

function StageLayerView({ layer }: { layer: DrawLayer }): React.JSX.Element {
  const style: React.CSSProperties = {
    left: layer.rect.x,
    top: layer.rect.y,
    width: layer.rect.w,
    height: layer.rect.h,
  };

  if (layer.role === "cue") {
    if (layer.blank || !layer.url) {
      // Kept in the DOM with its real rect so nothing shifts when the bytes arrive.
      return <div className="layer-cue is-blank" style={style} aria-hidden="true" />;
    }
    return (
      <img
        className="layer-cue"
        style={style}
        src={layer.url}
        alt=""
        aria-hidden="true"
        decoding="async"
      />
    );
  }

  return (
    <img
      className={`layer layer-${layer.role}`}
      style={style}
      src={layer.url}
      alt=""
      aria-hidden="true"
      decoding="async"
    />
  );
}

/**
 * The two media slots, so a block switch costs no decode (§19.32, P2).
 *
 * Both slots are always mounted while the plan has any video. The hidden one is `visibility:
 * hidden`, **not** `display: none` and not unmounted: an unmounted `<video>` loses its decoded
 * frames and its buffers, which is exactly the 116 ms this feature removes. `visibility: hidden`
 * still composites pixels the same way, so a flip is a paint, not a load.
 *
 * The offset is passed in per render rather than stored in the slot: it changes twenty times a
 * second, and a slot that carried it would invalidate the memo on every tick.
 */
function MediaDeckView({
  deck,
  layer,
  playing,
  onNotice,
}: {
  deck: DeckLayout;
  layer: DrawLayer | null;
  playing: boolean;
  onNotice?: (message: string) => void;
}): React.JSX.Element {
  const offsetMs = layer?.offsetMs ?? 0;
  // The container sits where the box is whenever a video is on screen; otherwise it collapses to
  // the box rect anyway so the two elements do not reflow between an image and a video.
  const rect = layer?.rect ?? deck.slots.find((slot) => slot !== null)?.rect ?? { x: 0, y: 0, w: 0, h: 0 };
  return (
    <div
      className="layer-deck"
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
      data-deck-front={deck.front === null ? "none" : String(deck.front)}
      data-deck-warmed={deck.warmed === null ? "none" : String(deck.warmed)}
      data-deck-degraded={deck.degraded ? deck.reason ?? "true" : "false"}
    >
      {deck.slots.map((slot, index) => {
        if (slot === null) return null;
        const visible = deck.front === index && layer !== null;
        return (
          <DeckSlotView
            key={slot.ordinal}
            slot={slot}
            visible={visible}
            offsetMs={offsetMs}
            playing={playing && visible}
            onNotice={onNotice}
          />
        );
      })}
    </div>
  );
}

/**
 * A still inside the box: the `object-fit` half of the media position.
 *
 * Not `StageLayerView`: that one only places a rect, which is correct for an avatar (stretched) and
 * for a cue band (its own PNG geometry) but wrong here — a `contain` image stretched to fill the box
 * measures as a completely different picture, which is how this was caught (11 of 20 consistency
 * failures with zero pixel shift: right geometry, wrong fit).
 */
function StillForeground({ layer }: { layer: DrawLayer }): React.JSX.Element {
  const fit = cssFit(layer.fit);
  const unscaled = isUnscaled(layer.fit);
  const style: React.CSSProperties = {
    objectFit: fit.objectFit,
    objectPosition: fit.objectPosition,
    // `blur_crop` keeps the material's own pixels: the chain overlays them at 1:1 and lets the box
    // clip, so `width: auto` inside the scaled canvas box is exactly right.
    width: unscaled ? "auto" : "100%",
    height: unscaled ? "auto" : "100%",
    maxWidth: unscaled ? "none" : "100%",
    left: unscaled ? "50%" : 0,
    transform: unscaled ? "translateX(-50%)" : undefined,
  };
  return (
    <div
      className="layer-media"
      style={{
        left: layer.rect.x,
        top: layer.rect.y,
        width: layer.rect.w,
        height: layer.rect.h,
      }}
    >
      {layer.backdrop ? (
        <img className="layer-backdrop" src={layer.backdrop} alt="" aria-hidden="true" />
      ) : null}
      <img className="layer-foreground" style={style} src={layer.url} alt="" decoding="async" />
    </div>
  );
}

/** One slot: the baked backdrop (if any) plus the synced picture. */
function DeckSlotView({
  slot,
  visible,
  offsetMs,
  playing,
  onNotice,
}: {
  slot: ResidentSlot;
  visible: boolean;
  offsetMs: number;
  playing: boolean;
  onNotice?: (message: string) => void;
}): React.JSX.Element {
  const fit = cssFit(slot.fit);
  const unscaled = isUnscaled(slot.fit);
  const style: React.CSSProperties = {
    objectFit: fit.objectFit,
    objectPosition: fit.objectPosition,
    // `blur_crop` keeps the material's own pixels: it is the strategy for screenshots that must
    // stay sharp, so blowing them up to the box would defeat it — the chain does not scale them
    // either, it overlays them at 1:1 and lets the box clip.
    width: unscaled ? "auto" : "100%",
    height: unscaled ? "auto" : "100%",
    maxWidth: unscaled ? "none" : "100%",
    // Centred horizontally, top-aligned: `overlay=(W-w)/2:0`.
    left: unscaled ? "50%" : 0,
    transform: unscaled ? "translateX(-50%)" : undefined,
  };
  /**
   * Whether this element failed to decode (§19.32's first row).
   *
   * A codec the browser cannot decode is not a transient failure, so the element is replaced by a
   * still and not retried: retrying would re-fetch the same bytes and produce the same error, and
   * meanwhile the box would flash black on every cut into that segment.
   */
  const [failed, setFailed] = useState(false);
  /**
   * The last successfully decoded frame, as a data URL — the still that stands in for a segment the
   * browser will not decode. Captured from a canvas while the element *was* working, so a segment
   * that fails after a few frames still shows something recognisable rather than nothing.
   */
  const [poster, setPoster] = useState<string | null>(null);

  if (failed) {
    return (
      <div className={`layer-media${visible ? "" : " is-standby"}`} aria-hidden={visible ? undefined : true}>
        {slot.backdrop ? (
          <img className="layer-backdrop" src={slot.backdrop} alt="" aria-hidden="true" />
        ) : null}
        {poster !== null ? (
          <img className="layer-foreground" style={style} src={poster} alt="" />
        ) : (
          // Nothing was ever decoded: an explicit placeholder, not a black box, so the user can
          // tell "this clip cannot be previewed" from "the preview is broken".
          <div className="layer-undecodable" style={style} aria-hidden="true">
            无法实时预览
          </div>
        )}
      </div>
    );
  }

  return (
    <div className={`layer-media${visible ? "" : " is-standby"}`} aria-hidden={visible ? undefined : true}>
      {slot.backdrop ? (
        <img className="layer-backdrop" src={slot.backdrop} alt="" aria-hidden="true" />
      ) : null}
      <SyncedVideo
        url={slot.url}
        offsetMs={offsetMs}
        loop={slot.loop}
        playing={playing}
        style={style}
        onNotice={onNotice}
        onFailed={() => {
          setFailed(true);
          onNotice?.("这一段视频实时预览不了（编码不受浏览器支持）。导出仍然是好的。");
        }}
        onPoster={(url) => setPoster(url)}
      />
    </div>
  );
}

/**
 * A video that follows the playhead.
 *
 * Two modes, and conflating them is why naive implementations stutter:
 *
 * * **The audio is playing** → the element plays on its own and is *corrected* only when it is
 *   more than `SEEK_TOLERANCE_MS` off. Seeking every tick would stall the decoder 20 times a
 *   second.
 * * **The audio is paused** → the element pauses and seeks to the exact instant. A paused preview
 *   must show the frame the user is looking at, not the frame it drifted to.
 *
 * A **standby** slot (`playing === false` while the audio runs) is paused at offset 0 and never
 * seeked: it is warm for a boundary that has not arrived, and decoding it would waste the very
 * frames the prewarm is buying.
 */
function SyncedVideo({
  url,
  offsetMs,
  loop,
  playing,
  style,
  onNotice,
  onFailed,
  onPoster,
}: {
  url: string;
  offsetMs: number;
  loop: boolean;
  playing: boolean;
  style: React.CSSProperties;
  onNotice?: (message: string) => void;
  onFailed?: () => void;
  onPoster?: (url: string) => void;
}): React.JSX.Element {
  const ref = useRef<HTMLVideoElement | null>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const expected = expectedVideoSeconds(offsetMs);
    // **Paused means exact; playing means tolerant.** The 200 ms tolerance exists so a running
    // element is not re-seeked on every tick — a seek stalls the decoder. While paused there is
    // only ever one seek, and being 30 ms off there means the still frame the user is comparing
    // against is a *different frame*: measured, a 32 ms drift put the stage 17.6 % away from the
    // film's frame at the same instant, which looks exactly like a broken layer.
    const tolerance = playing ? SEEK_TOLERANCE_MS : 0;
    if (needsSeek(element.currentTime, expected, tolerance)) {
      try {
        element.currentTime = expected;
      } catch {
        // A seek before metadata arrives throws; the loadedmetadata handler below retries.
      }
    }
  }, [offsetMs, loop, url, playing]);

  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (playing) {
      // `play()` can reject for reasons that are not errors — the element was paused by the
      // browser because it left the viewport, or a newer source replaced this one mid-call.
      void element.play().catch(() => undefined);
    } else {
      element.pause();
    }
  }, [playing, url]);

  return (
    <video
      ref={ref}
      className="layer-foreground"
      style={style}
      src={url}
      muted
      playsInline
      loop={loop}
      preload="auto"
      onLoadedMetadata={(event) => {
        const element = event.currentTarget;
        // Exact, not "close enough": assigning `currentTime` makes the browser show the frame
        // whose interval contains it, which is the same rule the film's `fps=24` uses.
        element.currentTime = expectedVideoSeconds(offsetMs);
      }}
      onError={() => {
        // `onFailed` replaces the element with a still (§19.32); the notice is its fallback when
        // the stage was mounted without one.
        if (onFailed) onFailed();
        else onNotice?.("这一段视频实时预览不了（编码不受浏览器支持）。导出仍然是好的。");
      }}
      onLoadedData={(event) => {
        // Capture one frame for the failure path while the element is definitely working. Cheap
        // (a 1920×1080 `drawImage` + `toDataURL` is a few milliseconds) and only ever once per
        // source, so it cannot become the thing that makes the preview stutter.
        if (!onPoster) return;
        const element = event.currentTarget;
        if (element.videoWidth === 0) return;
        try {
          const canvas = document.createElement("canvas");
          canvas.width = Math.min(element.videoWidth, 960);
          canvas.height = Math.round((canvas.width * element.videoHeight) / element.videoWidth);
          const context = canvas.getContext("2d");
          if (context === null) return;
          context.drawImage(element, 0, 0, canvas.width, canvas.height);
          onPoster(canvas.toDataURL("image/jpeg", 0.8));
        } catch {
          // A tainted canvas throws; so does a CORS-less cross-origin source. Both mean "no
          // poster", which the failure path already handles with a labelled placeholder.
        }
      }}
    />
  );
}

/**
 * Already-warm cue bands, so a band that the playhead reaches does not start its fetch then.
 *
 * Module-level and bounded: a Map keeps insertion order, so the oldest key is the first and
 * evicting it is one line. An unbounded cache here would grow to 772 decoded bitmaps over a full
 * playback — the whole reason §19.31.4 rejects pre-baking everything.
 */
const warmBands = new Map<string, HTMLImageElement>();
const WARM_LIMIT = 24;

function warm(url: string): void {
  if (!url || warmBands.has(url)) return;
  const image = new Image();
  image.decoding = "async";
  image.src = url;
  warmBands.set(url, image);
  while (warmBands.size > WARM_LIMIT) {
    const oldest = warmBands.keys().next();
    if (oldest.done) break;
    warmBands.delete(oldest.value);
  }
}
