/**
 * The whole-file waveform envelope, held in memory.
 *
 * Why the client now holds every peak instead of fetching a window: the old
 * design issued one request per zoom notch, and on a real 105-minute episode
 * that measured 121–152 ms per notch. Decoding and drawing were never the
 * problem (978 canvas columns ≈ 1.5 ms); waiting for the network was. So the
 * envelope arrives once, and zoom/pan become pure arithmetic with no I/O at all.
 *
 * Size is why this is affordable: the backend caps the envelope at 2M buckets
 * and ships signed bytes, so a two-hour file is 4 MB — not the 16 MB it would
 * be as floats, and not the 143 MB the old multi-level cache held on disk.
 *
 * The `min`/`max` arrays are a **view** on the downloaded buffer, not copies.
 * `Int8Array` over the same `ArrayBuffer` is what keeps 4 MB from becoming
 * 8 MB; the `byteOffset`/`length` arguments are what make that possible.
 */

/** Mip factors, finest first. Each level is the extrema of `factor` buckets. */
const MIP_FACTORS = [1, 16, 256] as const;

/** `PKE1` header: magic(4) version(u16) count(u32) duration(i32) rate(u32). */
const HEADER_BYTES = 18;
const MAGIC = "PKE1";

export interface EnvelopeLevel {
  /** How many base buckets this level folds together. */
  factor: number;
  min: Int8Array;
  max: Int8Array;
}

export interface Envelope {
  durationMs: number;
  /** Milliseconds covered by one base bucket. */
  bucketMs: number;
  /** Level 0 is the raw envelope; later levels are coarser summaries. */
  levels: EnvelopeLevel[];
}

export class EnvelopeFormatError extends Error {}

/**
 * Parse one `PKE1` blob.
 *
 * Every length is checked against the buffer before it is trusted: a truncated
 * download must throw, because a short envelope would draw a waveform that
 * silently stops matching the audio at some point in the file — and the user's
 * next action is to trust it while cutting.
 */
export function parseEnvelope(buffer: ArrayBuffer): Envelope {
  if (buffer.byteLength < HEADER_BYTES) {
    throw new EnvelopeFormatError(`波形数据太短（${buffer.byteLength} 字节）`);
  }
  const view = new DataView(buffer);
  const magic = String.fromCharCode(
    view.getUint8(0),
    view.getUint8(1),
    view.getUint8(2),
    view.getUint8(3),
  );
  if (magic !== MAGIC) throw new EnvelopeFormatError(`波形数据格式不对（${magic}）`);

  const version = view.getUint16(4, true);
  if (version !== 1) throw new EnvelopeFormatError(`波形数据版本不支持（${version}）`);

  const count = view.getUint32(6, true);
  const durationMs = view.getInt32(10, true);
  const expected = HEADER_BYTES + count * 2;
  if (buffer.byteLength < expected) {
    throw new EnvelopeFormatError(
      `波形数据被截断：应为 ${expected} 字节，实际 ${buffer.byteLength}`,
    );
  }

  const baseMin = new Int8Array(buffer, HEADER_BYTES, count);
  const baseMax = new Int8Array(buffer, HEADER_BYTES + count, count);

  return {
    durationMs,
    bucketMs: count > 0 ? durationMs / count : 0,
    levels: buildLevels(baseMin, baseMax),
  };
}

/**
 * Pre-reduce the envelope so a zoomed-out draw does not scan 2M buckets.
 *
 * Without this, drawing the full file walks every bucket once per pixel column
 * (~2064 of them), which is 2M reads per frame — survivable, but it is work
 * that never changes, so it should happen once at load rather than sixty times
 * a second. The levels are *reductions*, not samples, so a loud transient at
 * full zoom-out is still drawn rather than aliased away.
 */
function buildLevels(baseMin: Int8Array, baseMax: Int8Array): EnvelopeLevel[] {
  const levels: EnvelopeLevel[] = [{ factor: 1, min: baseMin, max: baseMax }];
  let prevMin = baseMin;
  let prevMax = baseMax;

  for (const factor of MIP_FACTORS) {
    if (factor === 1) continue;
    const ratio = factor / (levels[levels.length - 1]?.factor ?? 1);
    const count = Math.floor(prevMin.length / ratio);
    if (count < 2) break;
    const min = new Int8Array(count);
    const max = new Int8Array(count);
    for (let i = 0; i < count; i += 1) {
      let lo = 0;
      let hi = 0;
      const from = i * ratio;
      for (let k = 0; k < ratio; k += 1) {
        const a = prevMin[from + k] ?? 0;
        const b = prevMax[from + k] ?? 0;
        if (a < lo) lo = a;
        if (b > hi) hi = b;
      }
      min[i] = lo;
      max[i] = hi;
    }
    levels.push({ factor, min, max });
    prevMin = min;
    prevMax = max;
  }
  return levels;
}

/** The level that keeps one column cheap without hiding the shape. */
export function levelFor(env: Envelope, bucketSpan: number): EnvelopeLevel {
  let chosen = env.levels[0];
  for (const level of env.levels) {
    // Fold only while the reduced span still has several buckets left, so the
    // column still describes a *range* rather than a single sample.
    if (bucketSpan / level.factor >= 4) chosen = level;
  }
  return chosen ?? env.levels[0]!;
}

/**
 * Peak amplitude over `[fromMs, toMs)`, normalised to 0..1.
 *
 * Returns half the peak-to-peak swing, matching what the old float payload
 * meant: `(max - min) / 2 / 127`.
 */
export function peakBetween(env: Envelope, fromMs: number, toMs: number): number {
  if (env.levels.length === 0 || env.bucketMs <= 0) return 0;
  const baseFrom = fromMs / env.bucketMs;
  const baseTo = Math.max(baseFrom, toMs / env.bucketMs);
  const level = levelFor(env, baseTo - baseFrom);
  const count = level.min.length;
  if (count === 0) return 0;

  const first = Math.max(0, Math.min(count - 1, Math.floor(baseFrom / level.factor)));
  const last = Math.max(first, Math.min(count - 1, Math.floor((baseTo - 1) / level.factor)));

  let lo = 0;
  let hi = 0;
  for (let i = first; i <= last; i += 1) {
    const a = level.min[i] ?? 0;
    const b = level.max[i] ?? 0;
    if (a < lo) lo = a;
    if (b > hi) hi = b;
  }
  return Math.min(1, (hi - lo) / 2 / 127);
}

/** True when this envelope can be drawn at all. */
export function hasShape(env: Envelope | null): env is Envelope {
  return env !== null && env.levels.length > 0 && env.levels[0]!.min.length > 0;
}
