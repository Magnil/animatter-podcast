# 架构：技术选型、系统结构、数据模型

> 读这份回答：**表长什么样、字段叫什么、目录怎么摆、依赖为什么是这些。**
> 加字段、写迁移、引依赖之前读；§3.2 末尾记了设计与实现的偏差，别照着规划写代码。
>
> 章节号 `§N` 全局唯一，源码注释里的 `AGENTS.md §N` 指的就是这里。

---

## 3. 技术选型

### 3.1 形态：本地优先（Local-first）

不做 SaaS。v1 = **本地 HTTP 服务 + 浏览器前端**，`127.0.0.1` 绑定，v1.5 用 Tauri 2 打包成桌面应用（壳只加载本地服务）。

理由：
- 音频原文件在本地，剪辑输出在本地，避免几十 GB 往返；
- ffmpeg 本地调用最稳；
- 未来要打包成桌面时，前端代码零改动。

### 3.2 栈

| 层 | 选型 | 备注 |
|---|---|---|
| 后端 | Python 3.12+ / FastAPI / Uvicorn | 现有环境 3.13 可用；锁 `requires-python >=3.11` |
| ORM | SQLAlchemy 2.0 (typed) + Alembic | 不用 SQLModel，避免 pydantic 版本纠缠 |
| 校验 | pydantic v2 | |
| HTTP | httpx（async）+ tenacity | 不用 `requests`，不用 dashscope SDK |
| 数据库 | SQLite（WAL） | 单机、十万级句子足够；项目级隔离 |
| 音频 | ffmpeg / ffprobe（系统二进制，启动时自检版本） | 不做 Python 端解码重采样 |
| 数值 | numpy | 波形 peaks 计算 |
| 日志 | structlog（JSON） | 便于复盘一次失败的转写 |
| 前端 | React 19 + Vite + TypeScript strict | |
| 状态 | ~~Zustand + TanStack Query~~ → **`App.tsx` 的 `useState` + props 下传** | 见下方「与实现的偏差」 |
| 波形 | ~~wavesurfer.js v7~~ → **自绘 canvas + 后端 peaks** | 见 §7.1：浏览器解码整段会爆，改为后端一次解码后分级降采样 |
| UI | ~~Tailwind + Radix~~ → **手写 `styles.css`** | 不引重型组件库（避免与波形手势冲突） |
| 测试 | pytest + pytest-asyncio；前端 **`node --test`** | 核心算法单测覆盖率 ≥90%；前端测试直接 import `.ts`，不经打包器 |

> **与实现的偏差（2026-11-09 核对）**：上面四行最初是规划，不是事实，现按代码订正。
> 前端运行时依赖只有 `react` / `react-dom` / `wavesurfer.js`；状态全在 `App.tsx` 的 28 个 `useState` 里；
> 样式是单个手写 CSS；测试跑 `node --test` 而不是 Vitest。
> 少掉的库没有变成技术债——自绘波形和自研虚拟列表反而各自绕开了一个坑（§7.1、§7.5）。

### 3.3 依赖红线

- 禁止：`dashscope`、`pydub`（会隐式调 ffmpeg 且慢）、`librosa`、`moviepy`。
- 允许：`httpx`、`tenacity`、`numpy`、`srt`（或自写 SRT 序列化）、`python-docx`。
- 前端禁止引入完整编辑器框架（ProseMirror/Slate）做字幕编辑——用**自研虚拟列表**，见 §7.5。

---

## 4. 系统架构

```
┌──────────────────────────── 浏览器 (React) ────────────────────────────┐
│  项目页 │ 热词页 │ 校正台(波形+词级标签) │ 成片预览 │ 文稿页            │
│         （状态在 App.tsx 的 useState 里，props 下传；见 §3.2 偏差说明）      │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ REST + SSE (127.0.0.1)
┌───────────────────────────────▼────────────────────────────────────────┐
│                        FastAPI (apps/api)                              │
│  routers/  projects · media · hotspots · transcribe · transcript       │
│            edits · render · script · export                            │
├────────────────────────────────────────────────────────────────────────┤
│  services/  用例编排（事务边界在这一层）                                 │
│   TranscribeService · EditService · RenderService · ScriptService      │
├──────────────┬──────────────┬──────────────┬───────────────────────────┤
│ providers/   │ media/       │ core/ (纯函数，无 IO)                    │
│ asr/         │ ffmpeg.py    │ timeline.py   时间轴/区间代数            │
│  base.py     │ peaks.py     │ tokens.py     word→char 展开             │
│  qwen.py     │ silence.py   │ sentence.py   句边界重划                 │
│  paraformer  │ render.py    │ edl.py        删除集→保留集→映射         │
│ llm/         │ probe.py     │ subtitle.py   CPS/断行/序列化            │
│ storage/     │              │ script.py     文稿结构                    │
│  temp_oss.py │              │                                          │
├──────────────┴──────────────┴──────────────┴───────────────────────────┤
│  jobs/  进程内 asyncio 队列 + jobs 表（可重启恢复）                      │
├────────────────────────────────────────────────────────────────────────┤
│  store/  SQLite(WAL) + data/projects/<id>/ 文件树                       │
└────────────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │ 阿里百炼               │
                    │ 临时OSS · ASR · Qwen  │
                    └───────────────────────┘
```

**分层铁律**

1. `core/` 必须是纯函数：输入 dataclass，输出 dataclass，**不碰 DB、不碰文件、不发网络**。所有时间轴算法在这里，可被单测穷举。
2. `providers/` 只做协议适配，不做业务判断（重试放 provider，语义判断放 service）。
3. `services/` 是唯一的事务边界，一个用例一个事务。
4. `routers/` 只做参数校验与权限（本地无鉴权，但要有 CSRF/Origin 校验）。
5. 前端不得直接拼 ffmpeg 参数，也不得持有 API Key。

---

## 5. 数据模型

### 5.1 SQLite 表

```sql
-- 项目
project(id TEXT PK, name TEXT, created_at, updated_at, schema_version INT)

-- 媒体（一个项目可挂多个，v1 通常一个）
media(id TEXT PK, project_id, path TEXT, sha256 TEXT UNIQUE,
      duration_ms INT, sample_rate INT, channels INT, codec TEXT,
      kind TEXT,            -- 'original' | 'asr_input' | 'derived'
      created_at)

-- 转写任务（可重跑，保留历史）
transcribe_job(id TEXT PK, project_id, media_id,
      provider TEXT, model TEXT,
      params_json TEXT,     -- 实际下发的参数快照（含热词、context、headers）
      oss_key TEXT, task_id TEXT,
      status TEXT,          -- queued|uploading|submitted|running|succeeded|failed|canceled
      error_code TEXT, error_msg TEXT,
      usage_json TEXT,      -- 计费/时长
      raw_path TEXT,        -- 落盘的原始结果 JSON
      -- 长音频自动分段（>2h，见 §6.2）
      parent_job_id TEXT NULL, segment_index INT DEFAULT 0,
      segment_start_ms INT DEFAULT 0, segment_end_ms INT DEFAULT 0,
      created_at, finished_at)

-- 说话人（项目级身份；分段识别的局部 id 映射到这里）
speaker(id TEXT PK, project_id, name TEXT, color TEXT, created_at)

-- 转写版本（一次成功转写 = 一个不可变版本）
transcript_version(id TEXT PK, project_id, media_id, job_id,
      version INT, created_at,
      duration_ms INT, token_count INT, sentence_count INT,
      is_active INT)

-- Token 流：**不可变**，只由 ASR 产生
token(id INTEGER PK AUTOINCREMENT,
      version_id TEXT, idx INT,                 -- idx 全局单调
      start_ms INT, end_ms INT,
      text TEXT,                                 -- 单个字/英文词，不含标点
      kind TEXT,                                 -- 'char' | 'word' | 'punct'
      src_word_idx INT,                          -- 来源于第几个 ASR word
      punc_after TEXT,                           -- 该 token 后的标点
      derived INT)                               -- 1 = 时间由插值估算

-- 句：**唯一可变**的结构，用 token 区间表达
sentence(id TEXT PK, version_id TEXT,
      ord INT,                                   -- 显示顺序，单调
      token_start INT, token_end INT,            -- [start, end) 半开区间
      speaker_id INT NULL,
      origin TEXT,                               -- 'asr' | 'manual_split' | 'manual_merge'
      updated_at)
-- 约束：同一 version 内区间按 ord 单调不重叠且完全覆盖 [0, token_count)

-- 编辑事件日志（可撤销 / 可重放）
edit_op(id TEXT PK, project_id, version_id,
      seq INT, op_type TEXT, payload_json TEXT,
      created_at, undone INT DEFAULT 0)

-- 删除集（剪辑意图），由 token 删除或框选产生
deletion(id TEXT PK, version_id TEXT,
      start_ms INT, end_ms INT,
      source TEXT,          -- 'token' | 'range'
      token_ids_json TEXT,
      created_at, removed INT DEFAULT 0)

-- 渲染产物
render(id TEXT PK, project_id, version_id,
      edl_hash TEXT,
      out_path TEXT, duration_ms INT,
      status TEXT, created_at)

-- 导出
export(id TEXT PK, project_id,
      kind TEXT,            -- 'srt'|'vtt'|'ass'|'md'|'docx'|'txt'
      path TEXT, options_json TEXT, created_at)

-- 全局热词（词库）
hotword(id TEXT PK, project_id NULL,          -- NULL = 跨项目全局
      text TEXT, weight INT, lang TEXT,
      enabled INT, tags_json TEXT,
      hit_count INT, miss_count INT,          -- 人工校正时自动累计
      created_at, updated_at)
UNIQUE(project_id, text)

-- 全局词表的远端映射（受 50 个配额 + 5 分钟冷却约束）
hotword_sync(id TEXT PK, project_id NULL,
      vocabulary_id TEXT, target_model TEXT,
      content_hash TEXT, dirty INT, last_synced_at, next_allowed_at)

-- 当期热词（属于某个 media / job）
episode_hotword(id TEXT PK, project_id, media_id,
      text TEXT, weight INT, lang TEXT,
      origin TEXT,          -- 'manual' | 'outline' | 'second_pass'
      evidence TEXT,        -- 大纲里的出处，便于人工判断
      enabled INT)

-- 大纲原文（保留，便于重生成）
outline(id TEXT PK, project_id, media_id, content TEXT, created_at)

-- 后台任务
job(id TEXT PK, kind TEXT, payload_json TEXT,
      status TEXT, progress REAL, message TEXT,
      created_at, started_at, finished_at, error TEXT)
```

### 5.2 文件树

```
data/
└─ projects/<project_id>/
   ├─ project.json              # 轻量快照，可读
   ├─ media/
   │   ├─ original.<ext>        # 原始，只读，永不变（剪辑永远读它）
   │   ├─ asr_input.mp3         # 16k 单声道，仅供上传
   │   └─ derived/              # 预览成片、临时分段
   ├─ transcripts/
   │   ├─ v1.raw.json           # 服务端原样，存档
   │   └─ v1.normalized.json    # 归一化 token 流快照（供 diff）
   ├─ peaks/
   │   └─ <sha256>.pkc          # 多级波形峰值，二进制
   ├─ edits/
   │   └─ ops.jsonl             # 【未实现】最初设计的追加式事件日志；撤销实际走 edit_op 表
   ├─ exports/
   └─ logs/
```

**原则：原始媒体 immutable，所有编辑是"对原文的引用"。** 用户删了 3 小时音频，磁盘上也只多一个 EDL。

---

