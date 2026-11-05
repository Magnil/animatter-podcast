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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let detail: ApiError = {
      code: "unknown",
      message: `请求失败（HTTP ${response.status}）`,
      hint: "",
    };
    try {
      const body = (await response.json()) as { error?: ApiError; detail?: string };
      if (body.error) detail = body.error;
      else if (body.detail) detail = { code: "unknown", message: body.detail, hint: "" };
    } catch {
      // Non-JSON error body: keep the generic message.
    }
    throw new RequestFailed(response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
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
    return request<{ id: string; duration_ms: number; status: string; url: string }>(
      `/api/versions/${versionId}/render${query ? `?${query}` : ""}`,
      { method: "POST" },
    );
  },

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

  addEpisodeHotword: (projectId: string, mediaId: string, text: string, weight = 3) =>
    request<{ accepted: HotwordItem[] }>(`/api/projects/${projectId}/media/${mediaId}/hotwords`, {
      method: "POST",
      body: JSON.stringify([{ text, weight }]),
    }),

  removeEpisodeHotword: (projectId: string, mediaId: string, text: string) =>
    request<void>(
      `/api/projects/${projectId}/media/${mediaId}/hotwords/${encodeURIComponent(text)}`,
      { method: "DELETE" },
    ),

  suggestHotwords: (projectId: string, mediaId: string) =>
    request<{ terms: HotwordItem[]; warnings: string[] }>(
      `/api/projects/${projectId}/media/${mediaId}/hotwords/suggest`,
      { method: "POST" },
    ),

  saveOutline: (projectId: string, mediaId: string, content: string) =>
    request<Record<string, unknown>>(`/api/projects/${projectId}/media/${mediaId}/outline`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    }),

  undo: (versionId: string) =>
    request<{ undone: string; op_type: string }>(`/api/versions/${versionId}/undo`, {
      method: "POST",
    }),

  audioUrl: (projectId: string, mediaId: string) =>
    `/api/projects/${projectId}/media/${mediaId}/audio`,
};

export const mediaIdFromVersion = (versionId: string): string => versionId;

export interface AppSettings {
  asr_model: string;
  llm_model: string;
  hotword_weight: number;
  use_hotwords: boolean;
  diarization: boolean;
  speaker_count: number;
  proofread_chunk_size: number;
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
