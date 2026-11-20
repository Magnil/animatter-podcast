/**
 * Typed API client.
 *
 * Every call surfaces the backend's `AppError(code, message, hint)` shape rather
 * than a bare status, because the hint is the actionable part (AGENTS.md §8).
 */

export interface ApiError {
  code: string;
  message: string;
  hint: string;
}

export class RequestFailed extends Error {
  readonly status: number;
  readonly detail: ApiError;

  constructor(status: number, detail: ApiError) {
    super(detail.message);
    this.name = "RequestFailed";
    this.status = status;
    this.detail = detail;
  }
}

/** Turn a non-2xx response into the `AppError` shape every caller expects. */
async function failure(response: Response, fallback: string): Promise<RequestFailed> {
  let detail: ApiError = {
    code: "unknown",
    message: `${fallback}（HTTP ${response.status}）`,
    hint: "",
  };
  try {
    const body = (await response.json()) as { error?: ApiError; detail?: string };
    if (body.error) detail = body.error;
    else if (body.detail) detail = { code: "unknown", message: body.detail, hint: "" };
  } catch {
    // Non-JSON error body: keep the generic message.
  }
  return new RequestFailed(response.status, detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await rawRequest(path, init);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * Same error handling as `request`, but returns the body as text.
 *
 * `request` unconditionally calls `response.json()`. The subtitle preview is
 * `text/plain` (SRT/VTT/TXT — a subtitle file is text by definition), so that
 * call threw, the panel's `catch` swallowed it, and the UI showed
 * 「没有可导出的字幕」 for a version that had 1074 perfectly good sentences.
 * Parsing must follow `Content-Type`, not the caller's hope.
 */
async function requestText(path: string, init?: RequestInit): Promise<string> {
  const response = await rawRequest(path, init);
  return response.text();
}

async function rawRequest(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) throw await failure(response, "请求失败");
  return response;
}

// --- types --------------------------------------------------------------

export interface Token {
  idx: number;
  start_ms: number;
  end_ms: number;
  text: string;
  kind: string;
  punc_after: string;
  derived: boolean;
  /** A human rewrote this word; rendered blue. */
  revised: boolean;
  /** A human hid this word from the text only; the audio still plays. */
  hidden: boolean;
}

export interface SpeakerInfo {
  local_id: number;
  name: string;
  color: string;
  sentence_count: number;
}

export interface Sentence {
  id: string;
  ord: number;
  token_start: number;
  token_end: number;
  speaker_id: number | null;
  text: string;
  start_ms: number;
  end_ms: number;
}

export interface RenderState {
  id: string;
  duration_ms: number;
  status: "queued" | "running" | "done" | "failed";
  url: string;
  /** 0..1 of the encoded output timeline. */
  progress: number;
  error: string | null;
}

export interface Version {
  id: string;
  version: number;
  duration_ms: number;
  token_count: number;
  sentence_count: number;
  created_at: string;
  /** How far this timeline sits from the ASR's own timestamps (ms). */
  time_shift_ms?: number;
}

export interface Transcript {
  version: Version;
  sentences: Sentence[];
  tokens: Token[];
}

export interface Waveform {
  rate: number;
  from_ms: number;
  to_ms: number;
  min: number[];
  max: number[];
  duration_ms: number;
  levels: number[];
}

export interface SentenceView {
  ord: number;
  token_start: number;
  token_end: number;
  speaker_id: number | null;
  start_ms: number;
  end_ms: number;
  text: string;
}

// --- endpoints ----------------------------------------------------------

export const api = {
  listProjects: () =>
    request<{ items: { id: string; name: string }[]; total: number }>("/api/projects"),

  listVersions: (projectId: string) =>
    request<Version[]>(`/api/projects/${projectId}/versions`),

  transcript: (versionId: string, from = 0, limit = 400) =>
    request<Transcript>(
      `/api/versions/${versionId}/transcript?from_sentence=${from}&limit=${limit}`,
    ),

  waveform: (projectId: string, mediaId: string, bps: number, fromMs: number, toMs: number) =>
    request<Waveform>(
      `/api/projects/${projectId}/media/${mediaId}/waveform` +
        `?buckets_per_second=${bps}&from_ms=${fromMs}&to_ms=${toMs}`,
    ),

  /**
   * The whole-file envelope, as one binary download.
   *
   * Deliberately not `request()`: this body is ~4 MB of int8 pairs and parsing
   * it as JSON would cost ten times the bytes plus a string decode. The caller
   * gets the raw buffer so `parseEnvelope` can *view* it without copying.
   */
  waveformEnvelope: async (projectId: string, mediaId: string): Promise<ArrayBuffer> => {
    const response = await fetch(
      `/api/projects/${projectId}/media/${mediaId}/waveform/envelope`,
    );
    if (!response.ok) throw await failure(response, "波形加载失败");
    return response.arrayBuffer();
  },

  /**
   * Move the whole timeline to `shiftMs` relative to the ASR's own timestamps.
   *
   * A target, not a step: sending the same value twice must not double the
   * offset. See `services/align.py` for why that distinction is load-bearing.
   */
  timeShift: (versionId: string, shiftMs: number) =>
    request<{
      shift_ms: number;
      previous_ms: number;
      delta_ms: number;
      tokens: number;
      deletions: number;
      clamped: boolean;
      reason: string;
    }>(`/api/versions/${versionId}/time-shift`, {
      method: "POST",
      body: JSON.stringify({ shift_ms: shiftMs }),
    }),

  timeShiftState: (versionId: string) =>
    request<{
      time_shift_ms: number;
      earliest_ms: number;
      latest_ms: number;
      duration_ms: number;
    }>(`/api/versions/${versionId}/time-shift`),

  boundary: (
    versionId: string,
    action: "new_sentence" | "merge" | "to_prev" | "to_next",
    fromToken: number,
    toToken: number,
  ) =>
    request<{ sentences: SentenceView[] }>(`/api/versions/${versionId}/boundaries`, {
      method: "POST",
      body: JSON.stringify({ action, from_token: fromToken, to_token: toToken }),
    }),

  /** Replace a character selection. Addresses by token range, not by text. */
  editText: (versionId: string, tokenStart: number, tokenEnd: number, proposed: string) =>
    request<{ sentence_ord: number; token_start: number; token_end: number; delta: number }>(
      `/api/versions/${versionId}/text`,
      {
        method: "POST",
        body: JSON.stringify({
          token_start: tokenStart,
          token_end: tokenEnd,
          proposed,
        }),
      },
    ),

  /**
   * Hide words from the text only — the audio is untouched.
   *
   * The filler-word operation: cutting «嗯» out of the render leaves an audible
   * gap, while leaving it in the subtitle looks sloppy. One endpoint for both
   * directions so a toggle cannot drift out of sync with the server.
   */
  hideWords: (versionId: string, tokenStart: number, tokenEnd: number, hidden = true) =>
    request<{ token_start: number; token_end: number; changed: number; hidden: boolean }>(
      `/api/versions/${versionId}/hide`,
      {
        method: "POST",
        body: JSON.stringify({ token_start: tokenStart, token_end: tokenEnd, hidden }),
      },
    ),

  /** Move one token edge. Only the addressed token changes timing. */
  adjustEdge: (versionId: string, tokenIndex: number, edge: "start" | "end", newMs: number) =>
    request<{
      token_index: number;
      edge: string;
      start_ms: number;
      end_ms: number;
      clamped: boolean;
      reason: string;
      changed: boolean;
    }>(`/api/versions/${versionId}/edge`, {
      method: "POST",
      body: JSON.stringify({ token_index: tokenIndex, edge, new_ms: newMs }),
    }),

  // --- clipping ---------------------------------------------------------

  deletions: (versionId: string) =>
    request<{ id: string; start_ms: number; end_ms: number; source: string }[]>(
      `/api/versions/${versionId}/deletions`,
    ),

  /** Delete by token ids (character selection) or by an explicit time range. */
  addDeletion: (versionId: string, body: { token_ids?: number[]; start_ms?: number; end_ms?: number }) =>
    request<{ id: string; start_ms: number; end_ms: number; source: string }[]>(
      `/api/versions/${versionId}/deletions`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  removeDeletion: (deletionId: string) =>
    request<void>(`/api/deletions/${deletionId}`, { method: "DELETE" }),

  /**
   * Take back the deletion of specific words — the editor's 「还」 button.
   *
   * Separate from `removeDeletion`, which drops a whole deletion row and is what
   * the 「恢复删除」 transport button uses. This one is word-scoped: a row that
   * covers a whole sentence must not disappear because one word in it was
   * clicked.
   */
  restoreWords: (versionId: string, tokenIds: number[]) =>
    request<{ id: string; start_ms: number; end_ms: number; source: string }[]>(
      `/api/versions/${versionId}/deletions/restore`,
      { method: "POST", body: JSON.stringify({ token_ids: tokenIds }) },
    ),

  edl: (versionId: string, snap = true) =>
    request<{
      original_duration_ms: number;
      output_duration_ms: number;
      removed_ms: number;
      segment_count: number;
      keeps: { orig_start: number; orig_end: number; new_start: number }[];
      deletions: { start_ms: number; end_ms: number; source: string }[];
      clamps: { requested: number; applied: number; edge: string; reason: string }[];
      signature: string;
    }>(`/api/versions/${versionId}/edl?snap=${snap}`),

  render: (versionId: string, options: { preview?: boolean; lossless?: boolean } = {}) => {
    const params = new URLSearchParams();
    if (options.preview) params.set("preview", "true");
    if (options.lossless) params.set("lossless", "true");
    const query = params.toString();
    return request<RenderState>(
      `/api/versions/${versionId}/render${query ? `?${query}` : ""}`,
      { method: "POST" },
    );
  },

  renderStatus: (renderId: string) => request<RenderState>(`/api/renders/${renderId}`),

  renderUrl: (path: string) => path,

  // --- history ----------------------------------------------------------

  history: (versionId: string) =>
    request<
      {
        id: string;
        seq: number;
        op_type: string;
        label: string;
        detail: string;
        created_at: string;
        undone: boolean;
        undoable: boolean;
      }[]
    >(`/api/versions/${versionId}/history`),

  undoOp: (versionId: string, opId: string) =>
    request<{ undone: string; op_type: string }>(`/api/versions/${versionId}/undo`, {
      method: "POST",
      body: JSON.stringify({ op_id: opId }),
    }),

  // --- settings ---------------------------------------------------------

  settings: () => request<AppSettings>("/api/settings"),

  saveSettings: (values: Partial<AppSettings>) =>
    request<AppSettings>("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ values }),
    }),

  modelOptions: () =>
    request<{
      asr: { id: string; instant_vocabulary: boolean; context: boolean; diarization: boolean; max_hotwords: number }[];
      llm: string[];
      defaults: Record<string, unknown>;
    }>("/api/settings/models"),

  // --- hotwords ---------------------------------------------------------

  globalHotwords: () => request<HotwordItem[]>("/api/hotwords"),

  addGlobalHotword: (text: string, weight = 3) =>
    request<{ accepted: HotwordItem[] }>("/api/hotwords", {
      method: "POST",
      body: JSON.stringify([{ text, weight }]),
    }),

  removeGlobalHotword: (text: string) =>
    request<void>(`/api/hotwords/${encodeURIComponent(text)}`, { method: "DELETE" }),

  episodeHotwords: (projectId: string, mediaId: string) =>
    request<HotwordItem[]>(`/api/projects/${projectId}/media/${mediaId}/hotwords`),

  /**
   * Replace the whole per-episode list.
   *
   * Note the *replace*, not *append*: the endpoint exists so outline extraction
   * can regenerate the list wholesale. A call that only names the new term
   * therefore silently deletes every existing one — ``addEpisodeHotword`` below
   * posts to /append instead, and that is the one the UI uses for a single word.
   */
  setEpisodeHotwords: (projectId: string, mediaId: string, texts: string[], weight = 3) =>
    request<{ accepted: HotwordItem[]; rejected: { text: string; reason: string }[] }>(
      `/api/projects/${projectId}/media/${mediaId}/hotwords`,
      {
        method: "POST",
        body: JSON.stringify(texts.map((text) => ({ text, weight }))),
      },
    ),

  /** Append one term, leaving the rest of the episode's list untouched. */
  addEpisodeHotword: (projectId: string, mediaId: string, text: string, weight = 3) =>
    request<{ accepted: HotwordItem[]; rejected: { text: string; reason: string }[] }>(
      `/api/projects/${projectId}/media/${mediaId}/hotwords/append`,
      {
        method: "POST",
        body: JSON.stringify([{ text, weight }]),
      },
    ),

  removeEpisodeHotword: (projectId: string, mediaId: string, text: string) =>
    request<void>(
      `/api/projects/${projectId}/media/${mediaId}/hotwords/${encodeURIComponent(text)}`,
      { method: "DELETE" },
    ),

  /** Append several terms at once. `/append` keeps what is already there. */
  appendEpisodeHotwords: (projectId: string, mediaId: string, terms: { text: string; weight: number }[]) =>
    request<{ accepted: HotwordItem[]; rejected: { text: string; reason: string }[] }>(
      `/api/projects/${projectId}/media/${mediaId}/hotwords/append`,
      { method: "POST", body: JSON.stringify(terms) },
    ),

  saveOutline: (projectId: string, mediaId: string, content: string) =>
    request<Record<string, unknown>>(`/api/projects/${projectId}/media/${mediaId}/outline`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),

  /**
   * Persist reviewed candidates as the episode's list.
   *
   * Deliberately the *replace* endpoint: an outline extraction regenerates the
   * episode list wholesale (AGENTS.md §6.1), so after dropping the wrong
   * candidates the operator wants exactly what is left on screen, not a merge.
   */
  acceptSuggestions: (
    projectId: string,
    mediaId: string,
    terms: { text: string; weight: number }[],
  ) =>
    request<{
      episode_count: number;
      global_count: number;
      vocabulary_count: number;
      trimmed: number;
    }>(`/api/projects/${projectId}/media/${mediaId}/hotwords/suggest/accept`, {
      method: "POST",
      body: JSON.stringify(terms),
    }),

  /** Extract candidates from an outline that belongs to an existing project. */
  suggestFromOutline: (projectId: string, mediaId: string, content: string, meta = "") =>
    request<{
      terms: HotwordItem[];
      rejected: { text: string; reason: string }[];
      warnings: string[];
      truncated: boolean;
      raw_count: number;
    }>(`/api/projects/${projectId}/media/${mediaId}/hotwords/suggest`, {
      method: "POST",
      body: JSON.stringify({ content, meta }),
    }),

  /**
   * Extract hotword candidates from an outline with no project yet.
   *
   * The new-project dialog needs this: importing an outline there is only worth
   * anything *before* the transcription runs, and at that moment the project
   * does not exist. Keying extraction to a media row would put the feature
   * everywhere except the one place it changes the audio.
   */
  suggestOutlineTerms: (content: string, meta = "") =>
    request<{
      terms: HotwordItem[];
      rejected: { text: string; reason: string }[];
      warnings: string[];
      truncated: boolean;
      raw_count: number;
    }>("/api/hotwords/suggest", {
      method: "POST",
      body: JSON.stringify({ content, meta }),
    }),

  // --- project lifecycle -------------------------------------------------

  createProject: (name: string) =>
    request<{ id: string; name: string }>("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  registerMedia: (projectId: string, path: string) =>
    request<{ id: string; duration_ms: number }>(`/api/projects/${projectId}/media`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  transcribe: (projectId: string, mediaId: string, options: { model?: string; hotwords?: boolean }) =>
    request<{ id: string; hotword_count: number }>(`/api/projects/${projectId}/transcribe`, {
      method: "POST",
      body: JSON.stringify({
        media_id: mediaId,
        model: options.model,
        use_hotwords: options.hotwords ?? false,
      }),
    }),

  // --- one-click cleanup -------------------------------------------------

  cleanupPlan: (
    versionId: string,
    options: { minSilenceMs?: number; silencePadMs?: number; minDisfluencyMs?: number } = {},
  ) => {
    const params = new URLSearchParams();
    if (options.minSilenceMs != null) params.set("min_silence_ms", String(options.minSilenceMs));
    if (options.silencePadMs != null) params.set("silence_pad_ms", String(options.silencePadMs));
    if (options.minDisfluencyMs != null)
      params.set("min_disfluency_ms", String(options.minDisfluencyMs));
    const query = params.toString();
    return request<CleanupPlan>(
      `/api/versions/${versionId}/cleanup/plan${query ? `?${query}` : ""}`,
    );
  },

  /**
   * Commit one cleanup. The response carries the batch id, and that batch is
   * what a single undo reverts — 400 cuts are one history line, not 400.
   */
  applyCleanup: (
    versionId: string,
    options: {
      kinds: string[];
      minSilenceMs?: number;
      silencePadMs?: number;
      minDisfluencyMs?: number;
    },
  ) =>
    request<{
      batch: string;
      op_id: string;
      deletions: number;
      counts: Record<string, number>;
      removed_ms: number;
      kinds: string[];
    }>(`/api/versions/${versionId}/cleanup`, {
      method: "POST",
      body: JSON.stringify({
        kinds: options.kinds,
        min_silence_ms: options.minSilenceMs,
        silence_pad_ms: options.silencePadMs,
        min_disfluency_ms: options.minDisfluencyMs,
      }),
    }),

  /** Bring back a whole category ("还原已删除口癖词") or one batch. */
  restoreCleanup: (versionId: string, options: { source?: string; batch?: string }) => {
    const params = new URLSearchParams();
    if (options.source) params.set("source", options.source);
    if (options.batch) params.set("batch", options.batch);
    return request<{ restored: number; source: string | null; batch: string | null }>(
      `/api/versions/${versionId}/cleanup/restore?${params}`,
      { method: "POST" },
    );
  },

  /**
   * Batch-hide filler words: gone from the subtitles, audio untouched.
   *
   * A separate call rather than a flag on `applyCleanup`, because the two write
   * different row types — one creates deletions that reach the renderer, the
   * other only flips a boolean on the token.
   */
  hideFillers: (versionId: string) =>
    request<{
      op_id: string;
      /** Places hidden — the same unit the dialog counted. */
      hidden: number;
      /** Raw tokens; larger when adjacent fillers merged into one band. */
      words: number;
      span_ms: number;
      counts: Record<string, number>;
      kinds: string[];
    }>(`/api/versions/${versionId}/cleanup/hide-fillers`, { method: "POST" }),

  cleanupCounts: (versionId: string) =>
    request<{ counts: Record<string, number> }>(`/api/versions/${versionId}/cleanup/counts`),

  // --- archive -----------------------------------------------------------

  /**
   * Save *and* download in one request, returning both halves of "where did it go".
   *
   * Writing the file on the server and reporting its path was not enough: the
   * user's report was "I clicked 保存项目 and nothing was downloaded, so I don't
   * know where the file is". Offering only a download would be the mirror
   * problem — the file lands in the browser's folder, and 载入项目 still has no
   * path it can read back. Calling both endpoints would write the archive twice.
   *
   * So: fetch the bytes here, read the server's absolute path out of the
   * response headers, and let the caller hand the blob to the browser. One
   * request, one file, and a notice that can name the exact location.
   */
  saveAndDownloadArchive: async (
    projectId: string,
  ): Promise<{
    blob: Blob;
    filename: string;
    path: string;
    sentence_count: number;
    op_count: number;
    deletion_count: number;
  }> => {
    // Not `request()`: that parses JSON, and this body is the archive itself.
    const response = await fetch(`/api/projects/${projectId}/archive/file`);
    if (!response.ok) throw await failure(response, "保存失败");

    // Percent-encoded on the server because HTTP headers are latin-1 and the
    // project names are not: 「第 07 期」 would raise on the way out otherwise.
    const decoded = (name: string): string => {
      const raw = response.headers.get(name) ?? "";
      try {
        return decodeURIComponent(raw);
      } catch {
        return raw;
      }
    };
    const number = (name: string): number => Number(response.headers.get(name) ?? "0") || 0;
    return {
      blob: await response.blob(),
      filename: decoded("X-Archive-Filename") || "project.project",
      path: decoded("X-Archive-Path"),
      sentence_count: number("X-Archive-Sentences"),
      op_count: number("X-Archive-Ops"),
      deletion_count: number("X-Archive-Deletions"),
    };
  },

  inspectArchive: (path: string) =>
    request<{
      path: string;
      filename: string;
      project_name: string;
      sentence_count: number;
      op_count: number;
      deletion_count: number;
      saved_at: string;
      size_bytes: number;
    }>("/api/archive/inspect", { method: "POST", body: JSON.stringify({ path }) }),

  loadArchive: (path: string, options: { name?: string; mediaPath?: string } = {}) =>
    request<{
      project_id: string;
      project_name: string;
      version_id: string;
      sentence_count: number;
      media_relinked: boolean;
      warning: string;
    }>("/api/archive/load", {
      method: "POST",
      body: JSON.stringify({ path, name: options.name, media_path: options.mediaPath }),
    }),

  // --- subtitles ---------------------------------------------------------

  subtitlePreview: (versionId: string, options: { fmt?: string; limit?: number; mapped?: boolean } = {}) => {
    const p = new URLSearchParams();
    p.set("fmt", options.fmt ?? "srt");
    if (options.limit) p.set("limit", String(options.limit));
    if (options.mapped === false) p.set("mapped", "false");
    return requestText(`/api/versions/${versionId}/subtitles/preview?${p}`);
  },

  subtitleIssues: (versionId: string) =>
    request<{ sentence_ord: number; kind: string; detail: string }[]>(
      `/api/versions/${versionId}/subtitles/issues`,
    ),

  exportSubtitles: (versionId: string, fmt: string) =>
    request<{
      id: string;
      filename: string;
      fmt: string;
      url: string;
      cue_count: number;
      mapped: boolean;
      issues: { sentence_ord: number; kind: string; detail: string }[];
    }>(`/api/versions/${versionId}/subtitles/export?fmt=${fmt}`, { method: "POST" }),

  exportFileUrl: (url: string) => url,

  // --- Audacity labels ---------------------------------------------------

  labelsPreview: (versionId: string, limit = 12) =>
    requestText(`/api/versions/${versionId}/labels/preview?limit=${limit}`),

  labelsInfo: (versionId: string, options: { includeFiller?: boolean } = {}) => {
    const p = new URLSearchParams();
    if (options.includeFiller === false) p.set("include_filler", "false");
    return request<{
      version_id: string;
      filename: string;
      lines: number;
      words: number;
      fillers: number;
      silences: number;
      deleted: number;
      tokens: number;
    }>(`/api/versions/${versionId}/labels/info?${p}`);
  },

  /**
   * Fetch the label file and the numbers describing it.
   *
   * Same header trick as `saveAndDownloadArchive`: the filename is Chinese, HTTP
   * headers are latin-1, so the server percent-encodes it and we decode here.
   */
  downloadLabels: async (
    versionId: string,
  ): Promise<{ blob: Blob; filename: string; lines: number }> => {
    const response = await rawRequest(`/api/versions/${versionId}/labels/file`);
    const decoded = (name: string): string => {
      const raw = response.headers.get(name) ?? "";
      try {
        return decodeURIComponent(raw);
      } catch {
        return raw;
      }
    };
    return {
      blob: await response.blob(),
      filename: decoded("X-Labels-Filename") || "audacity_labels.txt",
      lines: Number(response.headers.get("X-Labels-Lines") ?? "0") || 0,
    };
  },


  undo: (versionId: string) =>
    request<{ undone: string; op_type: string }>(`/api/versions/${versionId}/undo`, {
      method: "POST",
    }),

  audioUrl: (projectId: string, mediaId: string) =>
    `/api/projects/${projectId}/media/${mediaId}/audio`,

  // --- ShowNotes / 文稿 ---------------------------------------------------

  /**
   * Generate one document.
   *
   * One request that takes as long as the model takes — several minutes on a
   * 105-minute episode. That is why the panel shows elapsed time instead of a
   * fake percentage: there is no server-side progress to read, only a promise
   * that has not settled yet.
   */
  generateScript: (
    versionId: string,
    options: { format: ScriptFormat; mapped?: boolean; title?: string },
  ) =>
    request<{
      format: ScriptFormat;
      title: string;
      body: string;
      filename: string;
      chapters: { heading: string; start_ms: number; clock: string }[];
      /** Candidate titles (≤22 chars); the first one is what the body uses. */
      titles: string[];
      turns: number;
      blocks: number;
      warnings: string[];
    }>(`/api/versions/${versionId}/script/generate`, {
      method: "POST",
      body: JSON.stringify({
        format: options.format,
        mapped: options.mapped ?? true,
        title: options.title ?? "",
      }),
    }),

  scriptPrompts: () =>
    request<{ format: ScriptFormat; label: string; default: string; current: string }[]>(
      "/api/script/prompts",
    ),

  /** Download what was just generated; the body round-trips through here. */
  downloadScript: async (body: string, filename: string): Promise<Blob> => {
    const response = await rawRequest("/api/script/file", {
      method: "POST",
      body: JSON.stringify({ body, filename }),
    });
    return response.blob();
  },

  // --- speakers ----------------------------------------------------------

  speakers: (projectId: string) =>
    request<SpeakerInfo[]>(`/api/projects/${projectId}/speakers`),

  createSpeaker: (projectId: string, name: string) =>
    request<SpeakerInfo>(`/api/projects/${projectId}/speakers`, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  renameSpeaker: (projectId: string, localId: number, name: string) =>
    request<SpeakerInfo>(`/api/projects/${projectId}/speakers/${localId}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),

  deleteSpeaker: (projectId: string, localId: number) =>
    request<void>(`/api/projects/${projectId}/speakers/${localId}`, { method: "DELETE" }),

  /** Point sentences at a speaker; `null` clears the annotation. */
  assignSpeaker: (versionId: string, sentenceOrds: number[], localId: number | null) =>
    request<{ updated: number; local_id: number | null }>(
      `/api/versions/${versionId}/speakers/assign`,
      {
        method: "POST",
        body: JSON.stringify({ sentence_ords: sentenceOrds, local_id: localId }),
      },
    ),

  // --- video -------------------------------------------------------------

  /** `null` means "this project has no template yet", which is a normal state. */
  videoTemplate: (projectId: string) =>
    request<VideoTemplate | null>(`/api/projects/${projectId}/video/template`),

  importVideoTemplate: (projectId: string, path: string) =>
    request<VideoTemplate>(`/api/projects/${projectId}/video/template`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  recompileVideoTemplate: (projectId: string) =>
    request<VideoTemplate>(`/api/projects/${projectId}/video/template/recompile`, {
      method: "POST",
    }),

  /** Every family this machine can draw, including 字由's own store. */
  fonts: () => request<{ families: string[]; preferred: string[]; faces: number }>("/api/fonts"),

  /** Override one text slot. An empty string clears that field back to the PSD's value. */
  editVideoText: (
    projectId: string,
    key: string,
    edit: { text?: string; font?: string; colour?: string; font_size?: number },
  ) =>
    request<VideoTemplate>(
      `/api/projects/${projectId}/video/template/texts/${encodeURIComponent(key)}`,
      { method: "PATCH", body: JSON.stringify(edit) },
    ),

  /** Replace one avatar's photo; `null` restores the PSD's own. */
  setVideoAvatarPhoto: (projectId: string, slot: string, assetId: string | null) =>
    request<VideoTemplate>(
      `/api/projects/${projectId}/video/template/avatars/${encodeURIComponent(slot)}/photo`,
      { method: "PUT", body: JSON.stringify({ asset_id: assetId }) },
    ),

  updateVideoTemplate: (projectId: string, boxFitDefault: string) =>
    request<VideoTemplate>(`/api/projects/${projectId}/video/template`, {
      method: "PATCH",
      body: JSON.stringify({ box_fit_default: boxFitDefault }),
    }),

  videoTimeline: (versionId: string) =>
    request<VideoTimeline>(`/api/versions/${versionId}/video/timeline`),

  // --- asset library (P2) ------------------------------------------------

  videoAssets: (projectId: string) =>
    request<VideoAsset[]>(`/api/projects/${projectId}/video/assets`),

  /** Import from a path on this machine — the same shape the PSD import uses. */
  importVideoAsset: (projectId: string, path: string) =>
    request<VideoAsset>(`/api/projects/${projectId}/video/assets`, {
      method: "POST",
      body: JSON.stringify({ path }),
    }),

  /**
   * Import bytes dragged into the browser.
   *
   * Uploaded rather than referenced because a `File` has no path: the browser hands over
   * a blob, and pretending otherwise would mean asking the user to type a filesystem path
   * for something they can see on screen.
   */
  uploadVideoAsset: async (projectId: string, file: File): Promise<VideoAsset> => {
    const form = new FormData();
    form.append("file", file, file.name);
    // Deliberately not `rawRequest` + a JSON content type: multipart sets its own
    // boundary, and overriding it makes the server see an unparseable body.
    const response = await fetch(`/api/projects/${projectId}/video/assets/upload`, {
      method: "POST",
      body: form,
    });
    if (!response.ok) throw await failure(response, `上传 ${file.name} 失败`);
    return (await response.json()) as VideoAsset;
  },

  // --- deck (P4, AGENTS.md §19.6) -------------------------------------

  /** The project's deck, or `null` before one has been generated. */
  deck: (projectId: string) => request<Deck | null>(`/api/projects/${projectId}/deck`),

  /**
   * Plan the deck's pages.
   *
   * `plan` (default) asks the model where the topics turn and what each stretch argues;
   * `transcript` cuts the transcript at pauses, which is free and instant and produces the
   * teleprompter this round replaced. The server falls back to `transcript` by itself when no
   * key is configured or the call fails, and says so in `warnings`.
   *
   * `chapters` is passed in rather than read server-side because chapters are never
   * persisted: they come back from the ShowNotes run and live only in the UI's memory.
   */
  generateDeck: (
    versionId: string,
    options: {
      replace?: boolean;
      chapters?: { heading: string; start_ms: number }[];
      mode?: "plan" | "transcript";
    } = {},
  ) =>
    request<Deck>(`/api/versions/${versionId}/deck/generate`, {
      method: "POST",
      body: JSON.stringify({
        replace: options.replace ?? true,
        chapters: options.chapters ?? [],
        mode: options.mode ?? "plan",
      }),
    }),

  updateSlide: (
    slideId: string,
    body: { title?: string; body?: string; layout?: string; start_ms?: number },
  ) =>
    request<DeckSlide>(`/api/slides/${slideId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  /**
   * The families this deck can be set in — Chinese-capable only.
   *
   * Separate from `api.fonts()` on purpose: that one is the template tab's list, which has to
   * include decorative Latin-only faces (the poster's episode number is digits). Every
   * character on a slide is Chinese, so a face without the glyphs draws empty boxes.
   */
  deckFonts: (projectId: string) =>
    request<{ families: string[]; title_font: string; body_font: string }>(
      `/api/projects/${projectId}/deck/fonts`,
    ),

  /**
   * Set the deck's title and body typefaces.
   *
   * Deck-wide, not per slide: type is what makes a deck look like one document. Omitting a
   * field leaves it alone; passing `""` puts it back to the default.
   */
  setDeckFonts: (
    projectId: string,
    body: { title_font?: string; body_font?: string },
  ) =>
    request<Deck>(`/api/projects/${projectId}/deck`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  moveSlide: (slideId: string, toOrd: number) =>
    request<DeckSlide[]>(`/api/slides/${slideId}/move`, {
      method: "POST",
      body: JSON.stringify({ to_ord: toOrd }),
    }),

  deleteSlide: (slideId: string) =>
    request<void>(`/api/slides/${slideId}`, { method: "DELETE" }),

  /**
   * The page's PNG. The server draws it on demand at the media box's size, so the film never
   * rescales it — a resampled slide is the one thing that must not happen to text.
   */
  slideImageUrl: (imageUrl: string, width: number, height: number) =>
    `${imageUrl}?width=${Math.round(width)}&height=${Math.round(height)}`,

  deleteVideoAsset: (assetId: string) =>
    request<void>(`/api/video/assets/${assetId}`, { method: "DELETE" }),

  addVideoItem: (
    versionId: string,
    body: {
      kind?: string;
      start_ms: number;
      end_ms: number;
      fit?: string;
      snap?: boolean;
      asset_id?: string;
      deck_id?: string;
      slide_id?: string;
      gallery_json?: string;
      /** `true` (silent) is the server default; only pass it to create a speaking block. */
      muted?: boolean;
      solo?: boolean;
      placement?: string;
    },
  ) =>
    request<VideoItem>(`/api/versions/${versionId}/video/items`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  patchVideoItem: (
    itemId: string,
    body: {
      kind?: string;
      start_ms?: number;
      end_ms?: number;
      fit?: string;
      asset_id?: string;
      gallery_json?: string;
      snap?: boolean;
      muted?: boolean;
      solo?: boolean;
      placement?: string;
    },
  ) =>
    request<VideoItem>(`/api/video/items/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deleteVideoItem: (itemId: string) =>
    request<void>(`/api/video/items/${itemId}`, { method: "DELETE" }),

  /** Every avatar slot with its speaker and how much of the film it talks over (P3). */
  videoAvatars: (versionId: string) =>
    request<VideoAvatar[]>(`/api/versions/${versionId}/video/avatars`),
  /**
   * Bind one slot to a speaker, or clear it with `null`.
   *
   * `versionId` is required even though the binding is project-wide: the *talk time* the
   * response reports is measured on one version, and without it the API would have to
   * guess which transcript the user is looking at.
   */
  bindVideoAvatar: (projectId: string, slot: string, versionId: string, speakerId: number | null) =>
    request<VideoAvatar>(
      `/api/projects/${projectId}/video/avatars/${encodeURIComponent(slot)}` +
        `?version_id=${encodeURIComponent(versionId)}`,
      { method: "PUT", body: JSON.stringify({ speaker_id: speakerId }) },
    ),

  /**
   * The whole film as one description, for the real-time DOM stage (P1, §19.31.2).
   *
   * One request instead of one per instant: 167 KB of geometry that only changes when the project
   * does. The style is a query parameter for the same reason the still preview's was — the user
   * drags the 字号 slider *while deciding* — and it is part of the digest, so a change renames
   * every baked band rather than showing a stale one.
   */
  videoPlan: (
    versionId: string,
    options: {
      font_size?: number;
      margin_v?: number;
      burn_subtitles?: boolean;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (options.font_size !== undefined) params.set("font_size", String(options.font_size));
    if (options.margin_v !== undefined) params.set("margin_v", String(options.margin_v));
    if (options.burn_subtitles === false) params.set("burn_subtitles", "false");
    const query = params.toString();
    return request<VideoPlan>(
      `/api/versions/${versionId}/video/plan${query ? `?${query}` : ""}`,
    );
  },

  /** One composited frame at `atMs` on the **output** timeline. */
  videoPreview: (
    versionId: string,
    atMs: number,
    style: { font_name?: string; font_size?: number; margin_v?: number } = {},
  ) => {
    const params = new URLSearchParams({ at_ms: String(Math.max(0, Math.round(atMs))) });
    if (style.font_name) params.set("font_name", style.font_name);
    if (style.font_size !== undefined) params.set("font_size", String(style.font_size));
    if (style.margin_v !== undefined) params.set("margin_v", String(style.margin_v));
    return request<{ url: string; at_ms: number }>(
      `/api/versions/${versionId}/video/preview?${params.toString()}`,
    );
  },

  videoSubtitleStyle: () =>
    request<{
      font_name: string;
      font_size: number;
      margin_v: number;
      primary_colour: string;
      outline: number;
      bold: boolean;
    }>("/api/video/subtitle-style"),

  previewUrl: (projectId: string, name: string) =>
    `/api/video/preview/file/${encodeURIComponent(name)}?project_id=${encodeURIComponent(projectId)}`,

  exportVideo: (
    versionId: string,
    options: {
      burn_subtitles?: boolean;
      font_name?: string;
      font_size?: number;
      margin_v?: number;
    } = {},
  ) =>
    request<VideoExport>(`/api/versions/${versionId}/video/export`, {
      method: "POST",
      body: JSON.stringify(options),
    }),

  videoExport: (exportId: string) => request<VideoExport>(`/api/video/exports/${exportId}`),

  /**
   * The finished film, as bytes.
   *
   * A plain `fetch` rather than `request()`: the body is an MP4, not JSON. The URL the
   * status endpoint returns is already same-origin (``/api/video/exports/{id}/file``), so
   * the browser's `download` attribute can name the file and no server header is involved.
   */
  videoExportBlob: async (url: string): Promise<Blob> => {
    const response = await fetch(url);
    if (!response.ok) throw await failure(response, "下载成片视频失败");
    return response.blob();
  },
};

export const mediaIdFromVersion = (versionId: string): string => versionId;

/** ShowNotes has no style of its own; 文稿 has two. */
export type ScriptFormat = "shownotes" | "dialogue" | "article";

export interface AppSettings {
  asr_model: string;
  llm_model: string;
  hotword_weight: number;
  use_hotwords: boolean;
  diarization: boolean;
  speaker_count: number;
  use_proofread: boolean;
  proofread_chunk_size: number;
  cleanup_fillers: string;
  cleanup_min_silence_ms: number;
  cleanup_silence_pad_ms: number;
  cleanup_min_disfluency_ms: number;
  silence_noise_db: number;
  silence_min_duration_ms: number;
  snap_to_silence: boolean;
  guard_ms: number;
  min_keep_ms: number;
  fade_ms: number;
  realtime_skip: boolean;
  export_format: "m4a" | "wav";
  export_bitrate: "128k" | "192k" | "256k" | "320k";
  subtitle_cps: number;
  subtitle_max_line: number;
  subtitle_max_lines: number;
  script_format: ScriptFormat;
  shownotes_signature: string;
  script_prompt_shownotes: string;
  script_prompt_dialogue: string;
  script_prompt_article: string;
  text_temperature: number;
  text_max_tokens: number;
}

export interface CleanupItem {
  kind: "filler" | "disfluency" | "silence";
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  text: string;
  token_indices: number[];
  sentence_ord: number;
}

export interface CleanupPlan {
  version_id: string;
  duration_ms: number;
  items: CleanupItem[];
  summary: Record<string, { items: number; duration_ms: number }>;
  fillers: string[];
  kinds: string[];
}

export interface HotwordItem {
  text: string;
  weight: number;
  lang: string;
  enabled: boolean;
  hit_count: number;
  miss_count: number;
}

export interface HistoryEntry {
  id: string;
  seq: number;
  op_type: string;
  label: string;
  detail: string;
  created_at: string;
  undone: boolean;
  undoable: boolean;
}

// --- video (AGENTS.md §19) ----------------------------------------------

export interface VideoRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface VideoAvatarSlot {
  name: string;
  /** What the user calls it: 「主播1」/「嘉宾2」, derived from which side it sits on. */
  label_name: string;
  rect: VideoRect;
  photo: VideoRect;
  /** The PSD's own name plate text — now only the **default** for this slot's name. */
  label: string;
  lit_path: string;
  dim_path: string;
  lit_url: string;
  dim_url: string;
  /** Key of this slot's name text slot (`avatar:<group>`), for editing it. */
  name_key: string;
  /** True once the user replaced this slot's photo. */
  photo_replaced: boolean;
}

/** One piece of text on the poster: 期号 / 当期标题 / 头像名 / a note. */
export interface VideoTextSlot {
  /** Stable across recompiles; this is what an override is stored under. */
  key: string;
  role: "episode" | "title" | "avatar_name" | "note" | "content";
  /** 「期号」/「当期标题」/「头像名 · 头像1」/the PSD layer's own name. */
  label: string;
  name: string;
  rect: VideoRect;
  text: string;
  font_size: number;
  /** `#rrggbb`, measured from the PSD rather than guessed. */
  colour: string;
  font: string;
}

/** One avatar panel slot with its speaker (P3). */
export interface VideoAvatar {
  name: string;
  label: string;
  x: number;
  y: number;
  lit_url: string;
  dim_url: string;
  speaker_id: number | null;
  speaker_name: string;
  /** How many separate stretches this slot is lit for. */
  turns: number;
  lit_ms: number;
  /** Share of the film's output length, 0–1. */
  share: number;
}

/** One stretch of box content, on the output clock (P1, §19.31.2). */
export interface PlanSegment {
  start_ms: number;
  end_ms: number;
  kind: string;
  fit: string;
  placement: string;
  loop: boolean;
  url: string;
  backdrop_url: string | null;
  /** A video whose blurred backdrop is a still: the preview's background cannot move. */
  backdrop_frozen: boolean;
  /** The period a browser's own `loop` repeats on, in ms. 0 when the segment does not loop. */
  period_ms: number;
}

export interface PlanCue {
  start_ms: number;
  end_ms: number;
  index: number;
  /** The band's top and height on the canvas, known **before** the PNG is baked. */
  top: number;
  height: number;
  url: string;
}

export interface PlanAvatar {
  name: string;
  rect: VideoRect;
  lit_url: string;
  dim_url: string;
  intervals: { start_ms: number; end_ms: number }[];
}

/**
 * The film as one description: what the DOM stage draws (§19.31.2).
 *
 * Deliberately parallel to `videoPlan.ts`'s `VideoPlan` rather than importing it: `api.ts` is the
 * HTTP contract and must not depend on the drawing module, or a change to how a layer is
 * positioned would look like a change to the wire format.
 */
export interface VideoPlan {
  digest: string;
  version_id: string;
  canvas: { w: number; h: number };
  box: VideoRect;
  output_duration_ms: number;
  background_url: string;
  overlay_url: string | null;
  text_url: string | null;
  segments: PlanSegment[];
  avatars: PlanAvatar[];
  cues: PlanCue[];
  unsupported: string[];
}

export interface VideoTemplate {
  id: string;
  status: "pending" | "compiling" | "ready" | "failed";
  error: string;
  canvas_w: number;
  canvas_h: number;
  box_rect: VideoRect;
  box_fit_default: string;
  background_url: string;
  overlay_url: string;
  slots: {
    avatars?: VideoAvatarSlot[];
    /** Resolved: the PSD's defaults with the user's edits already applied. */
    texts?: VideoTextSlot[];
    notes?: string[];
  };
  compiled_at: string | null;
}

export interface VideoItem {
  id: string;
  kind: "media" | "gallery" | "slide";
  /**
   * What the block is called: the imported file's own name, the deck's title, or the first
   * gallery member's name. Derived by the server on read — renaming a deck must change every
   * block of it, which a copied column could not do. Empty when nothing names the block.
   */
  label: string;
  /** Original-timeline edges, snapped to a sentence start. */
  start_ms: number;
  end_ms: number;
  /** Length of the block's own edges — **not** what it plays for. */
  duration_ms: number;
  /** Length after the audio cuts are applied: the block plays continuously this long. */
  output_ms: number;
  z: number;
  fit: string;
  motion: string;
  asset_id: string | null;
  deck_id: string | null;
  slide_id: string | null;
  gallery_json: string;
  orphaned: boolean;
  /**
   * Whether the block's own sound is silenced (§19.19).
   *
   * `true` by default — the film's audio is the podcast, and a block's sound is something the
   * user asks for. So an untouched project sounds exactly as it did before the feature.
   */
  muted: boolean;
  /**
   * 「S」: the block's own sound **replaces** the podcast over its stretch instead of being mixed
   * under it (§19.24). Mutually exclusive with `muted` — the server clears whichever was pressed
   * last, so `muted && solo` is never stored.
   */
  solo: boolean;
  /**
   * `box` draws the block inside the template's material box (the default); `full` fills the
   * whole canvas (§19.25).
   */
  placement: string;
  /**
   * Whether unmuting would bring sound back: `false` for a still, a deck, or a silent clip.
   *
   * Computed on the server from `video_asset.has_audio`, because the timeline payload carries
   * no asset rows and the button must not be offered where it would be a lie.
   */
  has_audio: boolean;
}

/** One page of the deck (P4). */
export interface DeckSlide {
  id: string;
  ord: number;
  title: string;
  body: string;
  /** `title` | `title_body` | `quote` */
  layout: string;
  /** Sentence ord this page came from — what the editor jumps to. */
  source_ord: number;
  /** Original-timeline start; a slide block snaps to this. */
  start_ms: number;
  image_url: string;
}

export interface Deck {
  id: string;
  project_id: string;
  title: string;
  slides: DeckSlide[];
  /** `model` when the pages were planned by the outline call, `transcript` for the fallback. */
  source: string;
  /** How the last generation had to repair the plan. Empty for a clean run. */
  warnings: string[];
  /** Resolved, never empty: an unset deck reports the renderer's own default. */
  title_font: string;
  body_font: string;
}

export interface VideoTimeline {
  grid: { boundaries: number[]; targets: number[]; duration_ms: number };
  items: VideoItem[];
  original_duration_ms: number;
  output_duration_ms: number;
  removed_ms: number;
}

export interface VideoExport {
  id: string;
  status: "queued" | "running" | "done" | "failed";
  progress: number;
  duration_ms: number;
  url: string;
  error: string | null;
}

export interface VideoAsset {
  id: string;
  kind: "image" | "video";
  name: string;
  sha256: string;
  width: number;
  height: number;
  /** 0 for images: they have no intrinsic length. */
  duration_ms: number;
  created_at: string;
  url: string;
  /** False when the file was deleted outside the app. */
  exists: boolean;
}
