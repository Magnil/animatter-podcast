/**
 * Visual harness for the transcript pane.
 *
 * Mounts `SentenceList` with hand-authored data so each visual state can be
 * checked in isolation — a screenshot of the real workbench can only show
 * whatever the database happens to contain, which never includes "a word is
 * selected and the bubble is open" or "the speaker menu is expanded".
 *
 * Not part of the app build: `harness.html` is a separate Vite entry used only
 * for `vite` + `/harness.html?scene=…` and for the headless screenshots.
 */

import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import type { Sentence, SpeakerInfo, Token } from "./api";
import { SentenceList } from "./SentenceList";
import type { TokenRange } from "./transcript";
import "./styles.css";

const sentences: Sentence[] = [
  {
    id: "s0",
    ord: 0,
    token_start: 0,
    token_end: 5,
    speaker_id: 0,
    text: "今天我们聊播客剪辑流程。",
    start_ms: 0,
    end_ms: 2400,
  },
  {
    id: "s1",
    ord: 1,
    token_start: 5,
    token_end: 9,
    speaker_id: 1,
    text: "首先介绍一下嘉宾张三。",
    start_ms: 3709,
    end_ms: 6440,
  },
];

/** Index 1 was corrected (blue); 3 is fully cut, 4 is half cut (red). */
const tokens: Token[] = [
  { idx: 0, start_ms: 0, end_ms: 760, text: "今天", kind: "word", punc_after: "", derived: false, revised: false },
  { idx: 1, start_ms: 760, end_ms: 1280, text: "咱们", kind: "word", punc_after: "", derived: false, revised: true },
  { idx: 2, start_ms: 1280, end_ms: 1960, text: "聊播客", kind: "word", punc_after: "", derived: false, revised: false },
  { idx: 3, start_ms: 1960, end_ms: 2120, text: "剪辑", kind: "word", punc_after: "", derived: false, revised: false },
  { idx: 4, start_ms: 2120, end_ms: 2400, text: "流程", kind: "word", punc_after: "。", derived: false, revised: false },
  { idx: 5, start_ms: 3709, end_ms: 4360, text: "首先", kind: "word", punc_after: "", derived: false, revised: false },
  { idx: 6, start_ms: 4360, end_ms: 5160, text: "介绍一下", kind: "word", punc_after: "", derived: false, revised: false },
  { idx: 7, start_ms: 5160, end_ms: 5560, text: "嘉宾", kind: "word", punc_after: "", derived: false, revised: false },
  { idx: 8, start_ms: 5560, end_ms: 6440, text: "张三", kind: "word", punc_after: "。", derived: false, revised: false },
];

const speakers: SpeakerInfo[] = [
  { local_id: 0, name: "主持人", color: "#2563eb", sentence_count: 1 },
  { local_id: 1, name: "嘉宾 张三", color: "#db2777", sentence_count: 1 },
];

/** One cut covers 剪辑 entirely; the other clips 流程's first 120 ms. */
const deletedRanges = [
  { start_ms: 1960, end_ms: 2120 },
  { start_ms: 2120, end_ms: 2240 },
];

const scene = new URLSearchParams(window.location.search).get("scene") ?? "bubble";

function Harness(): React.JSX.Element {
  const [selection, setSelection] = useState<TokenRange | null>(null);
  const [sentenceSelection, setSentenceSelection] = useState<Set<number>>(
    scene === "multi" ? new Set([0, 1]) : new Set(),
  );
  const [noted, setNoted] = useState("");

  useEffect(() => {
    // Drive the real handler so the bubble gets a real DOM rect; synthesising
    // coordinates instead would let a broken `pointAt` pass unnoticed.
    const timer = window.setTimeout(() => {
      if (scene === "bubble" || scene === "correct") {
        const target = document.querySelector<HTMLElement>('[data-token="2"]');
        target?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        if (scene === "correct") {
          window.setTimeout(() => {
            document.querySelector<HTMLButtonElement>(".bubble-fix")?.click();
          }, 150);
        }
      }
      if (scene === "speaker") {
        document.querySelector<HTMLButtonElement>(".speaker-name")?.click();
      }
      if (scene === "rename") {
        document.querySelector<HTMLButtonElement>(".speaker-name")?.click();
        window.setTimeout(() => {
          const items = Array.from(document.querySelectorAll<HTMLButtonElement>(".speaker-menu button"));
          items.find((item) => item.textContent?.includes("重命名"))?.click();
        }, 150);
      }
      if (scene === "colors") {
        // Nothing selected: the point is to see the resting state of every word.
      }
      if (scene === "drag") {
        // React implements `onMouseEnter` from delegated `mouseover` events, so
        // dispatching `mouseenter` directly would be silently ignored and the
        // drag would look broken when it is not.
        const first = document.querySelector<HTMLElement>('[data-token="5"]');
        const last = document.querySelector<HTMLElement>('[data-token="7"]');
        first?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
        last?.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true }));
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, []);

  return (
    <div style={{ width: 420, padding: 12, background: "var(--panel)", minHeight: "100vh" }}>
      <SentenceList
        sentences={sentences}
        tokens={tokens}
        positionMs={500}
        selection={selection}
        sentenceSelection={sentenceSelection}
        speakers={speakers}
        onSelectionChange={setSelection}
        onSentenceSelectionChange={setSentenceSelection}
        onPlaySentence={() => setNoted("play")}
        onSeek={() => undefined}
        onDeleteWords={() => setNoted("delete")}
        onCorrectWord={(proposed) => setNoted(`correct:${proposed}`)}
        onBoundary={(action) => setNoted(`boundary:${action}`)}
        onAssignSpeaker={(localId, ords) => setNoted(`assign:${localId}:${ords.join(",")}`)}
        onCreateSpeaker={(ords) => setNoted(`create:${ords.join(",")}`)}
        onRenameSpeaker={(localId, name) => setNoted(`rename:${localId}:${name}`)}
        deletedRanges={deletedRanges}
        busy={false}
      />
      <p className="muted small" style={{ marginTop: 12 }}>
        last callback: {noted || "—"}
      </p>
    </div>
  );
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <Harness />
  </StrictMode>,
);
