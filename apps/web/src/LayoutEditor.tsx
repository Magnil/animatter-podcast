/**
 * The canvas editor (§19.38, round 139) — phase three of the template work.
 *
 * **What this adds that `TemplateTexts` could not.** That panel changes what a slot *says*; the
 * layout was the designer's and 「导入模板」 was a promise to keep it. The promise is still true —
 * what changes is that the promise now has an escape hatch. The three things users kept asking
 * for are exactly the three the PSD cannot answer: the media box needs to be 4:3 for one series,
 * 主播1's frame sits too high on this poster, and the title is 40 px wider than the designer
 * left room for.
 *
 * **Why this is a separate tab and not a section.** A drag needs the canvas at a usable size and
 * a list of every slot at the same time; the 模板 tab's column is 320–480 px of form controls and
 * its left pane is the live preview. So the 布局 tab swaps the *left* pane for the canvas and
 * keeps the right column for the readout — one screen, both halves, no modal.
 *
 * **Why it saves on drop and not on every pointermove.** A drag produces 60 positions a second;
 * each would be a PUT, a `video_edits.signature()` change, and a new text PNG. The commit is the
 * gesture boundary — the same reason `TemplateTexts` commits on blur rather than on input.
 *
 * **What the picture is.** The poster's *open* twin plus the plate and the 描边 at their current
 * rects plus each avatar's lit frame: the same stack the preview and the export draw, minus the
 * material inside the box and the subtitles. If the canvas drew a grey placeholder instead, the
 * one thing the user is judging — 「这个标题是不是压到会客厅了」 — would be the one thing it
 * cannot show.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, type VideoLayout, type VideoRect, type VideoTemplate } from "./api";
import {
  MIN_SIZE,
  clampRect,
  describe,
  handlePoints,
  hitHandle,
  isMoved,
  moveRect,
  payloadFor,
  resizeRect,
  scaledFont,
  snapRect,
  toCanvasPoint,
  type Canvas,
  type Handle,
  type Rect,
  type SlotKind,
} from "./layoutMath";

interface Props {
  projectId: string;
  template: VideoTemplate;
  onNotice: (message: string) => void;
  /** A save landed: the panel refetches the plan so the preview agrees with the canvas. */
  onChanged: () => void;
}

type Kind = SlotKind | "box";

interface Target {
  kind: Kind;
  key: string;
}

interface Draft {
  box: Rect;
  texts: Record<string, Rect>;
  avatars: Record<string, Rect>;
}

/** How far from a handle a press still grabs it, in **screen** pixels. */
const HANDLE_GRAB_PX = 9;

export function LayoutEditor({ projectId, template, onNotice, onChanged }: Props): React.JSX.Element {
  const [state, setState] = useState<VideoLayout | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [target, setTarget] = useState<Target | null>(null);
  const [guides, setGuides] = useState<{ axis: "x" | "y"; at: number }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [scale, setScale] = useState(0.5);
  const hostRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.videoLayout(projectId);
      setState(next);
      setDraft(draftOf(next));
      setError(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, [projectId]);

  // Refetched when the template changes under us — a recompile, a different PSD, or a package
  // import all move the compiled geometry, and a draft built against the old one would fight the
  // server on the next drag.
  useEffect(() => {
    void load();
  }, [load, template.compiled_at]);

  const canvas: Canvas = state?.canvas ?? { w: template.canvas_w, h: template.canvas_h };

  // Fit the canvas into the column. `ResizeObserver`, like the preview: the panel is resizable
  // and the window is not the only thing that can change its width.
  //
  // `state` is a dependency, not just `canvas.w`: on the first render the component returns the
  // 「正在读取布局…」 line instead of the canvas, so `hostRef.current` is still null and there is
  // nothing to observe. The canvas width does not change when the fetch lands (the fallback is
  // `template.canvas_w`), so without this the effect never runs again and the stage keeps the
  // initial 0.5 — measured on the sample project, a 1920-wide canvas drawn at 960 px inside a
  // 480 px column, clipped, with every slot's centre outside the visible area. That is why the
  // drag test could not grab the title.
  useEffect(() => {
    const host = hostRef.current;
    if (host === null) return;
    const measure = (): void => {
      const width = host.clientWidth;
      if (width > 0 && canvas.w > 0) setScale(width / canvas.w);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, [canvas.w, state]);

  const drag = useRef<{
    kind: Kind;
    key: string;
    start: Rect;
    startPoint: { x: number; y: number };
    handle: Handle | null;
  } | null>(null);

  const rectOf = useCallback(
    (kind: Kind, key: string): Rect | null => {
      if (draft === null) return null;
      if (kind === "box") return draft.box;
      if (kind === "avatar") return draft.avatars[key] ?? null;
      return draft.texts[key] ?? null;
    },
    [draft],
  );

  /** Slope of the compiled geometry, for the two things that must not change shape. */
  const compiled = useMemo(() => {
    if (state === null) return new Map<string, Rect>();
    const out = new Map<string, Rect>();
    out.set("box", state.compiled_box);
    for (const slot of state.texts) out.set(`text:${slot.key}`, slot);
    for (const slot of state.avatars) out.set(`avatar:${slot.key}`, slot);
    return out;
  }, [state]);

  const commit = useCallback(
    async (kind: Kind, key: string, rect: Rect, previous: Rect) => {
      if (!isMoved(rect, previous)) return;
      setSaving(true);
      try {
        const next = await api.updateVideoLayout(projectId, payloadFor(kind, key, rect));
        setState(next);
        setDraft(draftOf(next));
        onChanged();
      } catch (caught) {
        // The canvas must go back to what the server believes, or the next drag starts from a
        // position nothing else agrees with.
        setError(caught instanceof Error ? caught.message : String(caught));
        void load();
      } finally {
        setSaving(false);
      }
    },
    [projectId, onChanged, load],
  );

  const pointToCanvas = useCallback(
    (event: { clientX: number; clientY: number }): { x: number; y: number } => {
      const host = stageRef.current;
      if (host === null) return { x: 0, y: 0 };
      const box = host.getBoundingClientRect();
      return toCanvasPoint(
        { x: event.clientX, y: event.clientY },
        { left: box.left, top: box.top },
        scale,
      );
    },
    [scale],
  );

  const stageRef = useRef<HTMLDivElement | null>(null);

  const onPointerDown = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      if (draft === null || saving) return;
      const point = pointToCanvas(event);
      const tolerance = HANDLE_GRAB_PX / Math.max(scale, 0.01);

      // A handle on the selected slot wins over everything: it is drawn on top, it is small, and
      // missing it means the user resizes the whole box instead of one corner.
      if (target !== null) {
        const rect = rectOf(target.kind, target.key);
        if (rect !== null) {
          const handle = hitHandle(rect, point, tolerance);
          if (handle !== null) {
            drag.current = { ...target, start: rect, startPoint: point, handle };
            event.currentTarget.setPointerCapture(event.pointerId);
            event.preventDefault();
            return;
          }
        }
      }

      // Topmost first. The text slots are drawn over the avatars and both over the box; picking
      // the first hit in that order is what makes 「点标题就选中标题」 work even where they overlap.
      const hit = hitTest(draft, state, point);
      setTarget(hit === null ? null : { kind: hit.kind, key: hit.key });
      if (hit !== null) {
        drag.current = {
          kind: hit.kind,
          key: hit.key,
          start: hit.rect,
          startPoint: point,
          handle: null,
        };
        event.currentTarget.setPointerCapture(event.pointerId);
        event.preventDefault();
      }
    },
    [draft, state, target, rectOf, pointToCanvas, scale, saving],
  );

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const active = drag.current;
      if (active === null || draft === null) return;
      const point = pointToCanvas(event);
      const dx = point.x - active.startPoint.x;
      const dy = point.y - active.startPoint.y;
      const min =
        active.kind === "box" ? MIN_SIZE.box : active.kind === "avatar" ? MIN_SIZE.avatar : MIN_SIZE.text;
      let rect =
        active.handle === null
          ? moveRect(active.start, dx, dy, canvas, min, { even: active.kind === "box" })
          : resizeRect(active.start, active.handle, dx, dy, canvas, min, {
              even: active.kind === "box",
              aspect: active.kind === "avatar",
            });
      if (active.handle === null) {
        // Snap only while moving: a resize is already anchored to an edge the user chose, and
        // pulling that edge elsewhere mid-gesture would fight them.
        const snapped = snapRect(rect, { canvas, others: otherRects(draft, active) });
        rect = clampRect({ ...rect, x: snapped.x, y: snapped.y }, canvas, min, {
          even: active.kind === "box",
        });
        setGuides(snapped.guides);
      } else {
        setGuides([]);
      }
      setDraft(withRect(draft, active.kind, active.key, rect));
    },
    [draft, canvas, pointToCanvas],
  );

  const onPointerUp = useCallback(() => {
    const active = drag.current;
    drag.current = null;
    setGuides([]);
    if (active === null || draft === null) return;
    const rect = rectOf(active.kind, active.key);
    if (rect === null) return;
    void commit(active.kind, active.key, rect, active.start);
  }, [draft, rectOf, commit]);

  /** Arrow keys repeat; one PUT per press would be one PUT per 30 ms. */
  const nudgeTimer = useRef<number>(0);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (target === null || draft === null) return;
      const step = event.shiftKey ? 10 : 1;
      const deltas: Record<string, [number, number]> = {
        ArrowLeft: [-step, 0],
        ArrowRight: [step, 0],
        ArrowUp: [0, -step],
        ArrowDown: [0, step],
      };
      const delta = deltas[event.key];
      if (delta === undefined) return;
      event.preventDefault();
      const rect = rectOf(target.kind, target.key);
      if (rect === null) return;
      const min =
        target.kind === "box" ? MIN_SIZE.box : target.kind === "avatar" ? MIN_SIZE.avatar : MIN_SIZE.text;
      const next = moveRect(rect, delta[0], delta[1], canvas, min, {
        even: target.kind === "box",
      });
      setDraft(withRect(draft, target.kind, target.key, next));
      // Keyed nudges come in bursts; committing each one would be one PUT per arrow press.
      window.clearTimeout(nudgeTimer.current);
      nudgeTimer.current = window.setTimeout(() => {
        void commit(target.kind, target.key, next, rect);
      }, 400);
    },
    [target, draft, rectOf, canvas, commit],
  );

  const resetAll = useCallback(async () => {
    setSaving(true);
    try {
      const next = await api.resetVideoLayout(projectId);
      setState(next);
      setDraft(draftOf(next));
      setTarget(null);
      onChanged();
      onNotice("布局已恢复成模板编译时的样子。");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }, [projectId, onChanged, onNotice]);

  const resetOne = useCallback(async () => {
    if (target === null) return;
    const rect = compiled.get(target.kind === "box" ? "box" : `${target.kind}:${target.key}`);
    if (rect === undefined) return;
    setSaving(true);
    try {
      const next = await api.updateVideoLayout(projectId, payloadFor(target.kind, target.key, rect));
      setState(next);
      setDraft(draftOf(next));
      onChanged();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSaving(false);
    }
  }, [target, compiled, projectId, onChanged]);

  if (state === null || draft === null) {
    return (
      <div className="layout-editor">
        {error === null ? (
          <p className="muted small">正在读取布局…</p>
        ) : (
          <p className="error-text">{error}</p>
        )}
      </div>
    );
  }

  const selected = target === null ? null : rectOf(target.kind, target.key);
  const selectedCompiled =
    target === null ? undefined : compiled.get(target.kind === "box" ? "box" : `${target.kind}:${target.key}`);
  const typeSize =
    selected === null || selectedCompiled === undefined || target === null || target.kind === "box"
      ? null
      : scaledFont(
          state.texts.find((slot) => slot.key === target.key)?.font_size ?? undefined,
          selectedCompiled,
          selected,
        );

  return (
    <div className="layout-editor">
      {error !== null ? <p className="error-text">{error}</p> : null}
      <div className="layout-host" ref={hostRef}>
        <div
          className="layout-stage"
          ref={stageRef}
          role="application"
          aria-label="画布布局"
          tabIndex={0}
          style={{ width: canvas.w, height: canvas.h, transform: `scale(${scale})` }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onKeyDown={onKeyDown}
        >
          {state.background_url !== "" ? (
            <img className="layout-background" src={state.background_url} alt="" draggable={false} />
          ) : null}
          {state.plate_url !== "" && state.plate !== null ? (
            <img
              className="layout-piece"
              src={state.plate_url}
              style={boxStyle(state.plate)}
              alt=""
              draggable={false}
            />
          ) : null}
          {state.stroke_url !== "" && state.stroke !== null ? (
            <img
              className="layout-piece"
              src={state.stroke_url}
              style={boxStyle(state.stroke)}
              alt=""
              draggable={false}
            />
          ) : null}
          {state.avatars.map((slot) =>
            slot.lit_url ? (
              <img
                key={`face-${slot.key}`}
                className="layout-piece"
                src={slot.lit_url}
                style={boxStyle(draft.avatars[slot.key] ?? slot)}
                alt=""
                draggable={false}
              />
            ) : null,
          )}
          {renderBox(draft.box, "box", "素材框", target, saving)}
          {state.texts.map((slot) => {
            const rect = draft.texts[slot.key] ?? slot;
            return renderBox(rect, "text", slot.label || slot.key, target, saving, slot.key);
          })}
          {state.avatars.map((slot) => {
            const rect = draft.avatars[slot.key] ?? slot;
            return renderBox(rect, "avatar", slot.label || slot.key, target, saving, slot.key);
          })}
          {guides.map((guide) => (
            <div
              key={`${guide.axis}-${guide.at}`}
              className={`layout-guide is-${guide.axis}`}
              style={guide.axis === "x" ? { left: guide.at } : { top: guide.at }}
            />
          ))}
        </div>
      </div>

      <div className="layout-bar">
        <span className="layout-readout">
          {selected === null || target === null
            ? `画布 ${canvas.w} × ${canvas.h} · 点一个框开始拖`
            : `${labelOf(state, target)} · ${describe(selected)}${
                typeSize === null ? "" : ` · 字号 ${typeSize}`
              }`}
        </span>
        {state.is_default && draftIsDefault(state, draft) ? null : (
          <span className="layout-flag">已改动</span>
        )}
        <span className="layout-actions">
          <button
            type="button"
            className="ghost"
            disabled={saving || target === null || selectedCompiled === undefined}
            onClick={() => void resetOne()}
          >
            这块恢复默认
          </button>
          <button
            type="button"
            className="ghost"
            disabled={saving || (state.is_default && draftIsDefault(state, draft))}
            onClick={() => void resetAll()}
          >
            全部恢复默认
          </button>
        </span>
      </div>

      <p className="section-note">
        拖动画框移动，拖边角改大小；方向键微调（按住 Shift 一次 10 px）。素材框只能是偶数尺寸，
        头像框按原比例缩放，文字框缩放时字号跟着变。这里改的是这个项目的布局，不会动模板本身。
      </p>
    </div>
  );
}

// --- pure helpers -------------------------------------------------------

function draftOf(layout: VideoLayout): Draft {
  return {
    box: layout.box,
    texts: Object.fromEntries(layout.texts.map((slot) => [slot.key, rectOfSlot(slot)])),
    avatars: Object.fromEntries(layout.avatars.map((slot) => [slot.key, rectOfSlot(slot)])),
  };
}

function rectOfSlot(slot: VideoRect): Rect {
  return { x: slot.x, y: slot.y, w: slot.w, h: slot.h };
}

function withRect(draft: Draft, kind: Kind, key: string, rect: Rect): Draft {
  if (kind === "box") return { ...draft, box: rect };
  if (kind === "avatar") return { ...draft, avatars: { ...draft.avatars, [key]: rect } };
  return { ...draft, texts: { ...draft.texts, [key]: rect } };
}

/** Whether the draft still matches the compile — including edits not yet committed. */
function draftIsDefault(state: VideoLayout, draft: Draft): boolean {
  if (isMoved(draft.box, state.compiled_box)) return false;
  for (const slot of state.texts) {
    const rect = draft.texts[slot.key];
    if (rect !== undefined && isMoved(rect, slot)) return false;
  }
  for (const slot of state.avatars) {
    const rect = draft.avatars[slot.key];
    if (rect !== undefined && isMoved(rect, slot)) return false;
  }
  return true;
}

/** Every other slot's rect, for snapping. The dragged one is excluded or it snaps to itself. */
function otherRects(draft: Draft, active: { kind: Kind; key: string }): Rect[] {
  const out: Rect[] = [];
  if (active.kind !== "box") out.push(draft.box);
  for (const [key, rect] of Object.entries(draft.texts)) {
    if (active.kind === "text" && active.key === key) continue;
    out.push(rect);
  }
  for (const [key, rect] of Object.entries(draft.avatars)) {
    if (active.kind === "avatar" && active.key === key) continue;
    out.push(rect);
  }
  return out;
}

/**
 * The topmost slot under the point.
 *
 * Text over avatars over the box: the draw order is the paint order, and a hit test that
 * disagreed with the picture would select the box every time the user aimed at the title inside
 * it — which is most of the time.
 */
function hitTest(
  draft: Draft,
  state: VideoLayout | null,
  point: { x: number; y: number },
): { kind: Kind; key: string; rect: Rect } | null {
  if (state === null) return null;
  const inside = (rect: Rect): boolean =>
    point.x >= rect.x &&
    point.x <= rect.x + rect.w &&
    point.y >= rect.y &&
    point.y <= rect.y + rect.h;
  for (const slot of [...state.texts].reverse()) {
    const rect = draft.texts[slot.key];
    if (rect !== undefined && inside(rect)) return { kind: "text", key: slot.key, rect };
  }
  for (const slot of [...state.avatars].reverse()) {
    const rect = draft.avatars[slot.key];
    if (rect !== undefined && inside(rect)) return { kind: "avatar", key: slot.key, rect };
  }
  return null;
}

function labelOf(state: VideoLayout, target: Target): string {
  if (target.kind === "box") return "素材框";
  const list = target.kind === "avatar" ? state.avatars : state.texts;
  return list.find((slot) => slot.key === target.key)?.label || target.key;
}

/** The style of one absolutely placed layer, in canvas pixels. */
function boxStyle(rect: VideoRect): React.CSSProperties {
  return { left: rect.x, top: rect.y, width: rect.w, height: rect.h };
}

function renderBox(
  rect: Rect,
  kind: Kind,
  label: string,
  target: Target | null,
  saving: boolean,
  key = "box",
): React.JSX.Element {
  const active = target !== null && target.kind === kind && target.key === key;
  return (
    <div
      key={`slot-${kind}-${key}`}
      className={`layout-slot is-${kind}${active ? " is-active" : ""}${saving ? " is-saving" : ""}`}
      style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
      data-slot={key}
      aria-hidden="true"
    >
      <span className="layout-slot-label">{label}</span>
      {active ? (
        <>
          {Object.entries(handlePoints(rect)).map(([handle, point]) => (
            <span
              key={handle}
              className={`layout-handle is-${handle}`}
              style={{ left: point.x - rect.x, top: point.y - rect.y }}
            />
          ))}
        </>
      ) : null}
    </div>
  );
}
