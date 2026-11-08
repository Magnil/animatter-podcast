# AGENTS.md — Animatter Podcast Studio

> 播客转写 / 字幕校正 / 波形剪辑 / 文稿整理。
> 本文件是**入口**：铁律、目录、命令，以及「改什么该读哪份」。
> 细节按需读 `docs/`。**不要一次把 docs 全读了**——那样和原来一份 2000 行的文件没有区别。

## 为什么拆开

原来所有内容在一份 ~12 万字节、2000 行的 `AGENT.md` 里，两个问题：

1. **它从来没被自动加载过。** harness 自动注入的工作区指令只有 `AGENTS.md` / `CLAUDE.md`，`AGENT.md`（少一个 S）不在名单里，所以过去的"先读文档"靠的是我记得去读，而且常常只读了几段。
2. **全读约 7 万 token，其中 43% 是历史。** 变更记录占 51 KB，写得细，但和"现在要动的地方"无关。

现在：本文件约 6 KB，随会话自动注入（预算 64 KB，`truncateUtf8` 保留的是**前** 64 KB，所以入口文件必须自己就够小，否则丢的是尾部）；`docs/` 按主题分 11 份，改哪块读哪份；`docs/CHANGELOG.md` 只在两种时候打开——查"当初为什么这么做"，或者追加新记录。

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

## 改什么，读哪份

| 你要动的 | 读这份 | 章节 |
|---|---|---|
| 产品边界、某功能该不该做、一次典型会话 | [`docs/product.md`](docs/product.md) | §0 §1 §17 |
| 百炼 API 的参数名/限制/配额/模型能力、文档链接 | [`docs/constraints.md`](docs/constraints.md) | §2 §18 |
| 表结构、字段、迁移、目录布局、技术选型 | [`docs/architecture.md`](docs/architecture.md) | §3 §4 §5 |
| 转写链路：转码、切分、热词、状态机、归一化 | [`docs/pipeline.md`](docs/pipeline.md) | §6.0–6.3 |
| 句/词模型、边界编辑、EDL、删除、字幕、文稿 | [`docs/editing-model.md`](docs/editing-model.md) | §6.4–6.7 |
| 前端布局、波形、虚拟列表、交互 | [`docs/frontend.md`](docs/frontend.md) | §7 |
| 接口形状、错误码、测试分层、崩溃恢复、密钥、成本 | [`docs/api-and-quality.md`](docs/api-and-quality.md) | §8–§12 |
| 已经拍板的决策、容易被漏掉的坑 | [`docs/decisions.md`](docs/decisions.md) | §14 §15 |
| 做到哪了、还差什么 | [`docs/milestones.md`](docs/milestones.md) | §13 |
| **某个东西当初为什么这么做** | [`docs/CHANGELOG.md`](docs/CHANGELOG.md) | 按日期 |
| 手头排队的活 | [`todolist.md`](todolist.md) | — |
| 协作偏好、提交纪律、todo 规范 | [`docs/conventions.md`](docs/conventions.md) | §16 |

`§N` 编号跨文件保持全局唯一，源码注释里的 `AGENTS.md §6.4` 指的就是上表对应的那份。

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
..\..\.venv\Scripts\python.exe -m pytest -q            # 651 passed, 1 skipped, 3 deselected
..\..\.venv\Scripts\python.exe -m ruff check src tests
..\..\.venv\Scripts\python.exe -m mypy --strict src    # 74 files

# 前端：在 apps/web 下跑
npx tsc --noEmit
npm test                                               # node --test，133 passed
npx vite build

# 启动（仓库根）
.\scripts\dev.ps1                                      # 装依赖 → 迁移 → 8317
```

**改完四个都要绿**：pytest、ruff + mypy、tsc、npm test。设计文档与实现不一致时，先改文档再改代码。

## 界面走查

`node scripts/cdp.mjs <url> <out.png> [script.js]`（仓库根）。脚本默认导出接收 `{ evaluate, click, shot, wait, mouse, send, out }`，**必须显式 `await shot(out)`**，否则不会产生 PNG。现有脚本见 `scripts/check-*.mjs`。
