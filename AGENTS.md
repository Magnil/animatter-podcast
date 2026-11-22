# AGENTS.md — Animatter Podcast Studio

> 播客转写 / 字幕校正 / 波形剪辑 / 文稿整理。
> 本文件是**入口**：铁律、目录、命令，以及「改什么该读哪份」。
> 细节按需读 `docs/`。**不要一次把 docs 全读了**——那样和原来一份 2000 行的文件没有区别。

## 为什么拆开

原来所有内容在一份 ~12 万字节、2000 行的 `AGENT.md` 里，两个问题：

1. **它从来没被自动加载过。** harness 自动注入的工作区指令只有 `AGENTS.md` / `CLAUDE.md`，`AGENT.md`（少一个 S）不在名单里，所以过去的"先读文档"靠的是我记得去读，而且常常只读了几段。
2. **全读约 7 万 token，其中 43% 是历史。** 变更记录占 200 KB，写得细，但和"现在要动的地方"无关。

现在：本文件约 6 KB，随会话自动注入（预算 64 KB，`truncateUtf8` 保留的是**前** 64 KB，所以入口文件必须自己就够小，否则丢的是尾部）；`docs/` 按主题分 12 份，改哪块读哪份；`docs/CHANGELOG.md` 只在两种时候打开——查"当初为什么这么做"，或者追加新记录。

## 项目是什么

把一期播客（1–3 小时、单人或多轨）变成三样东西：

1. 可校正的**词级时间轴**（最小单位 = 一个 ASR 词）；
2. 按词级标签删改后的**成片音频**（只做剪切，不做音效）；
3. 一份**结构化文稿** + 配套字幕（时间轴已重映射到成片）。

## 铁律

违反任何一条就是 bug，不需要再查文档确认：

- **时间是 `int` 毫秒。** 禁止 float 秒。`core/` 里禁止 `datetime`。
- **区间一律半开 `[start, end)`**，命名 `*_start` / `*_end`，不混用 `begin/end`。
- **一个 token = 一个 ASR 词。** 不展开到字、不插值——时间戳是观测值，不是算出来的。
- **句 = token 区间。** `sentence.ord` 是显示位置不是 ID，任何编辑后从 0 重排（INV-1..4）。
- **分层**：`core/`（纯函数、无 IO）→ `providers/` → `services/`（事务边界）→ `routers/`（只做校验）。
- **`core/` 里禁止** import `httpx` / `sqlalchemy` / 任何 IO。
- **不引 `dashscope` SDK**，走 `httpx` + REST（SDK 不支持 `oss://` 和自定义 header）。
- **API Key 只在后端**（`.env`），永不下发前端、不写日志、不进 bundle。
- **原片只读。** 剪辑是一组区间引用（EDL），绝不改写原始媒体文件。
- **渲染链路不许静默挪时间点。** 吸附到静音必须有日志、可关闭。
- **重大改动逐步 commit，不许攒。** 每完成一个可回滚的语义单元就提交一次——一个端点、一个面板、
  一个纯函数模块、一次验收。攒上几轮再回头拆，等于事后重新推导「哪几行属于哪一轮」：那件事
  花了整整一晚，还要靠 difflib 重建中间状态，而且差一个换行符就会得到一个 `import` 失败的提交。
  **正在做的事不必等它完美**，先提交能通过的半步，下一步再改。

## 改什么，读哪份

**每份文档只有一个职责，不要把内容写在别的分类里。** 判据：读这份是在回答哪个问题。

| 你要动的 | 读这份 | 章节 | 这份回答的问题 |
|---|---|---|---|
| 产品边界、某功能该不该做、一次典型会话 | [`docs/product.md`](docs/product.md) | §0 §1 §17 | 做什么、不做什么 |
| 百炼 API 的参数名/限制/配额/模型能力、文档链接 | [`docs/constraints.md`](docs/constraints.md) | §2 §18 | 外部世界强加了什么 |
| 表结构、字段、迁移、目录布局、技术选型 | [`docs/architecture.md`](docs/architecture.md) | §3 §4 §5 | 系统长什么样 |
| 转写链路：转码、切分、热词、状态机、归一化 | [`docs/pipeline.md`](docs/pipeline.md) | §6.0–6.3 | 一次转写怎么走完 |
| 句/词模型、边界编辑、EDL、删除、字幕、文稿 | [`docs/editing-model.md`](docs/editing-model.md) | §6.4–6.7 | 编辑的语义与不变量 |
| 前端布局、波形、虚拟列表、交互 | [`docs/frontend.md`](docs/frontend.md) | §7 | 界面为什么这样 |
| 接口形状、错误码、测试分层、崩溃恢复、密钥、成本 | [`docs/api-and-quality.md`](docs/api-and-quality.md) | §8–§12 | 契约与质量下限 |
| 已经拍板的决策、容易被漏掉的坑 | [`docs/decisions.md`](docs/decisions.md) | §14 §15 | **不要再争论什么** |
| 视频剪辑：模板编译、素材框、头像亮暗、渲染管线、**实时预览与导出提速** | [`docs/video.md`](docs/video.md) | §19 | 视频怎么合成、预览怎么实时、导出为什么这么快、踩过哪些坑 |
| **做到哪了、验收标准是什么** | [`docs/milestones.md`](docs/milestones.md) | §13 | 已交付什么（只记结论） |
| **某个东西当初为什么这么做** | [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | 按日期 | 当时的取舍与试错 |
| **还没做的事，以及为什么现在不做** | [`todolist.md`](todolist.md) | A–E | 下一步做什么 |
| 协作偏好、提交纪律、todo 规范 | [`docs/conventions.md`](docs/conventions.md) | §16 | 怎么在这个仓库干活 |

**四条最容易写错的边界**（第 122 轮整理出来的）：

- `todolist.md` **只写没做的事**。做完一条就删掉——验收写进 `milestones.md`，原因写进
  `CHANGELOG.md`。留着的已完成项会让人以为还要做。
- `milestones.md` **只记结论和判据**，不记设计推理（那在主题文档里）和逐轮细节（那在 CHANGELOG）。
- 主题文档（`video.md`、`frontend.md` 等）**只写现在的设计与实测**；「这一轮为什么这么改」属于
  CHANGELOG，只有当作设计约束留下时才写进主题文档。
- 决策一旦拍板就进 `decisions.md` 并给编号（D1–D26，D27 起可用），主题文档里只引用编号，不复制理由。

`§N` 编号跨文件保持全局唯一，源码注释里的 `AGENTS.md §6.4` 指的就是上表对应的那份。
**§20 已经不存在**：它原是「实时预览与导出提速」的方案文档，七个阶段落地后按约定并进了
`docs/video.md` §19.31–§19.34，全文删除。源码里不会再出现 `§20.x`。

## 目录

```
apps/api/                后端：FastAPI + SQLAlchemy 2.0 + Alembic
  src/animatter/
    core/                纯函数（tokens / sentence / edl / subtitle / hotwords / realign / script）
    providers/           HTTP、ASR、LLM、临时 OSS
    services/            事务边界（transcribe / editing / cleanup / renders / align / script）
    media/               ffmpeg、peaks、render、probe、silences
    routers/             只做参数校验与响应
  alembic/versions/      迁移（必须幂等，test_migrations.py 会重放）
  tests/                 单元 + 集成（`live` 标记默认跳过）
apps/web/                前端：React 19 + Vite 6 + TS strict（样式是手写 styles.css）
  src/                   App.tsx 是主干；可单测的纯逻辑放在 *.ts 里
data/                    运行时数据（gitignore）：app.db + 素材 + 渲染产物
scripts/                 dev.ps1、CDP 走查脚本
docs/                    本文件拆出来的设计文档
```

## 命令

```powershell
# 后端：在 apps/api 下跑
..\..\.venv\Scripts\python.exe -m pytest -q -n 2      # 1526 passed, 2 skipped，约 90 s
..\..\.venv\Scripts\python.exe -m ruff check src tests
..\..\.venv\Scripts\python.exe -m mypy --strict src    # 112 files

# 前端：在 apps/web 下跑
npx tsc --noEmit
npm test                                               # node --test，349 passed
npx vite build

# 启动（仓库根）
.\scripts\dev.ps1                                      # 装依赖 → 迁移 → 8317
```

**pytest 默认并行跑**（`addopts` 里 `-n auto --dist loadscope`）。迭代单个文件时加 `-n0`，
比启动 worker 快：`pytest tests/test_decks.py -n0`。

**这台机器上 `-n auto` 已经不可靠**：8 个 worker 同时跑 PSD 编译（`test_video_template_*`
每次要 1440×2560 的 float32 画布）会偶发 `MemoryError: Unable to allocate 42.2 MiB`。
**用 `-n 2`**；单文件 `-n0` 全过。

**但 `-n 2` 也不是永远全绿**：空闲内存低于约 5 GB 时（浏览器开着、后台还在跑别的），
`test_video_template_edits.py` 会成批 ERROR（就是上面那条 `MemoryError`），
`test_video_avatar_render.py` 里那条真 x264 编码会以 `ffmpeg` 退出码 `0xDFABA7BB` 失败。
**判据是「换个跑法还过不过」，不是失败本身**：

```powershell
..\..\.venv\Scripts\python.exe -m pytest -q -n 1        # 内存不够时用这个，慢约 60 %
```

`-n 1` 连跑 **1526 passed / 2 skipped**（约 185 s）。看到那两类失败时先跑一遍 `-n 1`
再判断是不是回归——**不要直接去查代码**，这条已经浪费过一次时间。

**改完四个都要绿**：pytest、ruff + mypy、tsc、npm test。设计文档与实现不一致时，先改文档再改代码。

## 界面走查

`.tmp/` 是 gitignore 的一次性工作目录（截图、测量脚本、中转视频），**随时会被清空**，不要往里放任何需要留下的东西；判据要入库就必须进 `scripts/`。

`node scripts/cdp.mjs <url> <out.png> [script.js]`（仓库根）。脚本默认导出接收 `{ evaluate, click, shot, wait, mouse, send, out }`，**必须显式 `await shot(out)`**，否则不会产生 PNG。现有脚本见 `scripts/check-*.mjs`。截图会自建父目录，所以 `.tmp/` 被清空之后走查脚本仍然能直接跑。

**点播放类走查必须用 `mouse()`，不能用 `element.click()`**：合成点击没有 user activation，`play()` 会以 `NotAllowedError` 被拒，把真正的解码错误盖掉——「03:35.180 点播放没反应」就是因此一直复现不出来。
