/**
 * Compact a DSH session log so a provider with an *item* limit will accept it.
 *
 * The problem this solves
 * -----------------------
 * OpenDesign GO caps a request at 1000 input items and rejects anything larger:
 *
 *   Input is too long: conversation history exceeds the limit on input items
 *   (1002 > 1000 items)
 *
 * DSH normally handles this itself: it summarises the older part of the
 * conversation and records the result as a `compaction/summary` row whose
 * `shadowedSeqs` lists the exact items it replaced. That is why the summary row
 * carries `shadowedRange` / `shadowedSeqs` / `shadowedTokenCount` — the shadow
 * list *is* the item list.
 *
 * But compaction needs an LLM call over the history it is about to summarise,
 * and that call is itself a request — with 1002 items. So the session hits a
 * deadlock: too big to compact, and compaction is the only way to get smaller.
 * The failure is recorded verbatim as
 * `compaction/end ... error: OpenDesign rejected the request`.
 *
 * What this script does that is safe
 * ----------------------------------
 * The log is append-only: one zstd frame per record (a 8 MB file is 7000+
 * frames). So this script does not rewrite anything. It appends three rows in
 * the exact shape DSH writes itself — `compaction/start`, `compaction/summary`,
 * `compaction/end` — where the summary row shadows the oldest live items and
 * carries a written-down summary in their place.
 *
 * No records are deleted, no bytes are rewritten, and the whole thing is undone
 * by restoring one backup file. That matters: this touches the file the user's
 * entire conversation lives in.
 *
 * Usage
 * -----
 *   node scripts/compact-session.mjs                 # dry run: report only
 *   node scripts/compact-session.mjs --apply         # append the compaction
 *   node scripts/compact-session.mjs --apply --keep-turns 2 --summary notes.md
 *
 * Run it with DSH closed. The running app holds the session in memory and keeps
 * appending its own rows with its own sequence counter; appending underneath it
 * would eventually collide. The freshness guard below refuses by default.
 */
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import zlib from "node:zlib";

/** The provider's hard limit, from the error message. Kept as a budget target. */
const ITEM_LIMIT = 1000;
/** Aim well under it so the next few turns do not immediately re-hit the wall. */
const TARGET_ITEMS = 400;
/** Refuse to touch a file written this recently: the app is almost certainly open. */
const FRESH_MS = 90_000;

/** Records that become one "input item" in the provider request. */
const ITEM_TYPES = new Set(["user/message", "assistant/message", "tool/result", "system/message"]);

const args = process.argv.slice(2);
const flag = (name) => args.includes(`--${name}`);
const value = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};

const apply = flag("apply");
const force = flag("force");
const keepTurns = Number(value("keep-turns", "2"));
const summaryFile = value("summary", "");
const sessionArg = value("session", "");

function main() {
  const file = sessionArg ? path.resolve(sessionArg) : locateSession();
  console.log(`session: ${file}`);

  const stat = fs.statSync(file);
  const ageMs = Date.now() - stat.mtimeMs;
  if (ageMs < FRESH_MS && !force) {
    console.error(
      `refusing: the file changed ${Math.round(ageMs / 1000)}s ago, so DSH is probably still running.\n` +
        `  Quit DSH, then run again. (--force overrides, but do not: a live app appends with its own seq counter.)`,
    );
    process.exit(2);
  }

  const records = readRecords(file);
  const shadowed = shadowedSeqs(records);
  const live = records.filter((r) => ITEM_TYPES.has(r.type) && !shadowed.has(r.seq));
  console.log(`total records: ${records.length}`);
  console.log(`live input items: ${live.length}  (provider limit ${ITEM_LIMIT})`);

  if (live.length <= TARGET_ITEMS) {
    console.log(`already under the ${TARGET_ITEMS}-item target; nothing to do.`);
    return;
  }

  const cut = findCut(records, live, keepTurns);
  if (cut === null) {
    console.error("could not find a turn boundary to cut at; leaving the file alone.");
    process.exit(1);
  }

  const toShadow = live.filter((r) => r.seq < cut.seq);
  const kept = live.filter((r) => r.seq >= cut.seq);
  console.log(
    `\ncut before turn ${cut.turn} (seq ${cut.seq}):\n` +
      `  shadow ${toShadow.length} items, keep ${kept.length} items`,
  );
  if (kept.length > TARGET_ITEMS) {
    console.error(
      `\nkeeping ${kept.length} items still exceeds the ${TARGET_ITEMS} target; raise --keep-turns? no — lower it.`,
    );
    process.exit(1);
  }

  const summary = summaryFile ? fs.readFileSync(summaryFile, "utf8").trim() : autoSummary(records, toShadow);
  console.log(`\nsummary: ${summary.length} chars`);

  if (!apply) {
    console.log("\n--- summary preview (first 1200 chars) ---");
    console.log(summary.slice(0, 1200));
    console.log(`\ndry run. Re-run with --apply to append the compaction.`);
    return;
  }

  const backup = `${file}.bak-${new Date().toISOString().replace(/[:.]/g, "-")}`;
  fs.copyFileSync(file, backup);
  console.log(`\nbackup: ${backup}`);

  const maxSeq = records.reduce((max, r) => (typeof r.seq === "number" && r.seq > max ? r.seq : max), 0);
  const now = Date.now();
  const compactionId = randomId();
  const tokenEstimate = toShadow.reduce((sum, r) => sum + Math.ceil(JSON.stringify(r).length / 3), 0);

  const rows = [
    {
      type: "compaction/start",
      seq: maxSeq + 1,
      time: now,
      data: { compactionId, turn: cut.turn },
    },
    {
      type: "compaction/summary",
      seq: maxSeq + 2,
      time: now + 1,
      data: {
        compactionId,
        summary: [{ type: "text", text: summary }],
        // Written by this script rather than an LLM, so record that honestly
        // instead of claiming a model wrote it.
        rawOutput: "",
        llmStreamCall: "",
        shadowedRange: { start: toShadow[0].seq, end: toShadow[toShadow.length - 1].seq },
        shadowedSeqs: toShadow.map((r) => r.seq),
        shadowedTokenCount: tokenEstimate,
        provider: "manual-compaction",
        model: "manual-compaction",
        maxTokens: 0,
        usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 },
      },
    },
    {
      type: "compaction/end",
      seq: maxSeq + 3,
      time: now + 2,
      data: { compactionId, turn: cut.turn },
    },
  ];

  appendRows(file, rows);
  verify(file, records.length + rows.length);

  console.log(`\nappended 3 rows (seq ${maxSeq + 1}..${maxSeq + 3}).`);
  console.log("Now reopen DSH (or reload the session) so it rebuilds context from disk.");
  console.log(`To undo: copy ${path.basename(backup)} back over the session file.`);
}

/** The session directory is per working directory; find ours by the env id. */
function locateSession() {
  const id = process.env.DSH_SESSION_ID;
  if (!id) throw new Error("DSH_SESSION_ID is not set; pass --session <path> instead");
  const roots = [
    path.join(os.homedir(), ".dsh", "sessions"),
  ];
  for (const root of roots) {
    if (!fs.existsSync(root)) continue;
    for (const dir of fs.readdirSync(root)) {
      const candidate = path.join(root, dir, id, "session.v4.jsonl.zstd");
      if (fs.existsSync(candidate)) return candidate;
    }
  }
  throw new Error(`could not find ${id} under ${roots.join(", ")}; pass --session <path>`);
}

/** The log is a concatenation of zstd frames, one JSON row each. */
function readRecords(file) {
  const raw = fs.readFileSync(file);
  const text = zlib.zstdDecompressSync(raw).toString("utf8");
  return text
    .split("\n")
    .filter((line) => line.trim())
    .map((line, index) => {
      try {
        return JSON.parse(line);
      } catch (error) {
        throw new Error(`row ${index + 1} is not valid JSON: ${error.message}`);
      }
    });
}

function shadowedSeqs(records) {
  const set = new Set();
  for (const record of records) {
    if (record.type !== "compaction/summary" && record.type !== "compaction/prune") continue;
    for (const seq of record.data?.shadowedSeqs ?? []) set.add(seq);
  }
  return set;
}

/** The turn/start row that begins the last `keepTurns` turns that are still live. */
function findCut(records, live, keepTurns) {
  const liveSeqs = new Set(live.map((r) => r.seq));
  let turn = 0;
  const starts = [];
  for (const record of records) {
    if (record.type === "turn/start") turn += 1;
    // Only count a turn if something in it is still live — an entirely shadowed
    // turn must not consume one of the "keep last N" slots.
    if (record.type === "turn/start" && live.some((r) => r.seq >= record.seq && r.data?.turn === turn)) {
      starts.push({ seq: record.seq, turn });
    }
  }
  void liveSeqs;
  if (starts.length === 0) return null;
  const index = Math.max(0, starts.length - Math.max(1, keepTurns));
  return starts[index];
}

/**
 * A summary for the shadowed range, built from the rows themselves.
 *
 * Deliberately factual: the user messages carry the actual intent, and the file
 * list carries what changed. Inventing prose about work the model did not see is
 * how a compaction quietly rewrites history.
 */
function autoSummary(records, toShadow) {
  const seqs = new Set(toShadow.map((r) => r.seq));
  const users = records
    .filter((r) => r.type === "user/message" && seqs.has(r.seq))
    .map((r) => extractText(r.data?.message?.content))
    .filter(Boolean);

  const files = new Set();
  for (const record of records) {
    if (!seqs.has(record.seq)) continue;
    for (const part of record.data?.message?.content ?? []) {
      const text = part?.text ?? "";
      for (const match of text.matchAll(/\b(?:apps|scripts)\/[\w./-]+\.(?:ts|tsx|py|mjs|css|md)\b/g)) {
        files.add(match[0]);
      }
    }
  }

  const lines = ["<compacted-summary>", "", "## 这段对话里用户提出的要求（原文摘录）", ""];
  for (const message of users.slice(-8)) {
    lines.push(`- ${message.replace(/\s+/g, " ").slice(0, 400)}`);
  }
  if (files.size > 0) {
    lines.push("", "## 这段对话里动过的文件", "");
    for (const file of [...files].sort().slice(0, 40)) lines.push(`- ${file}`);
  }
  lines.push(
    "",
    "## 说明",
    "",
    "这段历史是人工压缩的（不是模型写的）：原因是一次请求的输入条目数超过了所用 provider 的",
    "1000 条上限，而 DSH 自动压缩本身也要发一次同样大的请求，于是卡死。被压缩区间里的原始记录",
    "仍完整保存在 session 日志里，只是不再进入上下文。",
    "</compacted-summary>",
  );
  return lines.join("\n");
}

function extractText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => (typeof part === "string" ? part : part?.text ?? ""))
    .filter(Boolean)
    .join("\n");
}

/** Append one zstd frame per row, matching the file's existing framing. */
function appendRows(file, rows) {
  const chunks = rows.map((row) => zlib.zstdCompressSync(Buffer.from(`${JSON.stringify(row)}\n`, "utf8")));
  fs.appendFileSync(file, Buffer.concat(chunks));
}

/** Re-read and prove the compaction did what it claimed. */
function verify(file, expectedRows) {
  const records = readRecords(file);
  if (records.length !== expectedRows) {
    throw new Error(`row count is ${records.length}, expected ${expectedRows}`);
  }
  const shadowed = shadowedSeqs(records);
  const live = records.filter((r) => ITEM_TYPES.has(r.type) && !shadowed.has(r.seq));
  console.log(`verified: ${records.length} rows parse, live input items now ${live.length}`);
  if (live.length >= ITEM_LIMIT) throw new Error(`still ${live.length} items — compaction did not help`);
}

function randomId() {
  try {
    return execFileSync("node", ["-e", "console.log(crypto.randomUUID())"], { encoding: "utf8" }).trim();
  } catch {
    return `manual-${Date.now().toString(36)}`;
  }
}

main();
