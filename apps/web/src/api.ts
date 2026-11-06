/**
 * Typed API client.
 *
 * Every call surfaces the backend's `AppError(code, message, hint)` shape rather
 * than a bare status, because the hint is the actionable part (AGENT.md §8).
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
   * episode list wholesale (AGENT.md §6.1), so after dropping the wrong
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

  /** Bring back a whole category ("还原口癖词") or one batch. */
  restoreCleanup: (versionId: string, options: { source?: string; batch?: string }) => {
    const params = new URLSearchParams();
    if (options.source) params.set("source", options.source);
    if (options.batch) params.set("batch", options.batch);
    return request<{ restored: number; source: string | null; batch: string | null }>(
      `/api/versions/${versionId}/cleanup/restore?${params}`,
      { method: "POST" },
    );
  },

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
};

export const mediaIdFromVersion = (versionId: string): string => versionId;

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
