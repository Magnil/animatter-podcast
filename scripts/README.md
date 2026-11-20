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
| `check-cleanup-dialog.mjs` | 清理对话框：三类计数与「本次会删 N 处」一致、逐条列出、全部取消后执行按钮禁用 |
| `check-cleanup-settings.mjs` | 设置 → 一键清理：61 行词表已载入、改阈值保存后从服务端读回、再复原 |

| `check-r135-export-dialogs.mjs` | 两个导出面板：菜单少 4 项、视频面板 2 文本框 + 2 勾选 + 无进度条、音频面板格式下拉 2 项且与文件名同排、两个面板都没有「取消」 |
| `check-r135-export-running.mjs` | 「导出 ⇄ 停止」真的换成：点导出后按钮变「停止」+ 进度条出现 + 输入框锁；点停止后「正在停止…」；终态「已停止」是**黄色**、按钮回「导出」、输入框解锁 |

## 非浏览器验证

`scripts/verify_hotwords.py` 走真后端 + 真百炼：建项目 → 登记音频 → 加本期热词
→ 转写 → 直接读 SQLite 里的 `transcribe_job.params_json`，确认
`parameters.vocabulary` 非空。单元测试只能证明 map 交给了 service，
只有这条能证明它活过了整条链路。

`scripts/verify-p1-consistency.py` 比对 CDP 截下的 20 个舞台时刻与 `preview_still`，
按「平坦区 ≤ 2 % + 全局亚像素位移 ≤ 0.5 px」判（口径见 `docs/video.md` §19.31.5）。

`scripts/verify-e1-plate.py` 验收「烘焙 plate 与旧图逐位相同」：单帧约 2 s；`--chunk N`
真编一个 chunk 两遍（旧图 / 烘焙图）并逐帧比 PSNR，约 1 min。判据是 **`inf`**——头像补丁
如果画错缩放器会掉到 54 dB，肉眼几乎看不出，所以这条不能靠目视。

`scripts/verify-e3-workers.py` 验收导出并行度：`formula` 打印「空闲内存 × 块形状」的档位表
（纯函数，不跑 ffmpeg）；`wire` 把内存探针桩成 12 GB 跑真的 `_render_picture`，判据是日志
`workers=2` + `note` 文案 + **成片帧数 = 两块之和**。单测只能证明 `plan_workers` 对，这条证明
它被调用。

## 为什么值得留着

- **迁移与后端测试覆盖不到界面**：词级改造里最脆的两个点（气泡被挤出屏幕、
  菜单溢出栏宽）只有真实布局能暴露。
- **改一次就可能坏**：这些脚本改动了数据库，跑之前先
  `cp data/app.db data/app.db.bak-before-e2e`，跑完恢复。
- **视觉走查另有一处**：`apps/web/harness.html?scene=bubble|correct|speaker|rename|drag|multi`
  用固定数据挂载 `SentenceList`，能截出真实工作台里抓不到的瞬间状态。
