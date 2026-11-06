/**
 * The whole-file waveform envelope, from bytes to drawable amplitude.
 *
 * These tests exist because a waveform that is *slightly* wrong is worse than
 * one that is missing: the user cuts on what they see. The two failure modes
 * worth pinning are a truncated payload (draws a short waveform that silently
 * stops matching the audio) and a bad peak reduction (an off-by-one in the mip
 * fold hides a transient, so a loud syllable looks silent).
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  EnvelopeFormatError,
  hasShape,
  levelFor,
  parseEnvelope,
  peakBetween,
} from "./envelope.ts";

/** Build a PKE1 blob the way the backend's `serialize()` does. */
function makeBlob({ durationMs = 1000, rate = 16000, min = [], max = [] } = {}) {
  const count = min.length;
  const buffer = new ArrayBuffer(18 + count * 2);
  const view = new DataView(buffer);
  view.setUint8(0, 0x50); // P
  view.setUint8(1, 0x4b); // K
  view.setUint8(2, 0x45); // E
  view.setUint8(3, 0x31); // 1
  view.setUint16(4, 1, true);
  view.setUint32(6, count, true);
  view.setInt32(10, durationMs, true);
  view.setUint32(14, rate, true);
  const bytes = new Int8Array(buffer);
  for (let i = 0; i < count; i += 1) bytes[18 + i] = min[i] ?? 0;
  for (let i = 0; i < count; i += 1) bytes[18 + count + i] = max[i] ?? 0;
  return buffer;
}

test("parseEnvelope reads the header", () => {
  const env = parseEnvelope(makeBlob({ durationMs: 2000, min: [0, 0], max: [0, 0] }));
  assert.equal(env.durationMs, 2000);
  assert.equal(env.levels[0].min.length, 2);
  assert.equal(env.bucketMs, 1000);
});

test("parseEnvelope rejects the wrong magic", () => {
  const buffer = makeBlob({ min: [0], max: [0] });
  new DataView(buffer).setUint8(0, 0x58);
  assert.throws(() => parseEnvelope(buffer), EnvelopeFormatError);
});

test("parseEnvelope rejects an unknown version", () => {
  const buffer = makeBlob({ min: [0], max: [0] });
  new DataView(buffer).setUint8(4, 9);
  assert.throws(() => parseEnvelope(buffer), /版本不支持/);
});

test("parseEnvelope rejects a truncated payload", () => {
  // This is the one that would silently misalign: a half-downloaded envelope
  // draws a waveform that matches the audio only up to the truncation point.
  const full = makeBlob({ min: [0, 10, 20], max: [0, 10, 20] });
  assert.throws(() => parseEnvelope(full.slice(0, full.byteLength - 1)), /截断/);
});

test("parseEnvelope rejects a payload shorter than the header", () => {
  assert.throws(() => parseEnvelope(new ArrayBuffer(8)), /太短/);
});

test("min and max are views on the same buffer, not copies", () => {
  // 4 MB must stay 4 MB. If these were copies the envelope would double.
  const buffer = makeBlob({ min: [-5, 7], max: [5, 9] });
  const env = parseEnvelope(buffer);
  assert.equal(env.levels[0].min.buffer, buffer);
  assert.equal(env.levels[0].max.buffer, buffer);
});

test("peakBetween returns half the peak-to-peak swing", () => {
  const env = parseEnvelope(
    makeBlob({ durationMs: 100, min: [-127, -127], max: [127, 127] }),
  );
  // Full scale both ways: swing 254, half of that over 127 is 1.0.
  assert.equal(peakBetween(env, 0, 100), 1);
});

test("peakBetween of a silent stretch is zero", () => {
  const env = parseEnvelope(makeBlob({ durationMs: 100, min: [0, 0], max: [0, 0] }));
  assert.equal(peakBetween(env, 0, 100), 0);
});

test("peakBetween keeps a lone loud bucket visible when the column is wide", () => {
  // The classic aliasing bug: one bucket is loud, its neighbours are silent,
  // and a naive "take the bucket under the pixel centre" reads silence.
  const env = parseEnvelope(
    makeBlob({ durationMs: 100, min: [0, 0, -100, 0, 0], max: [0, 0, 100, 0, 0] }),
  );
  // Ask for the whole file in one column: the transient must survive.
  assert.ok(peakBetween(env, 0, 100) > 0.7);
});

test("peakBetween clamps out-of-range spans instead of throwing", () => {
  const env = parseEnvelope(makeBlob({ durationMs: 100, min: [-10], max: [10] }));
  assert.ok(Number.isFinite(peakBetween(env, -500, 900)));
});

test("peakBetween on an empty envelope returns zero", () => {
  const empty = { durationMs: 0, bucketMs: 0, levels: [{ factor: 1, min: new Int8Array(0), max: new Int8Array(0) }] };
  assert.equal(peakBetween(empty, 0, 100), 0);
});

test("mip levels are built and stay coarser than level 0", () => {
  const min = new Array(4096).fill(0);
  const max = new Array(4096).fill(0);
  max[2000] = 90;
  const env = parseEnvelope(makeBlob({ durationMs: 4096, min, max }));
  assert.ok(env.levels.length > 1, "long envelopes should be pre-reduced");
  for (const level of env.levels.slice(1)) {
    assert.ok(level.min.length < env.levels[0].min.length);
  }
  // The transient at 2000 must survive every reduction, or zooming out hides it.
  assert.ok(peakBetween(env, 0, 4096) > 0.3);
});

test("levelFor picks the finest level that still has several buckets per column", () => {
  const min = new Array(4096).fill(0);
  const max = new Array(4096).fill(0);
  const env = parseEnvelope(makeBlob({ durationMs: 4096, min, max }));
  // One column spanning 4 buckets cannot be folded 16× without losing detail.
  assert.equal(levelFor(env, 4).factor, 1);
  // One column spanning 1024 buckets may use a coarser level.
  assert.ok(levelFor(env, 1024).factor > 1);
});

test("hasShape is false for null and for an empty envelope", () => {
  assert.equal(hasShape(null), false);
  assert.equal(
    hasShape({ durationMs: 0, bucketMs: 0, levels: [{ factor: 1, min: new Int8Array(0), max: new Int8Array(0) }] }),
    false,
  );
  assert.equal(hasShape(parseEnvelope(makeBlob({ min: [1], max: [1] }))), true);
});
