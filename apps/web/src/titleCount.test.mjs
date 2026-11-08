/**
 * Title counting must agree with the backend, or the panel shows a number that
 * contradicts the filter that produced the list.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { MAX_TITLE_CHARS, countTitleChars } from "./titleCount.ts";

const WITHOUT_ADJECTIVE = "音乐如何被画出来：学院与二次元之间的网络创作"; // 22
const WITH_ADJECTIVE = "音乐如何被画出来：学院派与二次元之间的网络创作"; // 23

test("全角字符和汉字各记一字", () => {
  assert.equal(countTitleChars(WITHOUT_ADJECTIVE), 22);
  assert.equal(countTitleChars(WITH_ADJECTIVE), 23);
});

test("半角字母按字符计，MV 是两个字", () => {
  assert.equal(countTitleChars("MV"), 2);
  assert.equal(countTitleChars("动画MV：最活跃"), 8);
});

test("半角标点和全角标点都各记一字", () => {
  assert.equal(countTitleChars("甲，乙"), 3); // 甲 ， 乙
  assert.equal(countTitleChars("甲,乙"), 3); // 甲 , 乙
});

test("空白不占字", () => {
  assert.equal(countTitleChars("音乐 如何"), 4);
  assert.equal(countTitleChars("  "), 0);
});

test("零宽字符跟着前一个字，不另记", () => {
  assert.equal(countTitleChars("甲\u200d乙"), 2);
  assert.equal(countTitleChars("甲\ufe0f"), 1);
});

test("上限就是文档里的 22", () => {
  assert.equal(MAX_TITLE_CHARS, 22);
  assert.equal(countTitleChars(WITHOUT_ADJECTIVE) <= MAX_TITLE_CHARS, true);
  assert.equal(countTitleChars(WITH_ADJECTIVE) <= MAX_TITLE_CHARS, false);
});
