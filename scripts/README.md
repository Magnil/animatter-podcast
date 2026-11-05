# 浏览器真机检查（CDP）

`node --test` 只能测纯函数，`--screenshot` 只能看首屏。这一组脚本用 Chrome
DevTools Protocol 驱动真实页面：点按钮、读**计算后**的样式、在编辑后回读状态。

它们断言的是"事实"，不是像素——「侧栏是 420px」「这个词带了 `revised` 类」
「菜单没出视口」这类，才是真的会坏的东西，而截图没法告诉审查者哪一项退化了。

## 用法

```bash
# 需要后端 (127.0.0.1:8317) 与 vite (localhost:5173) 都在跑
node scripts/cdp.mjs http://localhost:5173/ out.png scripts/check-edit-loop.mjs
```

`scripts/cdp.mjs <url> <out.png> [script.mjs]` 会起一个无头 Chrome、开调试端口、
注入脚本；脚本默认导出一个 `{ evaluate, click, shot, wait, out }` 函数。
不传脚本时就只是截一张图。

Node 22+ 自带全局 `WebSocket`，所以没有额外依赖。

## 现有脚本

| 脚本 | 检查什么 |
|---|---|
| `check-workbench.mjs` | 三栏布局宽度、词/句数量；点一个词后气泡出现、按钮有「删除/修正」、且不越界 |
| `check-edit-loop.mjs` | 点词 → 点删除 → 回读 `line-through` 与红色；再点修正 → 回读蓝色与字重 |
| `check-speaker-menu.mjs` | 「…」菜单在视口内、条目齐全；改名后所有该说话人的句子都跟着变 |
| `check-waveform-zoom.mjs` | 滚轮 4 格后滑块变窄、读数变小；拖滑块 `aria-valuenow` 变化；点「全部」回到 100%；设置里出现「热词与校对」 |
| `check-new-project-hotwords.mjs` | 勾选「转写时启用热词」才出现编辑器；输入的词进入本期草稿；全局页能看到共享词库 |
| `check-hotword-panel.mjs` | 加第二个本期热词后第一个还在（防「追加被当成替换」）；删掉后恢复原状 |

## 非浏览器验证

`scripts/verify_hotwords.py` 走真后端 + 真百炼：建项目 → 登记音频 → 加本期热词
→ 转写 → 直接读 SQLite 里的 `transcribe_job.params_json`，确认
`parameters.vocabulary` 非空。单元测试只能证明 map 交给了 service，
只有这条能证明它活过了整条链路。

## 为什么值得留着

- **迁移与后端测试覆盖不到界面**：词级改造里最脆的两个点（气泡被挤出屏幕、
  菜单溢出栏宽）只有真实布局能暴露。
- **改一次就可能坏**：这些脚本改动了数据库，跑之前先
  `cp data/app.db data/app.db.bak-before-e2e`，跑完恢复。
- **视觉走查另有一处**：`apps/web/harness.html?scene=bubble|correct|speaker|rename|drag|multi`
  用固定数据挂载 `SentenceList`，能截出真实工作台里抓不到的瞬间状态。
