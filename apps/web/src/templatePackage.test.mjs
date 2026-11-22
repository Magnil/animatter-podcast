/**
 * Tests for the template package's browser-side arithmetic.
 *
 * The bug this pins: the server sends both `filename="template.template"` and
 * `filename*=UTF-8''播客.template`. Reading the first one wins by accident and the user gets
 * a file called `template.template` — no error anywhere, just a useless name. The extended
 * form is the only one that can carry Chinese at all.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  PACKAGE_EXT,
  REPLACE_WARNING,
  filenameFromDisposition,
  packageSize,
} from "./templatePackage.ts";

test("the RFC 5987 name wins over the ASCII one", () => {
  const header = `attachment; filename="template.template"; filename*=UTF-8''%E6%92%AD%E5%AE%A2%E6%A8%A1%E6%9D%BF.template`;
  assert.equal(filenameFromDisposition(header), "播客模板.template");
});

test("a quoted name is honoured when there is no extended form", () => {
  assert.equal(filenameFromDisposition(`attachment; filename="第 3 期.template"`), "第 3 期.template");
});

test("an unquoted name is honoured too", () => {
  assert.equal(filenameFromDisposition("attachment; filename=ep3.template"), "ep3.template");
});

test("a missing header falls back instead of returning empty", () => {
  // An empty `link.download` makes the browser invent a name from the URL — on a blob URL
  // that is a UUID. Better a boring name than a UUID.
  assert.equal(filenameFromDisposition(null), `模板${PACKAGE_EXT}`);
  assert.equal(filenameFromDisposition("attachment"), `模板${PACKAGE_EXT}`);
  assert.equal(filenameFromDisposition(null, "兜底.template"), "兜底.template");
});

test("a malformed escape falls through rather than throwing", () => {
  // decodeURIComponent throws on a lone '%'; an export must not fail on a bad header.
  assert.equal(
    filenameFromDisposition(`attachment; filename="ok.template"; filename*=UTF-8''%E6%92%AD%`),
    "ok.template",
  );
});

test("quotes around the extended value are stripped", () => {
  assert.equal(
    filenameFromDisposition(`attachment; filename*=UTF-8''%E7%AC%AC1%E6%9C%9F.template`),
    "第1期.template",
  );
});

test("package sizes are rounded to something a person reads", () => {
  assert.equal(packageSize(1_292_670), "1.2 MB");
  assert.equal(packageSize(2048), "2 KB");
  assert.equal(packageSize(700), "1 KB");
  // Zero means "we don't know" (no X-Template-Bytes header): print nothing, not "0 KB".
  assert.equal(packageSize(0), "");
  assert.equal(packageSize(Number.NaN), "");
});

test("the replace warning names the two things that are lost", () => {
  // Not a question ("确定吗？") — a statement of consequences. A generic confirm teaches the
  // user to click through confirms.
  assert.match(REPLACE_WARNING, /文字/);
  assert.match(REPLACE_WARNING, /头像/);
  assert.match(REPLACE_WARNING, /PSD/);
});
