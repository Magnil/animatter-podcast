/**
 * 两个导出面板的**纯逻辑**：默认文件名、按钮文字、进度条状态。
 *
 * 为什么单独一个文件：这三件事都有「看起来对、实际错」的写法，而且都在面板里，
 * 组件测试在这个仓库里是走查脚本（要跑浏览器），太贵。放在纯函数里就能用 `node --test` 钉住：
 *
 * - 默认文件名用了 UTC 日期 → 半夜导出会变成昨天的日期（`filmFilename` 已经踩过一次）；
 * - 停止之后按钮仍然写「停止」→ 用户点第二下以为没生效；
 * - `cancelled` 被当成 `failed` → 一个用户主动的动作被显示成红色故障。
 */

export interface ExportProgress {
  /** The server's job id: needed to poll it and to deliver the finished file. */
  id: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  /** 0..1 of the encoded output timeline. */
  progress: number;
  error: string | null;
  /** Server's line about *how* it is running: 「14 块串行编码：空闲 4.2 GB < …」. */
  note: string;
}

/**
 * How the delivery (the copy to the user's folder) is going.
 *
 * `done` on the job means the *encode* finished; the copy is a second thing that can still be
 * in flight or can fail. Keeping them apart is what lets the panel stop saying 「正在保存…」
 * after it has actually saved.
 */
export type DeliveryState = "idle" | "saving" | "saved" | "failed";

/** The export dialog's own state: idle, or one job in flight. */
export interface ExportState {
  /** Null while idle or after the job reached a terminal state. */
  job: ExportProgress | null;
  /** True from the click on 停止 until the server confirms the job is gone. */
  stopping: boolean;
  delivery: DeliveryState;
  /** Where it landed, once it has. Shown instead of a vague 「已保存」. */
  savedTo: string;
}

export const IDLE: ExportState = { job: null, stopping: false, delivery: "idle", savedTo: "" };

const TERMINAL = ["done", "failed", "cancelled"] as const;

/** Has this job stopped moving — finished, stopped, or broken? */
export function isTerminalStatus(status: ExportProgress["status"]): boolean {
  return (TERMINAL as readonly string[]).includes(status);
}

/**
 * What the dialog's primary button says.
 *
 * `停止` is not offered once stopping has been requested: a second click would send a second
 * cancel to a job that is already dying, and — worse — the button would look like it did
 * nothing when the first click was in fact working.
 *
 * It is also not offered for a **finished** job. The panel keeps the last progress on screen
 * after `done` (the bar says 「编码完成，正在保存…」 while the copy runs), so keying the button
 * off `job !== null` leaves a 「停止」 under a film that already exists — and clicking it sends
 * a cancel for a job that ended.
 */
export function primaryLabel(state: ExportState): string {
  if (state.stopping) return "正在停止…";
  if (isRunning(state)) return "停止";
  return "导出";
}

/** Is the primary button usable right now? */
export function primaryDisabled(state: ExportState, ...rest: boolean[]): boolean {
  if (state.stopping) return true;
  if (isRunning(state)) return false;
  return rest.some(Boolean);
}

/** Is a job in flight, i.e. should 停止 be shown instead of 导出? */
export function isRunning(state: ExportState): boolean {
  return state.job !== null && !isTerminalStatus(state.job.status);
}

export interface ProgressView {
  /** 0..100, clamped. */
  percent: number;
  /** The line under the bar. */
  text: string;
  tone: "work" | "ok" | "bad" | "warn";
}

/**
 * The progress bar's content.
 *
 * Four terminal shapes, and they are deliberately different colours: an export that the *user*
 * stopped is not a failure, and colouring it red teaches them to distrust the one signal that
 * means "something actually went wrong".
 */
export function progressView(state: ExportState, eta: string): ProgressView {
  const job = state.job;
  if (job === null) return { percent: 0, text: "", tone: "work" };
  const percent = Math.max(0, Math.min(100, Math.floor(job.progress * 100)));
  if (state.stopping && !isTerminalStatus(job.status)) {
    return { percent, text: `正在停止…（${percent}%）`, tone: "warn" };
  }
  switch (job.status) {
    case "queued":
      return { percent, text: "排队中…", tone: "work" };
    case "running":
      return {
        percent,
        // The note is the only place 「串行」 vs 「2 个并行」 is visible; without it a
        // 18-minute bar cannot be told apart from a stuck one.
        text: `${percent}%　${eta}${job.note ? `　${job.note}` : ""}`,
        tone: "work",
      };
    case "done":
      // Three different sentences, because 「正在保存…」 that never changes is worse than no
      // text at all: it looks like the save hung, and the user cannot tell whether the file
      // they asked for exists. The bar is at 100 % in all three — the *encode* is done; the
      // union of "encoded" and "in my folder" is what the sentence has to communicate.
      if (state.delivery === "saved") {
        return {
          percent: 100,
          text: state.savedTo ? `已保存到 ${state.savedTo}` : "已保存",
          tone: "ok",
        };
      }
      if (state.delivery === "failed") {
        return { percent: 100, text: "编码完成，但保存到文件夹失败（见提示）", tone: "bad" };
      }
      return { percent: 100, text: "编码完成，正在保存…", tone: "ok" };
    case "cancelled":
      return { percent, text: "已停止", tone: "warn" };
    case "failed":
      return { percent, text: job.error ?? "导出失败", tone: "bad" };
  }
}

/**
 * 默认文件名：期号 + 本地日期，不带后缀。
 *
 * 不带后缀是刻意的——后缀由成品格式决定，面板上的输入框只编辑名字。用户在框里手打
 * 「成片.wav」也不会得到 `成片.wav.m4a`：服务端会剥掉任何已知媒体后缀。
 *
 * 本地日期而不是 `toISOString()`：凌晨 00:30 在 UTC 还是前一天，用户看到的日期必须和他
 * 的日历一致（`filmFilename` 已经因为这件事改过一次）。
 */
export function defaultExportName(episode: string, now: Date): string {
  const cleaned = episode.replace(/[\\/:*?"<>|]/g, "_").trim() || "成片";
  const pad = (value: number): string => String(value).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}`;
  return `${cleaned}-${stamp}`;
}

/** The value the folder field starts with. Empty means "the server's default folder". */
export const DEFAULT_FOLDER = "";

/** A human sentence for where a file landed, used as the notice after delivery. */
export function deliveryNotice(path: string, bytes: number): string {
  const mb = bytes / 1024 / 1024;
  const size = mb >= 1 ? `${mb.toFixed(0)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `已保存到 ${path}（${size}）`;
}
