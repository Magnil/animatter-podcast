# AGENT.md — 播客转写 / 字幕校正 / 波形剪辑 / 文稿整理

> 本文件是本仓库的**设计基线 + Agent 工作约定**。
> 任何实现、重构、依赖引入都必须先与本文件一致；需要偏离时，先改本文件再改代码，并在"决策记录"里留痕。
> 当前状态：**v0.8 — M0/M1/M2a/M2b/M3/M4/M5a 已落地；词级编辑 + 说话人改名已落地**。
> 工作台界面（菜单栏 + 三栏）、波形剪辑、EDL 时间轴重映射、项目存档、字幕导出均已用真实音频验证。
> 下一步：M5b（文稿 DOCX/Markdown 生成）、M2c 影子测试、M6 稳定性。

---

## 0. 一句话目标

把一段播客音频（1–12 小时、单人或多人、可能分轨）变成三样东西：

1. **可校正的 词级/句级 时间轴**（人工校对成本最低的形态）；
2. **按词级标签删改后的成片音频**（不做音效，只做"剪"）；
3. **一份结构化的文稿 + 配套字幕文件**（时间轴已重映射到成片）。

---

## 1. 范围与非目标

### 1.1 范围内（v1 必须做到）

| # | 能力 | 验收口径 |
|---|---|---|
| R1 | 音频转写走阿里百炼云 API，本地文件经**百炼临时存储**上传取 `oss://` URL | 不落地任何公网可访问文件；失败可重试 |
| R2 | 全局热词 + 当期热词；当期热词可由粘贴的大纲自动生成 | 生成结果可人工增删改权重，最终作用于识别 |
| R3 | 句级标签 + 词级标签；词级标签可多选后重划归属（上句 / 下句 / 另起一句） | 任意划分不丢词、不重词、时间单调 |
| R4 | 通过删除词级标签剪辑音频；也可在波形上框选区间删除 | 输出音频与字幕时间轴严格一致 |
| R5 | 校正后的字幕自动整理为文稿并导出 | Markdown / DOCX / TXT 至少三种 |

### 1.2 范围外（明确不做，避免范围蔓延）

- 多轨混音、降噪、EQ、压缩、母带等一切"音响处理"（只允许剪辑必需的极短淡入淡出，见 §7.4）。
- 视频剪辑（**例外**：视频播客的音频轨按同一 EDL 剪，画面仅做"同源裁剪"作为 v1.5 可选）。
- 云端协作、账号体系、多租户（v1 为单机本地应用）。
- 实时转写（不做直播字幕）。
- 自动配音 / 变声 / TTS。

---

## 2. 关键外部约束（来自官方文档，硬事实）

这些数字直接决定了架构，**不得凭记忆改写**。

### 2.1 百炼录音文件识别（异步）

| 项 | 值 | 影响 |
|---|---|---|
| 提交 | `POST /api/v1/services/audio/asr/transcription` + `X-DashScope-Async: enable` | 必须异步 |
| 查询 | `GET /api/v1/tasks/{task_id}`，默认 20 QPS | 轮询间隔 ≥2s，指数退避 |
| 结果 | `results[].transcription_url`，**24 小时**有效 | 必须立刻下载落盘 |
| 转写 JSON | `transcripts[].sentences[]`，句含 `begin_time/end_time/text`，句含 `words[]`（词级 `begin_time/end_time/text/punctuation`） | **只有词级**，编辑器直接用这一层，不再展开到字（§6.3） |
| 时长/体积 | ≤12h、≤2GB；开说话人分离建议 ≤2h | 长播客要评估是否开分离 |
| 说话人分离 | `diarization_enabled`，**仅单声道** | 立体声分轨录制时用 `channel_id` 分别识别 |
| 热词-即时 | `parameters.vocabulary`，`{词: 权重}` | 当期热词走这条 |
| 热词-预编译 | `parameters.vocabulary_id` | 全局热词走这条 |
| 热词上限 | Qwen-Audio-ASR 系列 2000；Paraformer/Fun-ASR 部分型号 500 | 合并超限会**随机丢弃**，客户端自控 ≤1800 |
| 超级热词 | `weight=50`，仅 Qwen-Audio-ASR 系列，最多 50 个 | 大纲里的核心标题词可用 |
| 预编译词表配额 | 每账号 **50 个**（全模型共享） | 全局词库只能映射极少数词表 → 必须"单表 + 增量更新" |
| 词表更新 | 建议两次更新间隔 ≥5 分钟，否则可能不生效 | 全局词库需变更合并 + 冷却队列 |
| 上下文增强 | `input.context`，最多 5 条消息，每轮 text 合计 ≤400 字符 | 大纲**不能整段灌入**，只能放术语/摘要；策略见 §6.1 |
| 敏感词 | 未传 `special_word_filter` 时**系统词表默认把命中词替换为等长 `*`** | 播客高风险！必须显式配置 |
| `language_hints` | 最多 4 个值 | 中英混合用 `["zh","en"]` |
| 结果时间戳 | `timestamp_alignment_enabled`（paraformer）；Qwen 系列固定开启 | — |

### 2.2 模型选型

| 模型 | 热词 | 上下文 | 说话人 | 时长 | 计费 | 结论 |
|---|---|---|---|---|---|---|
| `qwen-audio-3.1-asr-flash-filetrans` | 即时+预编译+超级 | ✅ | ✅ | 12h/2GB | Token | **默认主选** |
| `paraformer-v2` | 预编译（500） | ❌ | ✅ | 12h/2GB | 语音时长 | 低成本备选 |
| `fun-asr` | 即时+预编译 | ❌ | ✅ | 12h/2GB | — | 备选 |
| `qwen3-asr-flash-filetrans` | 不支持 | ❌ | ❌ | 12h/2GB | — | 不满足 R2，排除 |

> **必须做 Provider 抽象**：不同模型的参数集不同（有无 `vocabulary`、有无 `context`、`keep_dialect` 仅 3.1 支持），上层不得假设。

### 2.3 临时 OSS 上传（百炼 `getPolicy` 方案）

| 项 | 值 | 影响 |
|---|---|---|
| 凭证接口 | `GET /api/v1/uploads?action=getPolicy&model=xxx` | 必须与**真实调用模型同名**，否则文件不可用 |
| 凭证有效期 | 300 秒 | 上传大文件要"先传再刷"，失败即重取 |
| 单文件上限 | **1GB** | 12h WAV 会远超 → **必须预转码**（§6.0） |
| 文件有效期 | 48 小时 | 只做"当天转写"，不做长期托管 |
| 调用要求 | HTTP 直连时**必须**带 `X-DashScope-OssResourceResolve: enable` | 漏了就是 400 |
| SDK | 录音文件识别 SDK **不支持** `oss://` | 所以走 REST + httpx，不引 dashscope SDK |
| 账号 | 上传与识别必须同一主账号 | — |

> 结论：**不引入 `dashscope` Python SDK**，用 `httpx` 直连 REST，避免 SDK 对 `oss://` 和自定义 header 的限制。这一条要写进依赖白名单。

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
| 状态 | Zustand（编辑器状态）+ TanStack Query（服务端状态） | 编辑态与查询态分离 |
| 波形 | wavesurfer.js v7（**MediaElement 后端**） | 见 §7.1，不用 WebAudio 解码整段 |
| UI | Tailwind + Radix Primitives | 不引重型组件库（避免与波形手势冲突） |
| 测试 | pytest + pytest-asyncio；前端 Vitest | 核心算法单测覆盖率 ≥90% |

### 3.3 依赖红线

- 禁止：`dashscope`、`pydub`（会隐式调 ffmpeg 且慢）、`librosa`、`moviepy`。
- 允许：`httpx`、`tenacity`、`numpy`、`srt`（或自写 SRT 序列化）、`python-docx`。
- 前端禁止引入完整编辑器框架（ProseMirror/Slate）做字幕编辑——用**自研虚拟列表**，见 §7.5。

---

## 4. 系统架构

```
┌──────────────────────────── 浏览器 (React) ────────────────────────────┐
│  项目页 │ 热词页 │ 校正台(波形+词级标签) │ 成片预览 │ 文稿页            │
│         Zustand 编辑器状态机  ·  TanStack Query  ·  SSE 进度            │
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
   │   └─ ops.jsonl             # 追加式事件日志（与 DB 双写，用于恢复）
   ├─ exports/
   └─ logs/
```

**原则：原始媒体 immutable，所有编辑是"对原文的引用"。** 用户删了 3 小时音频，磁盘上也只多一个 EDL。

---

## 6. 核心流程

### 6.0 预处理（转写前）

```
ffprobe → duration / channels / sample_rate / codec
      ├─ 检查时长 > 12h → 提示需要分段（v1.5 实现，v1 直接拒绝并给建议）
      ├─ 生成 asr_input.mp3：-ac 1 -ar 16000 -b:a 64k（12h ≈ 345MB，稳在 1GB 内）
      └─ 分轨录音（stereo 且左右声道是不同人）→ 检测左右声道相关性
            低相关 → 提示"是否分别识别两个音轨"，走 channel_id=[0] / [1] 两次任务
```

> 为什么必须转码：临时 OSS 单文件 1GB。12h 44.1k/16bit 立体声 WAV ≈ 7.6GB，直接上传必失败。

### 6.1 热词

#### 全局热词 → 预编译词表

- 每账号只有 50 个词表配额、更新有 5 分钟冷却 → **一个项目一个 `vocabulary_id`**，不做"每次识别建新表"。
- 同步策略：`content_hash` 变化 → 标记 `dirty` → 后台在 `next_allowed_at` 之后 `update_vocabulary`（全量替换）→ 回写 hash。
- 全局词库超 500/2000 时按 `weight desc, hit_count desc, updated_at desc` 截断，**截断结果写入 `transcribe_job.params_json` 快照**，保证可复现。
- 命中率反馈：人工把某词改对一次，`hit_count +1`；被改掉的错误写法 `miss_count +1`，用于自动降权。

#### 当期热词 → 即时热词

- 存储为 `episode_hotword`，识别时组装成 `{"词": 权重}`。
- 与全局词表**合并**后控制在 1800 以内（Qwen 系列 2000，留余量），避免服务端随机丢弃。

#### ⚠️ 实测：热词会「过度纠偏」（2026-11-05，M1 期间）

用同一段 17.6s 中文音频做了对照实验，参数已确认真正下发（`params_json` 快照可查）：

| 条件 | 识别结果 |
|---|---|
| 无热词 | …包括**句级**标签和**字级**标签。 |
| `{"张三":5,"百炼":5,"字幕校正":5}` | …包括**剧集**标签和**字集**标签。 |

**热词把原本正确的「句级」拉坏成了「剧集」。**这说明：

1. 热词不是"只纠正错的地方"，它是**全局的解码偏置**，会挤占其他词的生存空间；
2. 官方文档写的「权重过大可能产生负面效果，导致其他词语识别不准确」不是免责声明，是实测可复现的；
3. `weight=5` 对短音频过于激进。

**因此实现的约束（已写进 provider 层）**：

- 默认权重统一取 **4**，不提供 5 作为默认值；UI 上 5 标注为「可能误伤其他词」；
- 热词是**可选增强**，不是默认开启项。默认关闭，让用户按需打开；
- 提供**A/B 对照**能力：一键用同参数跑两遍（带/不带热词）并 diff，让用户自己判断是否值得开；
- 「加热词前后对比」的集成测试（§9.2 要求）**不能断言"改善"**——因为可能变差。断言应改为"两次结果都合法且可 diff"，由人工或指标（词错误率）判断优劣；
- 风险提示必须进文档与 UI：**加了热词反而更差是正常现象**，别让用户以为是 bug。

#### 大纲 → 当期热词（LLM）

```
输入：大纲全文（可长）+ 本期元信息（嘉宾/主题/节目名）
模型：qwen-plus（够用且便宜）
输出：严格 JSON：
{
  "terms": [
    {"text": "…", "weight": 4, "type": "person|org|product|term|title",
     "evidence": "大纲中的原句片段"}
  ]
}
```

**后处理（必须，别信模型）**：

1. 去重（保留最高权重）；
2. 长度校验：含非 ASCII ≤15 字符；纯 ASCII 按空格切 ≤7 段；
3. 剔除停用词、纯数字、纯标点、超长短语、完整句子；
4. 大类词降权（"人工智能"这种通用词 weight 降到 2，否则会挤占配额并误伤）；
5. 上限：默认 120 条，按 `type 优先级 → 大纲出现次数` 排序；
6. 每条保留 `evidence`，前端显示"依据"，让人 3 秒判断对不对。

**可选两遍法**（成本可接受时）：

- Pass 1：不加热词识别 → 从结果里统计低频高置信专名（连续 2–4 字、出现 ≥3 次、不在通用词表）；
- 与大纲热词合并去重 → Pass 2 正式识别。
- 成本约 ×2，只对"重要期"手动开启。

> **上下文增强不是热词的替代品**：每轮 ≤400 字符、最多 5 条，塞不进大纲。只用于传"本期人名/产品名列表"的紧凑串（≤400 字），作为辅助。

### 6.2 转写执行链（一次 job 的完整状态机）

```
queued
  → uploading      getPolicy(model=实际模型) → multipart POST → oss://key
                   ↳ 凭证 300s 过期 / 上传失败 → 重新 getPolicy 重试（最多 3 次）
  → submitted      POST /transcription，header 必须含：
                     Authorization, Content-Type,
                     X-DashScope-Async: enable,
                     X-DashScope-OssResourceResolve: enable
  → running        轮询 GET /tasks/{id}，间隔 2s→5s→10s（上限 10s），总超时 45min
  → downloading    取 transcription_url，**立刻**下载（24h 有效）→ v1.raw.json
  → normalizing    校验 → token 流 → 初始句划分 → 写库
  → succeeded
```

失败语义：
- `PENDING` 超过 30min 未动 → 提示排队，继续等；
- `subtask_status=FAILED` → 记录 `code/message`（常见 `FILE_DOWNLOAD_FAILED`），区分"OSS 文件过期"（重传）与"音频不可解"（终止）；
- 任何网络错误：指数退避 3 次，之后标 `failed` 并保留现场（`params_json` + response body）。

#### 长音频自动分段（>2h，决策 D8）

说话人分离在官方文档里建议 ≤2h，超长会失败或超时。因此：

```
duration ≤ 2h  → 单任务，diarization_enabled=true
duration > 2h  → 拆成 N 段（每段目标 90 分钟），逐段识别，再拼接
```

**切点选择（关键，别用等分）**：

1. 先跑 `ffmpeg -af silencedetect=noise=-35dB:d=0.4` 拿到全量静音区间；
2. 对每个目标切点（90min, 180min, …），在 ±5 分钟窗口内挑**离目标最近且长度 ≥0.4s 的静音**，切在该静音的**中点**；
3. 窗口内没有可用静音 → 退化为按时间硬切，并在任务里记 `seam_quality='forced'`，UI 提示该接缝需人工核对；
4. 每段写一条 `transcribe_job`（`parent_job_id` 指向总任务，`segment_index` 递增，`segment_start_ms` 记偏移）。

**拼接**：

- 子任务结果是**局部时间轴**，必须整体加上 `segment_start_ms` 再入库；
- 切在静音中点时无需 overlap，直接首尾相接；
- 若为 `forced` 硬切，允许 ±150ms 的重叠区，重叠内按「后段优先、删除前段尾部 token」去重；
- 句级接缝：若上一段末句没有终止标点、下一段首句不以大写/正常起始，则合并为一句（`origin='seam_merge'`）；
- 任一段失败只重试该段，其余段结果保留 —— 这正是分段的最大收益。

**已知粗糙点（必须告诉用户）**：说话人分离返回的 `speaker_id` 是**段内局部编号**。跨段拼出来的 "说话人1" 可能不是同一个人。v1 的处理是：分段结果默认按段着色并在 UI 标注「第 N 段 · 说话人 M」，提供**合并/重命名**工具让人一次性归并；不要假装它是全局身份。

### 6.3 归一化与词级展开（2026-11-06 起：一个 token = 一个 ASR 词）

ASR 只给**词级**时间戳，中文词常为 2–4 字。**编辑器的最小单位就是这个词**，不再往下切到字。

```
ASR word: { text: "今天我们来聊", begin: 1200, end: 1900, punctuation: "，" }
        ↓
一个词 = 一个 token（不再按字符切分，不再插值）
        ↓
token.start_ms = word.begin, token.end_ms = word.end
        ↓
derived = 0（时间是 ASR 真实观测到的，不是估算）
标点不单独成 token，挂在所属词的 punc_after
```

**为什么从字级退回词级（这是被推翻的旧设计）**

v1 按字符切分，把词的时长按字符数比例插值到每个字上，标记 `derived=1`。代价有两层：

1. 字级时间**根本不存在**，误差 = `词时长 / 字数`，中文典型 60–200ms/字，删一个字必然切在词中间；
2. 更要命的是**改字**：一次替换若新旧字数不同，就必须重新分配窗口内部的时间点，于是"改一个错别字"会悄悄挪动用户正在听的剪切点（实测：`14800–14960 / 14960–15200` 被改成 `14800–15000 / 15000–15200`）。

词级把这两个问题一并消掉：一个 token 就是一个观测区间，替换永远是 **1 token → 1 token**，两条边都是真的，内部没有可移动的时间点，**替换文本可以任意长**（"人工智能" → "AI" 也合法）。代价是选择粒度变粗——这是"永远不谎报时间"的诚实价格。

**保留的多单元路径**：`_split_units` 仍是通用函数。导入 SRT、或历史数据里一个"词"确实含多个单元时，仍会切成多个 token 并插值，此时 `derived=1`、`kind='char'`，UI 必须标注"时间为估算"。

**必须知道的后果**：

- 删除的最小单位是词。想剪掉一个词里的一半，只能在波形上框选时间区间（`deletion(source='range')`）。
- 不再需要 `min_cut_unit` 开关：词级本身就是最小单位，没有"按字剪会切在词中间"这回事。
- 词级 token 的 `punc_after` 可能挂多个标点（ASR 会把逗号、句号都塞进同一个词）。

### 6.4 句级 / 词级 标签模型

**内部表示：句 = token 区间**。不存"字属于哪个句"的父子指针，因为重划边界时父子指针会产生大量悬垂引用。

```
sentence = { ord, token_start, token_end }   // 半开区间
不变量 INV-1..4：
  INV-1  同一 version 内，句子按 ord 排序后 token 区间首尾相接、无空洞
  INV-2  首句 token_start == 0；末句 token_end == token_count
  INV-3  每句至少 1 个 token
  INV-4  任何编辑后立即重算 ord，并从 0 重新编号（ord 只是显示序，不是 ID）
```

`transcript_version.token_count` / `sentence_count` 是这两张表的**冗余缓存**，每次边界编辑或文本编辑都必须同步更新；它们被顶栏、存档和字幕面板读取，不同步就会出现"5 句的稿子显示 3 句"。

**用户操作 → 区间操作映射**

| 用户动作 | 语义 | 算法 |
|---|---|---|
| 多选词 [a,b)，**另起一句** | 在 a、b 两处各切一刀 | `split_at(a)` + `split_at(b)` |
| 多选词 [a,b)，**并入上句** | 上句延伸到 b | 若存在覆盖 [a,b) 的句边界，先 `merge_range(a,b)` 再 `merge_with_prev` |
| 多选词 [a,b)，**并入下句** | 下句提前到 a | `merge_with_next` + 对齐 |
| 删句 / 合并相邻两句 | 直接合并区间 | `merge(s_i, s_{i+1})` |
| 拆分 | 句中任意 token 处切 | `split_at(k)` |

所有操作以 `edit_op` 事件落库，**撤销 = 反向事件**，不做快照回滚（快照只用于崩溃恢复）。

**说话人是区间编辑的约束，不只是并列的一列。** 一句只有一个 `speaker_id`，所以：

| 操作 | 跨两个已标注且不同的说话人 |
|---|---|
| 合并 / 并入上句 / 并入下句（整句） | 拒绝，`speaker_conflict` |
| 并入上/下句（半句，把词搬到相邻句） | 拒绝——词的归属会跟着变 |
| 另起一句 | 允许，两半都继承原句的标注 |
| 一方未标注 | 允许，保留已有的那个标注 |

拒绝而不是「以第一句为准」，是因为后者会把 A 的提问算成 B 的回答——是事实错误，而用户从界面上看不出来。编辑器的句边界按钮在这种情况下置灰并**渲染**出原因：`disabled` 按钮收不到 pointer 事件，`title` 永远不会出现。

**持久化必须按 span 写 `speaker_id`。** `sentence.ord` 是位置而不是 ID（INV-4），切一刀之后下方所有句子的 ord 都变了，所以「ord 为 N 的数据库行」已经不是原来那句。`_persist_spans` 只更新区间、不更新 `speaker_id` 时，每行都留着**上一个占位者**的标注：切开「说话人1」的句子，后半截会继承下一句（说话人2）的标注；合并两句则静默丢掉被删那行的标注。撤销快照同理——`before` 里必须带 `speaker_id`，否则撤销只恢复形状，让每句指向此刻坐在那个 ord 上的人。

**文本纠错是另一维度**：改错字（如"旋枢"→"璇枢"）不改变区间，只改 `token.text`，记 `op_type='text'`，并把该 token 的 `revised` 置为 1——编辑器靠这个标志把"人写过的词"染蓝。**必须把"文本纠错"和"时间归属校正"分开记录**，否则统计不准、影响后续热词学习。

**说话人标注怎么存**：`sentence.speaker_id` 存的是**项目内局部整数**（不是 `speaker.id` 字符串），所以改名只碰一行、合并两人只需改整数。模型不做分人时（实测 `qwen-audio-3.1-asr-flash-filetrans` 对 17.6s 单人样本返回 0 个 speaker），用户可以手动 `POST /speakers` 建一个再指派句子。

### 6.5 剪辑：删除集 → EDL → 渲染
### 6.5 剪辑：删除集 → EDL → 渲染

#### 两种输入，一套模型

- 删除词级标签 → 该 token 的 `[start_ms, end_ms)` 加入 `deletion(source='token')`；
- 波形框选 → `deletion(source='range')`。

最终都进入区间代数：

```
1. 规整：按 start 排序，合并重叠/相邻（间隙 < 5ms 视为相邻）
2. 吸附（可选，默认开）：
   - 左右各扩展至最近的静音谷（-40dBFS 以下、≥120ms 的平台中心），
     上限 ±150ms；找不到静音就保持原边界
   - 目的：避免切在元音中间产生"半截字"
   - **静音检测结果必须走缓存**（`services/silence_map.py` → `media/silences.py`），
     键为「内容 sha256 + noise_db + min_duration」。否则每次 `build_edl`（每次编辑）
     都要重跑全片 `silencedetect`：实测 105 分钟素材 9.1s，首屏 `GET /edl`
     因此长时间拿不到 `deletions`，删除与导出按钮一直显示为灰色不可用。
3. 微调：start -= 20ms, end += 20ms（防吞字），但不越过相邻保留段
4. 剔除：保留段 < 80ms 的并入删除（避免"咔哒"碎片）
5. keep_ranges = [0, duration) \ delete_ranges
6. 若 keep_ranges 为空 → 拒绝渲染并报错
```

#### 时间轴映射（**关键，不可省**）

```python
# core/edl.py
@dataclass(frozen=True)
class TimeMap:
    segments: tuple[Segment, ...]   # (orig_start, orig_end, new_start)

    def to_new(self, t: int) -> int | None:
        """返回新时间轴位置；若 t 落在被删区间 → 返回最近保留点"""

    def to_old(self, t: int) -> int | None: ...
```

成片里所有字幕时间、章节时间、文稿时间戳都要走 `to_new()`。**这是本设计里最容易漏、且漏了会全盘作废的一环。**

#### ffmpeg 渲染

片段数可能上千 → 命令行会长到爆。用 `-filter_complex_script` 文件：

```
# 每个保留段
[0:a]atrim=start=S:end=E,asetpts=PTS-STARTPTS,
     afade=t=in:st=0:d=0.005,
     afade=t=out:st=<segdur-0.005>:d=0.005[a0];
...
[a0][a1]...[aN]concat=n=N:v=0:a=1[out]
```

- 输出：`-c:a aac -b:a 192k` 的 m4a，或用户要无损时 `-c:a pcm_s16le` wav；
- 采样率/声道沿用原始（不强制 16k，成片要给人听）；
- 单次渲染超 2000 段时降级为"分段渲染 + concat demuxer 二次拼接"。

> **为什么每个片段要 5ms fade**：不加必然在拼接点产生爆音。这不是"音效处理"，是剪辑正确性的一部分。文档里必须写明，防止后续 agent 以"用户说不管音效"为由删掉它。

#### 预览 vs 导出

- **快速预览**：只渲染当前播放位置前后 60s 的成片片段，缓存到 `derived/preview_<hash>.m4a`；
- **完整导出**：后台 job，进度 SSE；
- 前端波形双模式：`原始时间轴`（默认，红块表示删除）⇄ `成片时间轴`（只读，用于核对）。

### 6.6 字幕生成

```
token 流（已校正文本 + 已校正句边界）
  → 应用 TimeMap 到新时间轴
  → 按句输出 cue
  → 断行/时长合规化（见下）
  → 序列化
```

合规规则（可在设置里改，默认值）：

| 规则 | 默认 |
|---|---|
| 单行最大显示宽度 | 20 全角字（中） / 42 字符（英） |
| 每 cue 最多行数 | 2 |
| 阅读速度 CPS | ≤ 9 字/秒（中文）/ ≤ 17 字符/秒（英文） |
| 最短停留 | 1000ms |
| 最长停留 | 7000ms |
| 句间最小间隔 | 80ms |
| 超长句处理 | 按标点 → 按 token 边界两段切，禁止切在词内 |

导出：`srt` / `vtt` / `ass`（含基础样式） / `txt`（纯文本，分说话人）。

### 6.7 文稿生成

输入：校正后的句子序列（含 speaker、时间戳、章节标记）。

三种润色档位（用户选）：

| 档位 | 处理 | 用途 |
|---|---|---|
| `verbatim` | 仅去重、分说话人、加时间码 | 存档/检索 |
| `light` | 去口水词（嗯/啊/那个/就是说）、修明显语病、补标点、保留原句序 | **默认** |
| `editorial` | 分段、加小标题、合并问答、书面化改写 | 公众号/Newsletter |

结构（LLM 输出 schema 固定）：

```json
{
  "title": "...", "summary": "...",
  "sections": [
    {"heading": "...", "start_ms": 123456, "paragraphs": [
       {"speaker": "嘉宾A", "text": "...", "src_sentences": [12,13,14]}
    ]}
  ],
  "quotes": [{"text": "...", "start_ms": 0}],
  "shownotes": ["..."]
}
```

**可追溯性硬要求**：每个段落必须带 `src_sentences`，前端可点击跳回对应音频位置。没有这个字段的输出视为不合格。

导出：Markdown（默认）/ DOCX（python-docx，带标题层级）/ TXT。

---

## 7. 前端设计要点

### 7.1 波形与播放（性能生死线）

12 小时音频的坑：

- `decodeAudioData` 整段解码 = 12h × 16k × 2ch × 4B ≈ 5.5GB 内存 → **必崩**。
- 所以：**wavesurfer v7 + MediaElement 后端**（`<audio>` + `<audio>` 直接流式），峰值用后端预计算。

峰值文件格式（`peaks/<sha256>.pkc`）：

```
header: magic 'PKC1' | version | sample_rate | channels | duration_ms | levels
level k: buckets_per_second (8, 64, 512, 4096)
         每 bucket 存 (min_i16, max_i16) × channels
```

- 后端：`ffmpeg -i in -f s16le -ac N -` 流式读 → numpy 分块 → 逐级降采样，内存 O(block)。
- 前端按缩放级别选择 level，一次只取可视窗口 ± 2 屏的 bucket。**不要一次加载全文件**。
- 10h 音频按 512 buckets/s 约 1840 万 bucket，单 level 约 74MB → 必须分块 HTTP Range 拉取。

### 7.2 校正台布局

```
┌───────────────────────────────────────────────────────────────┐
│ 顶部：项目名 · 版本号 · 播放控制 · 时间码 · 顶栏读数           │
├──────────────────────┬────────────────────────────────────────┤
│  波形：删除块=红、    │  （宽屏时波形占满上方，此处为句列表）  │
│  ▲播放头              │                                        │
├──────────────────────┴────────────────────────────────────────┤
│ 句边界栏：[另起一句][并入上句][并入下句][合并句子]  选区摘要   │
├───────────────────────────────────────────────────────────────┤
│ 句列表（虚拟滚动）                                             │
│  ┌ ☐ 00:01:23  嘉宾A …   [已修改]        ▶                    │
│  │ 今天我们来聊…（整词可点，词间无空隙）                        │
│  │        ┌ ✂删除 ┐ ← 选中整词后贴着词下方浮出                 │
│  │        └ ✎修正 ┘                                            │
│  └                                                             │
└───────────────────────────────────────────────────────────────┘
```

**为什么句边界按钮搬进文字区**：它们的操作对象是"这段文字"，摆在舞台区的底栏里时，用户的视线要在两个区域之间来回跳。搬到句列表上方后，按钮、选区摘要和它作用的文字在同一列里。

**为什么不再有"改文字"输入框**：表单是第二个需要维护的状态（打开、取消、当前目标），并且和选区状态容易不同步（旧实现里按钮曾把 `undefined` 当提案发出去）。改成贴着选区的气泡后，"选中 → 点"是同一个动作的延续。

### 7.3 词级选择交互

- 句内每个 token 渲染为一个可点格子（不是 contenteditable）。**这是为了能用区间运算，而不是依赖 DOM 选区。**
- 点击 = 选中**整词**；Shift+点击 = 连选到该词；拖拽划过 = 按词范围选（不会选到半个词）。
- 选中态 = `{from, to}` 的 token 半开区间，句选与词选互斥（一次只有一个明确目标）。
- 选中后浮出气泡：**✂ 删除**（剪掉这些词的音频）、**✎ 修正**（改这一个词的文字）。修正只在单选一个词时可用——多词替换会重新分配窗口内部时间点，正是词级设计要避免的。
- 词的状态用颜色说话，且**落库**（不是会话态）：
  - 已删除 = 红字 + 删除线 + 淡红底；
  - 部分删除（边界拖动啃掉词的一边）= 红字 + 点线删除线（不能声称整词都没了）；
  - 已修正 = 蓝字 + 淡蓝底 + 下方蓝线 + 角标 ✎。
- 空格键播放/暂停，`[` `]` 上一句/下一句，`Esc` 取消选择/关闭气泡。

**一个必须存在的限制**：词级意味着**改不了"半个词"**。想删掉"人工智能"里的"人工"，只能整词删除，或在波形上框选时间区间。

**替换长度不受限**：词级下一条边都是真观测，所以「人工智能 → AI」压缩 4 个 token 到 1 个 token 是合法的。UI 只在"新文本显著快于原音"时给一句提示，因为这种字幕读起来会别扭，但**不阻止**。

### 7.4 删除边界可视

- 词级删除：红块覆盖 token 的 `[start,end)`；
- 框选删除：自由 region，拖动边缘调整；
- 吸附到的静音谷位置画一条淡蓝色竖线，让用户知道"系统替你挪了 40ms"；
- **吸附只能向外**（起点只左移、终点只右移）。早期实现会让吸附把切点往里推，于是用户删到 6010ms、实际只删到 5991ms，有 19ms 他删掉的音频又回来了。

### 7.5 性能预算
### 7.5 性能预算

| 场景 | 指标 |
|---|---|
| 首次打开 3h 项目 | < 2s 可交互（波形局部先行渲染） |
| 10 万字句列表 | 虚拟滚动，滚动 60fps |
| 选中/重划边界 | < 50ms 本地乐观更新，后台静默同步 |
| 播放拖动 | < 120ms 起播 |
| 一次完整渲染 3h（约 300 段） | < 90s（含编码） |

---

## 8. API 草案（REST + SSE）

```
POST   /api/projects                          创建
GET    /api/projects/{id}
POST   /api/projects/{id}/media               上传原始音频（multipart，分片）
GET    /api/projects/{id}/media/{mid}/waveform?level=&from=&to=

POST   /api/projects/{id}/outline             保存大纲
POST   /api/projects/{id}/hotwords/suggest    大纲→当期热词（返回候选，不直接写入）
GET/POST/PATCH/DELETE /api/projects/{id}/hotwords        当期热词 CRUD
GET/POST/PATCH/DELETE /api/hotwords           全局词库 CRUD

POST   /api/projects/{id}/transcribe          提交识别 job → {job_id}
GET    /api/jobs/{job_id}                     轮询
GET    /api/projects/{id}/events              SSE：job 进度、渲染进度

GET    /api/versions/{vid}/transcript?from=&to=        句子分页
PATCH  /api/versions/{vid}/sentences/{sid}             改文本
POST   /api/versions/{vid}/boundaries                  重划句边界
       body: {action:'new_sentence'|'to_prev'|'to_next', from:int, to:int}
POST   /api/versions/{vid}/undo  |  /redo

POST   /api/versions/{vid}/deletions                   删除（token 或 range）
DELETE /api/versions/{vid}/deletions/{id}
GET    /api/versions/{vid}/edl                         预算 EDL + 新时长
POST   /api/versions/{vid}/render                      排队渲染，立刻返回 {id,status,progress}
GET    /api/renders/{id}                               轮询进度 0..1 / status / error
GET    /api/renders/{id}/file                          下载

POST   /api/projects/{id}/script/generate              生成文稿
GET    /api/projects/{id}/script
POST   /api/projects/{id}/export                       导出（kind 参数）
```

约定：
- 所有写操作返回 `{data, edit_op_id}`，便于前端撤销栈与后端对齐；
- 错误统一 `{error:{code, message, hint}}`，`hint` 必须是**可执行建议**（例如"文件超过 1GB，已自动转码为 64kbps 单声道"）；
- 时间一律毫秒整数，字段名以 `_ms` 结尾，禁止秒/浮点混用。

---

## 9. 质量与测试

### 9.1 必测的纯函数（`core/`，覆盖率 ≥90%）

| 模块 | 用例要点 |
|---|---|
| `tokens.expand_words` | 中英混排、数字、标点、零时长词、时间倒挂（end<start）容错 |
| `sentence.split_at` | 切点在句首/句尾/句中/标点处；切完不变量 INV-1..4 仍成立 |
| `sentence.merge_range` | 跨 3 句合并；连续同 speaker 是否自动合并（是） |
| `edl.compile` | 重叠删除、相邻删除、全删（报错）、删除跨越整句、范围超出媒体时长 |
| `edl.TimeMap` | 单调性、边界点、落在删除区间的查询、往返映射 |
| `subtitle.wrap` | 超长句、CPS 超限、英文断词（不能在单词中间断） |
| `hotword.sanitize` | 15 字符/7 词限制、去重、权重合并 |

### 9.2 集成测试（需要真实 API，用 `pytest -m live` 隔离）

- 3 分钟样例音频：上传 → 转写 → 下载 → 归一化 → 断言句子数 > 0、时间单调、末句 end ≤ duration + 500ms；
- 热词生效验证：造一段含生僻专名的音频，对比加热词前后文本差异。**注意断言方式**——不能断言"变好了"，因为 §6.1 已实测热词可能反而变差。正确断言是"两次结果都合法（时间轴单调、句区间完整）且可 diff"，优劣由人工或词错误率指标判断。
- 渲染验证：删除中间 5s → 输出时长 = 原时长 - 5s ± 100ms（因为 fade 不改时长，误差来自吸附）。

### 9.3 回归基线

`tests/fixtures/` 放 3 个固定音频（30s 单speaker / 3min 双speaker / 10min 中英混排）+ 期望的 token 快照（JSON）。算法改动必须过快照 diff。

---

## 10. 错误处理与可恢复性

| 场景 | 策略 |
|---|---|
| getPolicy 凭证过期（300s） | 重新获取，最多 3 次；大文件先探测上传耗时 |
| OSS 上传中断 | httpx 分块 + 断点（v1 简化为整包重传，>200MB 提示） |
| 识别任务超时 | 45min 上限；超时可"用原 task_id 继续查"，不重复提交（避免重复计费） |
| transcription_url 过期（24h） | 打开旧项目时若 raw.json 缺失 → 明确提示"结果已过期，需重新识别" |
| 进程崩溃 | 启动时扫 `jobs` 表：`running` → 重置为 `queued`；`edits/ops.jsonl` 与 DB 比对，以日志为准重放 |
| 磁盘满 | 渲染前预估输出大小（码率 × 时长），不足则拒绝并给数字 |
| 素材被移动/删除 | 启动时校验 sha256，缺失则项目置只读并提示重新关联 |

---

## 11. 安全与合规

- API Key 存 OS 凭据库（Windows Credential Manager / keyring），`.env` 仅供开发；**Key 永不下发前端**。
- 服务只绑 `127.0.0.1`；所有写接口校验 `Origin`，防本地网页 CSRF。
- 上传到百炼临时存储 = 音频**离开本机**，首次使用必须弹一次明确告知（含"48 小时后自动清理"）。
- 日志脱敏：不打印 `oss://` 全路径、不打印 API Key、句子内容默认不进日志（只记长度）。
- 敏感词：默认为项目配置 `special_word_filter`，把 `system_reserved_filter` 设为 `false` 并显式给出 `filter_with_signed/filter_with_empty` 白名单空格，避免播客被系统词表误伤成 `***`。**这一项要在首次识别前让用户确认。**

---

## 12. 成本与容量

| 项 | 估算 |
|---|---|
| 转写（Qwen-Audio-ASR-Filetrans，按 Token） | 3h 中文约 4–5 万 token 输入；具体单价以控制台为准，系统内只记录 `usage` 与累计估算 |
| 热词 | 免费 |
| 临时 OSS | 免费，48h 自动清理，单文件 1GB |
| 大纲抽词（qwen-plus） | 单次 < 5k token |
| 文稿生成 | 3h 转录 ≈ 4–5 万字 ≈ 6–8 万 token（需分段摘要 + 合并，见下） |
| 本地磁盘 | 3h 原始 mp3 ≈ 170MB；peaks 三级 ≈ 60MB；成片 ≈ 200MB |

**长文稿必须分段处理**：3h 转录远超单次上下文舒适区。策略：按章节（人工或静音/话题切分）→ 分段生成 → 二次合并摘要。这一步要单独设计，不能一把梭。

---

## 13. 里程碑

| M | 内容 | 完成判据 |
|---|---|---|
| **M0 ✅** | 骨架：repo 结构、配置、SQLite 迁移、ffmpeg 自检、健康检查、项目 CRUD | ✅ 见下方「M0 交付清单」 |
| **M1 ✅** | 转写闭环：上传→识别→下载→归一化→入库 | ✅ 见下方「M1 交付清单」 |
| **M2a ✅** | 热词库：全局/当期词库、词条规范化、大纲抽词 | ✅ 真实 LLM 抽出 11 条高质量候选 |
| **M2b ✅** | LLM 校对：候选生成 + 区间内重排 + 校对台接口 | ✅ 真实 LLM 精确命中植入错写，零误报 |
| M2c | 影子测试（采样 A/B）+ 权重推荐 | **刻意推迟**：需真实播出数据才有效，现阶段 ROI 低 |
| **M3 ✅** | 校正台 UI：波形 + 句列表 + 词级选择 + 重划边界 + 边界拖动 | ✅ 见「M3 交付清单」 |
| **M4 ✅** | 剪辑：删除集、EDL、渲染、时间轴重映射、实时试听 | ✅ 成片时长误差 **0ms**，见「M4 交付清单」 |
| **M5a ✅** | 字幕导出：SRT / VTT / TXT，映射到成片时间轴 | ✅ 见「M5a 交付清单」 |
| M5b | 文稿生成：结构化 DOCX / Markdown 导出 | 待做 |
| **§ 工作台** | 菜单栏、三栏布局、设置、存档、操作记录 | ✅ 见「工作台（2026-11-05）」 |
| M6 | 稳定性：崩溃恢复、错误提示、成本面板、打包 | 断电重开不丢编辑 |

### 进度一览（2026-11-05 末）

```
M0 ✅ 骨架          M1 ✅ 转写闭环       M2a ✅ 热词库
M2b ✅ LLM 校对     M2c ⏸  影子测试(推迟)  M3 ✅ 校正台 UI
M4 ✅ 剪辑+时间轴   M5a ✅ 字幕导出       M5b ⬜ 文稿生成
M6 ⬜ 稳定性
工作台 ✅ 菜单/三栏/设置/存档/操作记录
```

**验证规模**：后端 377 个测试通过（`mypy --strict` 零错误），前端 42 条逻辑测试，
真实音频端到端跑通（17.6s 样本，转写→校正→删除→渲染→导出字幕）。

### M0 交付清单（2026-11-05）

```
apps/api/src/animatter/
  config.py      配置 + loopback 强校验（拒绝 0.0.0.0）
  log.py         structlog + sk-*/Bearer/signature 脱敏
  ids.py         可排序前缀 ID（prj_…）+ UTC 时间戳
  db.py          SQLite 引擎 + WAL/foreign_keys/busy_timeout pragma
  models.py      16 张表（project/media/speaker/transcribe_job/…/job）
  migrate.py     启动时 alembic upgrade head（幂等）
  errors.py      AppError(code, message, hint) → 统一 JSON
  main.py        应用工厂 + Origin 写保护 + 请求耗时头
  routers/       /healthz、/api/projects
  media/probe.py ffmpeg/ffprobe 自检（版本、路径、安装提示）
  __main__.py    serve | check | db-upgrade | version
apps/api/alembic/  初始迁移（autogenerate）
apps/api/tests/    22 个用例：配置不变量、ID、健康检查降级、项目 CRUD、Origin 防护
scripts/dev.ps1    一键：建 venv → 装依赖 → check → migrate → serve --reload
```

验证结果：`pytest` 22 passed、`ruff check` 全绿、`mypy --strict` 18 文件无错、`/healthz` 200（ffmpeg 7.1 / 磁盘 11.8GB / 无密钥时仅该检查为 false 且不返回 503）。

**与设计的差异**：`make` 在目标机器不可用，改用 `scripts/dev.ps1`；包管理未用 `uv`（未安装），用 venv + pip。

### M1 交付清单（2026-11-05）

```
apps/api/src/animatter/
  core/tokens.py        word → 词级 token（一个 ASR 词 = 一个 token，时间原样透传）
  core/sentence.py      句区间代数：split_at / merge_range / extend_into + INV-1..4 校验
  media/ffmpeg.py       probe / 转码 16k 单声道 mp3 / 分段切片 / silencedetect / 切点选择
  providers/http.py     统一 httpx 客户端（trust_env=False，规避本机 SOCKS 代理）
  providers/storage/temp_oss.py  getPolicy → multipart 流式上传 → oss://（1GB/300s 约束）
  providers/asr/base.py          AsrCapabilities/AsrRequest/AsrResult + Protocol
  providers/asr/bailian.py       提交/轮询/下载 + 模型能力表 + 参数按能力裁剪
  services/normalize.py  ASR JSON → token 流 + 句区间（含分段 offset 与 merge）
  services/chunking.py   >2h 分段规划（静音中点优先，退化 forced）
  services/transcribe.py 完整用例：转码→规划→上传→提交→轮询→下载→归一化→落库
  cli_transcribe.py      `animatter transcribe` 端到端命令
  routers/transcribe.py  登记媒体 / 转写 / 版本列表 / 转录分页读取 / 统计
apps/api/tests/  411 个用例（+3 deselected live），覆盖词级展开、句区间不变量、
                 offset 拼接、切点选择、请求体 wire format、per-key 能力裁剪
samples/speech.wav  17.6s 中文测试音频（SAPI 合成）
```

**真实验证结果**（API Key + 工作空间域名 `ws-n330k7al4hg4i087.cn-beijing.maas.aliyuncs.com`）：

| 项 | 结果 |
|---|---|
| `list_vocabulary` | 200（鉴权与工作空间正确） |
| 端到端转写 17.6s 音频 | 成功：3 句 / 61 字 / `derived_ratio=0.852` |
| 中文文本正确性 | ✅「今天我们来聊一聊播客剪辑。首先介绍一下嘉宾张三…」 |
| 词级切分 | ✅ 一个 ASR 词 = 一个 token，标点挂在词的 `punc_after` |
| 时间轴单调性 | ✅ 无倒挂 |
| live 集成测试 | ✅ 3.6s 音频，1 句 12 token，时间轴断言全过 |
| 总测试 | **119 passed / 1 deselected（live）** |

**两个 M1 期间发现并修正的真实缺陷**：

1. **配置加载漏检**：`.env` 只在当前工作目录找，从 `apps/api` 启动时静默读不到 key，表现为"未配置 API Key"。改为**向上逐级搜索** `_find_env_files()`。
2. **「并入整句」无出口**：`extend_into` 对整句选区直接报错，用户想把整句并到上句时无路可走。改为**整句选区退化为 merge**——合并两个句子不该让用户去找另一个按钮。

**一个重要实测结论**（见 §6.1）：热词会过度纠偏，`weight=5` 能把正确的「句级」拉坏成「剧集」。已在 provider 层把默认权重定为 4，并规定热词默认关闭 + 提供 A/B 对照。

**M1 之前不要动前端**。转写链路的不确定性（鉴权、地域、模型名、OSS 头）必须在 CLI 里先全部打穿。

---

## 14. 已定决策

2026-11-05 评审通过，按此执行。表中「备注」是执行时要知道的后果。

| # | 决策 | 结论 | 备注 |
|---|---|---|---|
| D1 | 应用形态 | **本地 Web**（FastAPI + React，127.0.0.1），M6 再用 Tauri 打包 | 浏览器入口与桌面壳共用同一套前端 |
| D2 | 识别模型 | **`qwen-audio-3.1-asr-flash-filetrans`** 为默认；`paraformer-v2` 作降级 | 降级时 `vocabulary` 即时热词不可用，只能走 `vocabulary_id`；能力差异必须在 provider 层声明 |
| ~~D3~~ | ~~字级删除粒度~~ | **已被词级设计取代（2026-11-06）**：最小单位就是词，不存在"删字切在词中间"；要剪词中间只能在波形上框选时间区间 | — |
| D4 | 剪辑质量 | **静音吸附 + 5ms fade**，两者都可关 | 关闭后渲染结果可能爆音，UI 需在关闭时警告一次 |
| D5 | 文稿整理 | LLM 生成，保留 `verbatim` 档位 | 每段必须带 `src_sentences` 才允许导出 |
| D6 | 立体声分轨 | 自动检测左右声道相关性（<0.5 视为分轨），**提示**用户选择 | 分轨时两次识别分别落 `channel_id=0/1`，计费 ×2，必须写进确认弹窗 |
| D7 | 说话人分离 | **≤2h 默认开启**；`speaker_count` 默认 2 | 只对单声道生效；分轨场景改用 channel 归属，二者互斥 |
| D8 | 长音频 | **>2h 自动分段**（每段目标 90min，切在静音中点），见 §6.2 | 原本的「v1 拒绝」作废；分段引入「说话人局部编号」问题，§6.2 已写处理方式 |
| D9 | 视频播客 | 只剪音轨，画面不动 | 后续若需同源裁剪，另立 EDL→视频滤镜链路 |
| D10 | 项目模型 | 多项目 | 数据表已按 `project_id` 全面隔离；全局热词用 `project_id IS NULL` 表达 |

> **变更记录**：D8 在评审时由「v1 拒绝 >12h」改为「>2h 自动分段」。触发原因：真实播客常见 2.5–3.5h，直接拒绝会让产品在最需要的场景失效。

---

## 15. 容易被漏掉的点（设计评审清单）

> 这一节是给 review 用的。逐条确认"是/否/不需要"，比读十页架构图有价值。

### 15.1 时间与精度

1. ~~字级时间戳是插值出来的，不是 ASR 给的~~ → **已改为词级**：一个 token 就是一个观测区间，没有插值，也没有"切在词中间"。
2. **剪辑后时间轴必须重映射**，否则字幕整体错位，且越到后面错得越多。这是漏了就会全盘返工的一条。
3. **删除区间吸附到静音谷**能显著提升可听性，但要给用户可见反馈（淡蓝线），否则会觉得"它偷偷挪我的剪辑点"。
4. 拼接点不加 fade 会爆音。这是物理问题，不是音效偏好。
5. ASR 的 `begin_time/end_time` 偶发倒挂或零时长，归一化必须有容错。
6. **分段相接处是第二大误差源**：切点没落在静音中点、或句合并判错，会让成片在接缝处「咯噔」一下。建议渲染前对每个接缝做一次能量检查（接缝 ±50ms 内出现语音峰值 → 标 `seam_quality='suspect'`），并在校正台里高亮。

### 15.2 数据与版本

6. **重跑识别如何不丢已做的编辑？** 编辑绑定 `version_id`，新版本默认不继承；需要"迁移"功能时按句子文本相似度做映射（v1.5）。v1 明确提示"重新识别会新建版本，旧编辑保留在旧版本"。
7. **句边界编辑与文本纠错要分开记录**，否则无法统计"哪类错误最多"，也无法反哺热词。
8. **撤销栈**：本地 20 步 + 服务端事件日志，两者必须能对齐（用 `edit_op_id`）。
9. **自动保存粒度**：每次 edit_op 立即落库（SQLite 写很快），不要做"每 30 秒存一次"。

### 15.3 云 API 的坑

10. **上传时的 model 必须与识别时的 model 完全一致**，否则文件不可用（不报错，只是找不到）。
11. **`oss://` + HTTP 直连必须带 `X-DashScope-OssResourceResolve: enable`**，SDK 反而用不了。
12. **预编译词表 50 个配额 + 5 分钟更新冷却** → 不能"每次识别建个新表"，必须长期维护少数几个表。
13. **热词超 2000 会被随机丢弃**，不是报错、不是截断末尾 → 自己先截。
14. **临时存储只有 48h**：今天转写完，明天想看结果，必须本地已落盘（我们落 `raw.json`）。
15. **系统敏感词表默认生效**，会把命中词替换成 `***` 且不告诉你 → 播客里聊到某些话题会莫名其妙出现星号。
16. **上下文增强每轮 ≤400 字符**，塞大纲塞不进去，只能当"术语串"用。
17. **12h WAV 超 1GB 上传限制**，必须预转码。
18. 查询接口 20 QPS，多个项目并行识别时轮询要合并到一个调度器，别一个一个死轮询。

### 15.4 交互与可用性

19. **校对效率**：仅靠鼠标点句子会累死人。必须有"空格播放当前句 / 自动播下一句"的连续校对流。
20. **词级标签要能看见时间**：hover 显示 `00:01:23.450–00:01:23.780`，否则用户没法判断该不该删。
21. **删错了怎么恢复**：删除是"软删除"（`removed=1`），列表里有"已删除"折叠区可恢复，比撤销栈更符合直觉。
22. **抖动/误删检测**：连续删除产生 <80ms 碎片时要自动并入相邻删除，否则成片会"咯噔咯噔"。
23. 长句在列表里要显示"超长"警告（CPS 超限），提前提示要断句。
24. 说话人改名（"说话人1" → "张三"）是高频操作，要能全局批量改。

### 15.5 内容与导出

25. **标点规范化**：中文全角、英文半角、数字与单位间的空格、省略号统一。ASR 的标点很随意。
26. **中英混排断行**：不能把 "Kubernetes" 断成 "Kuber" + "netes"。
27. **文稿的可追溯性**：每段能跳回音频，否则编辑不敢改。
28. **导出文件的命名规则**（含日期、版本、时长），否则用户桌面会堆 `final_final_v3`。
29. **章节/Show notes** 是播客的实际交付物之一，建议与文稿一起生成。

### 15.6 工程

30. **长任务不能跑在 HTTP 请求里**（浏览器 5 分钟超时）→ 必须 job + SSE。
31. **ffmpeg 是外部依赖**：启动自检，给出明确的安装指引，不要等到渲染时才报 `FileNotFoundError`。
32. **Windows 路径**：中文路径、空格、超长路径都要测（本项目就在 Windows 上）。
33. **峰值缓存要能失效**：媒体文件变了（sha256 变）→ 缓存键跟着变，别用文件名。
34. **磁盘占用清理**：`derived/preview_*` 要有 TTL 清理策略。

---

## 16. Agent 工作约定

### 16.0 协作偏好（用户明确要求，跨会话有效）

- **大型任务默认走 OpenDesign**（`dsh-opendesign-go`）provider，前提是额度还有余量。
- **token 要省**：非紧急任务不要拿贵模型通刷；小步验证、少来回、别重复读同一份材料。
- **OpenDesign 故障时**：先诊断并尝试解决故障（key 是否失效 / 额度 / 代理节点 / 网关状态），
  解决后**等用户切回 OpenDesign 再继续**；不要在降级模型上把大任务跑完，也不要反复重试烧额度。

### 16.1 动手前

- 先读本文件相关章节，确认改动落在哪一层；跨层改动先提方案。
- 涉及百炼 API 的改动，**必须**先核对官方文档（`help.aliyun.com/zh/model-studio/...`），不得凭记忆写参数名。
- 不确定的产品行为，先在 `docs/decisions/` 写 ADR，再实现。

### 16.2 代码规范

- Python：`ruff` + `mypy --strict`（`core/` 与 `services/` 必须严格）；公开函数必须有类型注解。
- 时间：内部一律 `int` 毫秒。**禁止 `float` 秒**。禁止 `datetime` 出现在 core 里。
- 区间：一律半开 `[start, end)`，命名 `*_start` / `*_end`，禁止 `begin/end` 混用。
- 错误：`core/` 抛领域异常（`InvalidRangeError` 等），`services/` 转成 `AppError(code, message, hint)`。
- 前端：TS `strict`，禁止 `any`；状态更新必须走 Zustand action，组件内不直接 mutate。
- 提交粒度：一个 commit 一个可回滚的语义单元；涉及 core 算法必须带测试。

### 16.3 禁止事项

- ❌ 在 `core/` 里 import `httpx` / `sqlalchemy` / 任何 IO。
- ❌ 把 API Key 写进代码、日志、前端 bundle。
- ❌ 为"省事"把编辑直接写进原始媒体文件。
- ❌ 在渲染链路里悄悄改用户的时间点（吸附必须有日志、可关闭）。
- ❌ 引入新的重型依赖（AV 框架、前端编辑器框架）而不先改本文件。

### 16.4 每完成一个里程碑

- 更新本文件"当前状态"与对应章节；
- 补 `docs/` 下的运行手册（如何配 Key、如何装 ffmpeg、常见错误码对照表）；
- 跑一遍 `tests/fixtures` 快照回归。

### 16.5 任务栏（todo）纪律

任务栏是**自报状态**，不是从代码或测试推断出来的：`todo_write` 整体替换列表，
harness 没有"回合结束自动同步"这回事。于是列表**只会在显式写入时前进**，
一旦某一轮停止调用它就停在最后一次写入的位置——这就是"1 进行中 / 7 待处理"
滞留的全部原因。上下文压缩会让它更糟：**列表本身存活，产生它的理由不存活**，
压缩后的模型甚至看不见它曾经写过什么，只能另起一份新列表，旧的那份就成孤儿。

规则：

- **不要为探索性工作建长期列表。** 只有"确定会在一轮内走完的多步任务"才建；
  一次做完的活写 3–6 条，不要写 10 条。
- 跨轮的列表必须**自解释**：每一步是一个可验收的交付物（"迁移幂等，31 不再是 62"），
  不是动作（"修迁移"）。压缩之后仍要能读懂。
- **每个回合结束前收口**：列表要么清空，要么只留一个 `in_progress`，
  且它的下一步具体到可以直接执行。默认是"做完就清"——
  全绿的列表只会让任务栏堆一排噪音，用户并不需要看已完成项。
- 状态由自己维护，不要留给用户去发现不一致。用户能看见任务栏，我看不见。

---

## 17. 附：一次典型会话的完整时序

```
[用户] 拖入 episode_42.mp3（2h13m，立体声）
  ↓ ffprobe
[系统] 检测到左右声道相关性 0.31 → 提示「像是分轨录音，是否分别识别？」
[用户] 否，混单声道
  ↓
[系统] 转码 asr_input.mp3（32MB）→ getPolicy(model=qwen-audio-3.1-asr-flash-filetrans)
       → 上传 → oss://dashscope-instant/...
[用户] （同时）粘贴大纲，点「生成当期热词」→ 得到 87 条候选，删掉 12 条明显误判
  ↓
[系统] 合并全局热词 214 条 → 共 289 条（<1800，安全）
       → POST /transcription（vocabulary + vocabulary_id + context 术语串）
       → 轮询 → 下载 raw.json（146MB）→ 归一化
[系统] 产出 1,842 句 / 38,761 个 token / 2 位说话人
  ↓
[用户] 校正 40 分钟：改错字 63 处；把 9 个句子边界重划；删掉 3 段跑题（共 7m12s）
  ↓
[系统] EDL：89 个删除区间，合并后 71 段保留区，成片 2h05m48s
       → 渲染（fade 5ms，静音吸附开）→ 用时 74s
       → TimeMap 就位
  ↓
[系统] 生成 SRT（CPS 合规 100%）→ 生成文稿（light 档）→ 导出 md + docx
[用户] 点文稿第 3 节 → 播放器跳到 00:47:12 → 核对无误
```

---

## 18. 参考

- 百炼录音文件识别 REST：<https://help.aliyun.com/zh/model-studio/paraformer-recorded-speech-recognition-restful-api>
- 百炼非实时语音识别用户指南（模型/限制/上下文/说话人）：<https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide>
- Qwen-Audio-ASR-Filetrans HTTP API（即时热词/context 参数）：<https://help.aliyun.com/zh/model-studio/qwen-audio-asr-filetrans-http-api>
- 提升识别准确率（热词规范、权重、配额、上下文 400 字限制）：<https://help.aliyun.com/zh/model-studio/improve-asr-accuracy>
- 定制热词 HTTP API（创建/更新/删除词表）：<https://help.aliyun.com/zh/model-studio/vocabulary-http-api>
- 上传本地文件获取临时 URL（getPolicy / 1GB / 48h / header）：<https://help.aliyun.com/zh/model-studio/get-temporary-file-url>
- 语音识别模型总览（时长、格式、语言）：<https://help.aliyun.com/zh/model-studio/asr-model>

### M2a / M2b 交付清单（2026-11-05）

```
apps/api/src/animatter/
  core/hotwords.py     词条规范化：长度/停用词/通用词降权/去重/截断 + 词表合并
  core/realign.py      区间内重排：改写文本但**不动时间边界** + 单调性断言
  providers/llm/bailian.py  兼容模式 chat + JSON 解析（容忍 ``` 围栏）
  services/hotwords.py  全局词库 ↔ 预编译词表（hash + dirty + 冷却）/ 当期词库
  services/outline.py   大纲 → 热词候选（提示词 + 后处理 + evidence）
  services/proofread.py 校对候选生成：三重护栏（见下）
  services/corrections.py 应用/拒绝候选 + 句子区间重建 + edit_op
  routers/hotwords.py, routers/proofread.py
alembic/versions/2007637db423_correction_candidates.py
```

**M2b 的三重护栏**（防止模型幻觉污染字幕）：

1. **必须有热词依据**：`matched_hotword` 不在词表里的候选，直接丢弃。这条把
   「润色型建议」和「臆造修改」全部挡在队列外。
2. **原文必须逐字定位**：`original` 在该句 token 流里找不到就拒绝。
   模糊匹配会改错位置，所以宁可不做。
3. **不改动时间轴**：`apply_replacement` 只允许区间内重排，区间首尾毫秒不变；
   跨边界、空替换、时间倒挂一律抛错。

**真实 LLM 验证**：植入一处同音错写「剧集标签」（正确应为「句级标签」），
模型输出**恰好一条**候选、置信度 0.95、零误报，消耗 454 prompt token。

**M2b 修掉的真实缺陷**：

- `shift_ranges_after` 在「编辑点正好落在句子起点」时会把整句平移，
  导致编辑点从所有句子区间里漏掉（违反 INV-1）。改为只有句尾移动。
- 兼容模式端点路径写错（`/api/v1/compatible-mode/...` → 404），
  正确是域名根下的 `/compatible-mode/v1`。
- 测试数据 `sizes=[4,4]` 与 7 字文本不匹配，暴露了区间校验缺失。

**验证**：242 个单测通过（新增 60 个），3 个 live 测试（转写/抽词/校对）全过。

## M3 校正台（2026-11-05）

### 后端补的两块

**波形峰值（`media/peaks.py`）**：3 小时音频解码成 float PCM 约 1.9GB，
浏览器直接 `decodeAudioData` 必崩（§7.1）。改为后端一次解码、逐级降采样成
min/max 桶，写入 `PKC1` 二进制缓存（键是媒体 sha256，不是文件名）。

| 级别 | 用途 |
|---|---|
| 8 buckets/s | 全览 |
| 64 | 默认视图 |
| 512 / 4096 | 放大到词级 |

**编辑接口（`services/editing.py`）**：句边界（另起一句/并入上下句/合并）、
文字修改、撤销。句边界与文字修改用不同 `op_type` 分开记录——这是 §6.4 的要求，
否则无法统计「哪类错误最多」。

### 前端要点

- **波形不用 wavesurfer 解码路径**，只用 canvas 画后端给的桶；播放用原生
  `<audio>` + HTTP Range，拖动不重下整文件。
- **词级选择是 token 点击/拖拽**，不是 DOM 选区。所有编辑都归约为 token 区间运算，
  contenteditable 的选区模型对不上（§7.3）。
- **文字修改必须基于选区**：后端在 token 流里逐字定位 `original`，而标点存在
  `punc_after` 而非 token 文本里，所以整句文本（含标点）永远定位不到。
  要求先选字，从构造上保证请求可定位。
- 快捷键：空格播当前句、`[` `]` 上下句、`Esc` 清选区。

### M3 修掉的真实缺陷（很重要）

**同字数替换会移动内部时间边界。** 把「句级」改成「句集」（2 字换 2 字），
原时间 `14800–14960 / 14960–15200` 被重新插值成 `14800–15000 / 15000–15200`。
音频根本没变，用户听到的切点却漂了 40ms。

根因：`apply_replacement` 只校验区间**首尾**边界，中间 token 的边界被
按字符数重新分配。修正为「同字数替换逐位复用原时间」，并把不变量加严为
**区间内每个 token 的边界都必须不变**。

这个 bug 单测没抓到——原有测试只断言了首尾和整体文本。现在补了两个回归测试，
其中一个用真实 ASR 那种不均匀的字词时长（180/220/600ms）来验证。

### 前端测试

`node --test src/logic.test.mjs`：9 条用例覆盖选区语义、活动句判定、CPS 计算、
半开区间边界。加它是因为 tsc 抓不到「参数个数对但值错」这类 bug——
文字修改按钮一度把 `undefined` 当 `proposed` 发给后端。

### M3 补充：键盘导航与边界拖动（用户反馈后）

**修掉三个真实 bug**

1. **音频根本播不了，导致快捷键像没实现。** 音频接口把 WAV 硬编码成
   `Content-Type: audio/mpeg`，浏览器拒绝解码，`play()` 静默失败。
   `[ ]` 的键盘处理器一直是对的，只是没有任何声音。
   顺带修了另外两点：`Response` 本身不处理 Range（只声明 `Accept-Ranges`
   是假的），以及 `read_bytes()` 每次请求都把整个文件读进内存。改用
   `FileResponse` + 按 codec 推断 MIME。

2. **`[ ]` 在句间静音处失效。** 原来用 `activeOrd` 定位，而播放头落在句间
   空白时 `activeOrd === -1`，`findIndex` 返回 -1，`-1 + 1 = 0`——
   两个方向都跳到第一句。改为按时间位置查找（`sentenceAtOrBefore`），
   并让 `[` 先回退到当前句开头、再退到上一句。

3. **空格键被按钮抢走。** 按钮点击后保持焦点，空格触发的是按钮而不是播放。
   现在按钮聚焦时空格/回车交还给按钮，其余情况才归播放；同时忽略输入法
   组字状态（中文输入法会吞掉 `[` `]`）。

**新增能力：在波形上拖动句边界**

设计取舍：
- 只有**句首首字**和**句末尾字**可拖。中间的 token 不可拖——移动它会
  让句子自身的区间失效，波形拖动给不出合理的修复方式。
- 拖动只改**被拖的那个 token**，其他 token 一个毫秒都不动。
  这是用户明确要求的，也是唯一安全的语义。
- 越界自动夹紧并说明原因（「不能晚于 400ms（会与相邻字重叠）」），
  而不是静默忽略——用户需要知道为什么拖不动。
- 拖动后该 token 的 `derived` 置为 false：它不再是从 ASR 插值来的，
  而是人定的。

实测（真实数据）：把句 0 末字从 `2660–2920` 拖到 `2660–3040`，
61 个 token 中**只有 idx=11 变化**，句 1 的 `3840–6080` 完全未动。

## M4 剪辑（2026-11-05）

### 核心：EDL 与时间轴重映射

`core/edl.py` 是纯函数层，两条不可妥协的性质：

1. **删除集是唯一真相，保留段是推导出来的。** 用户删东西，不是指定保留什么。
   推导意味着撤销一次删除只需删掉那条记录，不需要重建任何东西。
2. **成片之后所有时间都要过 `TimeMap`。** 字幕、章节、文稿导出引用的都是
   原始时间轴；不映射就会每删一次、偏差再累积一次。

`TimeMap` 提供 `to_new`（原文→成片）、`to_old`（成片→原文）双向，
以及 `to_new_clamped`（被删瞬间吸附到最近保留点，用于字幕 cue 起点落在删除区的情况）
和 `map_range`（整段映射，全被删则返回 None）。

### 渲染（`media/render.py`）

- 每个片段**必须**加 5ms 淡入淡出。不加就是 PCM 阶跃，拼接头必然爆音。
  这不是音效处理，是剪辑正确性的一部分。
- 段数多时命令行会超长，filtergraph 写文件传给 `-filter_complex_script`。
- 输出沿用源采样率与声道——这是交付物，不是 ASR 输入，降采样等于丢用户已有的质量。
- 渲染结果按 `edl_signature` 缓存，同一份 EDL 不重复编码两小时的音频。
- **渲染必须异步**（`services/renders.py`）。实测：105 分钟素材、400 段，
  编码耗时约 12 分钟。同步实现有两个必然后果，而且都真实发生过：
  按钮十二分钟没有任何反馈（用户报的就是「点击后无反应」）；
  整个编码期间持有一个 SQLite 写事务，其他任何写操作都 `database is locked`。
  现在是「短事务记录任务 → 工作线程跑 ffmpeg → 短事务写进度」，
  `POST /render` 0.35s 返回，`GET /api/renders/{id}` 轮询 `progress`。
  进度取自 ffmpeg 的 `-progress` 流（`out_time_ms` 实际是微秒）。它是**编码吞吐**，
  不能按成片时长反推剩余时间：本机编码约为实时的 8 倍。

### 实测结果（真实音频，17.6s）

删除第 2 句（3709–6080ms）后：

| 项 | 值 |
|---|---|
| EDL 预期成片 | 15306ms |
| 实际文件时长 | **15306ms（误差 0ms）** |
| 段落数 | 2 |
| 静音吸附 | 末尾 6080ms → 5971ms（吸附到静音起点） |
| 时间轴映射 | 被删句 → `None`；后续句全部精确前移 2302ms |
| 恢复删除后 | 精确回到 17608ms，1 段 |

### M4 修掉的真实缺陷

**`to_new` 把删除区间的第一个瞬间映射回保留段末尾。**
keep 是半开区间 `[orig_start, orig_end)`，当 `ms == keep.orig_end` 时原实现
返回 `keep.new_end`，于是「已被删除的瞬间」得到一个有效的新时间。
后果是字幕 cue 本该消失，却提前一帧重新出现——静默、且随着删除变多而累积。

修正为：边界瞬间若属于下一个 keep，就由下一个 keep 处理；否则返回 `None`。

（另有一个低级错误：渲染命令漏了输出路径，ffmpeg 写到 stdout 后「成功」返回，
磁盘上什么都没有。已修，并补了时长断言测试。）

### 测试

后端 337 个（新增 49 条：区间代数 20、时间映射 12、过滤图与真实渲染 17），
前端 20 条。渲染时长断言容差 50ms——AAC 有约 20ms 的 encoder delay，
超过这个量级就意味着字幕会肉眼可见地漂移。

### M4 补充：实时预览与整句选区（用户反馈后）

**1. 实时预览删除效果 —— 不做渲染，做「跳过式播放」**

用户的原话是每次都要渲染成片才能判断删除是否合理。这个问题必须解决，
因为渲染一小时节目要几分钟，尝试性剪辑根本没法用。

比较了三种方案：

| 方案 | 每次改动的延迟 | 问题 |
|---|---|---|
| 每次改动渲染预览 | 5–60s | 探索性剪辑不可用 |
| 预渲染整条时间轴 | 几分钟 | 每次编辑都失效 |
| **跳过式播放** | **0ms** | 最多漏出 1 个轮询间隔的音频 |

采用第三种：播放时若播放头落入删除区间，直接 seek 到区间末尾。

- 用 50ms 轮询而非 `timeupdate`：后者在部分浏览器被节流到 250ms，
  会漏出至多四分之一秒的被删内容。
- **相邻删除区间必须合并**，否则一串删除会触发多次 seek，听起来像卡顿。
- seek 落入删除区时自动前移到最近的存活位置，否则用户点波形会听到空白。

代价是播放头位置在跳切处不连续，UI 上以波形红色区域标示，用户能预期。

**2. 整句选区**

两种选区并存，且互相排斥：

- **词级选区**（token 区间）：重划边界、改错字、删词。
- **整句选区**（`Set<ord>`）：整句并入上下句、多句批量删除。

分开存而不是互相推导，是因为「选 5 句一起删」应该是点第一下和最后一下，
而不是拖过 300 个字。点字会清空句选区、点句会清空字选区，操作栏永远只有
一个明确目标。

按钮启用条件按选区类型推导（此前几个按钮用同一个 `!selection` 判断，
导致整句选中时按钮全灰）：

| 按钮 | 需要什么 | 为什么 |
|---|---|---|
| 另起一句 | 仅词级、且在一句内 | 整句已是独立一句，再切无意义 |
| 并入上句/下句 | 词级在句内，或任意整句 | 整句时退化为合并 |
| 合并选区 | 任意 | — |
| 删除选中 | 任意 | 多句会合并成一个删除区间 |

**3. 一个排查记录**

多句删除测试时 EDL 里出现了两个删除区间，一度怀疑是吸附或 guard 造成的。
查库确认是历史遗留的有效删除记录，`removed=1` 的软删除记录被正确排除。
结论：数据无问题，`soft delete` 生效正常。这条记录下来是因为
「两个区间」看起来很像 bug，实际是测试数据未清理。

## 工作台界面与导航修复（2026-11-05）

### 界面重构为三区工作台

```
┌────────────────────────────────────────────────────────┐
│ Animatter  项目  设置  导出         项目名 · v1        │  菜单栏
├──────────────┬─────────────────────────────────────────┤
│ 热词         │ 时间码 · 句/字数 · 成片时长             │
│ 本期/全局    ├─────────────────────────────────────────┤
├──────────────┤ 波形 + 字幕叠加（点击字幕跳该句）        │
│              │                                         │
│ 字幕校对     ├─────────────────────────────────────────┤
│ （句列表）   │ ▶ 播放 ⟨上一句 下一句⟩ 删除 恢复 导出     │
│              ├─────────────────────────────────────────┤
│              │ 另起一句 并入上句 并入下句 合并选区      │
├──────────────┤                                         │
│ 操作记录     │ 提示 / 成片试听                          │
└──────────────┴─────────────────────────────────────────┘
```

布局原则：
- 左栏固定 340px。字幕是密集内容，需要稳定列宽；波形每多一像素都有用。
- 只有字幕列滚动，热词与操作记录固定。两者都是「随时瞟一眼」的信息，
  滚动出去就等于没有。
- 波形上部 22px 是字幕带，文字不遮住它描述的波形。字幕按自身时间区间裁剪，
  长句不会溢出到邻句。
- 字幕可点击 → 播放该句。

### 设置

单行 JSON 存 `setting` 表，而不是一列一个配置项：可调项会持续增加，
blob 让新增一项不需要迁移。

校验必须**大声失败**。未知键直接报错而不是存下来——静默忽略一个设置项，
在用户看来和「这个功能坏了」没有区别。
实测：未知键 400、越界值 400、枚举值非法 400。

菜单按「何时生效」分组，因为这才是用户需要知道的：
模型类改动要重新转写，剪辑参数影响下次渲染，导出选项只在导出时生效。
混在一起会让人以为换个 ASR 模型就能修正已有转录。

### 操作记录

可撤销性按 op 类型区分，且**在 UI 里说明原因**：灰掉一行不解释，看起来就是 bug。

| 操作类型 | 可撤销 | 原因 |
|---|---|---|
| boundary（重划边界） | ✅ | 存了完整句子快照 |
| text（改文字） | ❌ | 会改变 token 数，需反向替换 |
| boundary_time（拖边界） | ❌ | 同上，且后续编辑已基于新时间 |

**只能撤销最近一次边界操作。** 更早的操作之后若还有别的编辑，
回滚它会用一份过期快照覆盖掉那些改动。这条限制在服务层强制，
实测越序撤销会报「只能撤销最近一次边界操作」。

### 修复两个导航 bug

用户报告的两个现象，根因是同一个：**导航不知道句子已被删除**。

**bug 1：点击静默区后按空格没声音。**
播放头落在句间空白（或删除区）时，`sentenceAtOrBefore` 返回的是前一句，
而前一句已被删除 → 起点在删除区内 → `playRange` 的停止点在起点之前
→ 立即暂停，表现为「按了没反应」。

**bug 2：前一句已删除时，点上一句或按 `[` 没反应。**
`playPrevious` 取 `sentences[current - 1]`，那正是被删的句子；
`seek` 到删除区内是空操作（`nextAudible` 会把它推回到同一位置）。

修法是把导航从「列表索引」改为「存活音频中的位置」（`navigation.ts`）：
- `previousSentence` / `nextSentence` 跳过整句被删的句子。
- `audibleStart` 对整句被删返回 `null`，部分被删则前移到存活部分。
- `audibleStop` 保证停止点严格在起点之后，否则返回 `null` 表示「播到自然结尾」。
- 全部更早的句子都删完时，`[` 回到第一处存活音频，而不是原地不动。

新增 12 条测试覆盖这些情况，包括相邻删除区间、部分删除、越界回退。

## 工作台与 M5a 字幕导出（2026-11-05）

### 修掉的界面缺陷：菜单和面板全透明

根因不是配色，是 **`--surface` 这个 CSS 变量从未定义**：

```css
/* styles.css 用了 3 次，定义处 0 次 */
background: var(--surface);
```

未定义的自定义属性会让整条声明在 computed-value 阶段**失效**，于是背景变成
`transparent`，下拉菜单和模态框直接透出背后的工作区文字。

修法：定义 `--surface`（浅色 `#ffffff` / 深色 `#1a2334`），并给所有引用加
兜底 `var(--surface, var(--panel))`——将来再写错一个变量名，退化成面板色而不是
"透明"这种最难排查的表现。

顺带补了 `--accent-dark`（同样被引用但未定义，chip 文字因此用了无效色）。
已加变量审计：任何 `var(--x)` 若无定义都会立刻暴露。

### 项目存档（`.project`）

设计目标不是"导出"，而是**可继续剪辑的存档**。因此归档必须携带：
文稿、每个句子边界、每处文字修正、每处删除、**完整操作记录**、热词、大纲、校对候选。

**刻意不携带音频**：两小时 WAV 是 GB 级，存档要能塞进邮箱附件。改为记录
`sha256`，载入时按哈希重新关联。三种情况：

| 情况 | 行为 |
|---|---|
| 文件在且哈希一致 | 正常关联 |
| 文件在但哈希不符 | 仍关联，并警告时间轴可能与音频不匹配 |
| 文件不在 | 用占位 media 行恢复文稿与全部编辑，提示重新关联音频 |

第三种是关键取舍：**音频丢了不能连带丢掉用户的剪辑工作**。

格式是 zip 包里的 `project.json`：任何工具都能读，transcript 压缩率高。
载入**永远新建项目**而不是覆盖——覆盖用户正在编辑的项目是不可恢复的。

### 修掉存档路径上的 4 个真实缺陷

写这个功能时踩到的，全部是"只有真正跑一遍才会发现"的类型：

1. **`sentence.updated_at` NOT NULL 未赋值** → 载入直接 500。
2. **`token.id` 是 INTEGER AUTOINCREMENT**，却传了 `tok_xxx` 字符串 →
   `sqlite3.IntegrityError: datatype mismatch`。
3. **`media.sha256` 全局唯一**，存档里带着原哈希再插一行必然冲突 →
   改为：已有同哈希行则复用；文件缺失时用 `missing:<random>` 合成哈希。
4. **`--surface` 未定义**（见上）。

教训：模型层每个 `NOT NULL` 列都应该在恢复路径上有明确来源。已按表核对一遍
（token / sentence / edit_op / deletion / episode_hotword / outline /
correction_candidate / transcript_version）。

### 吸附的单调性（顺带修掉的一个语义 bug）

`sanitize` 的静音吸附原来会挑"最近的静音边界"，**不看方向**，于是能把切点
往里挪：

```
用户删到 6010，附近静音 5991–6100  →  实际删到 5991（少删了 19ms）
```

这等于把用户明确要删的音频还回来，而且恰恰留下了吸附本要消除的半截音素。
现在强制单调：**start 只能左移、end 只能右移**，吸附永远只会扩大删除范围。
补了两条回归测试。

### M5a 字幕导出

- SRT / VTT / TXT 三种格式。TXT 就是"自动整理的文稿"初版（一行一句，无时间码）。
- **所有时间戳都过 `TimeMap`**。实测：删掉第 2 句后，第 3 句字幕从
  `00:00:06,440` 变成 `00:00:04,138`，与成片音频对齐；未映射版本仍是 6.44s。
- 没有删除时返回 `None` 而不是恒等映射——恒等映射也要经过一次浮点运算，
  没必要时不该引入舍入机会。
- 长句按标点优先拆分成多条字幕（`max_chars × max_lines` 预算），拆完按字符数
  比例分配原有时间，不改变句子总时长。
- 质量检查：`too_fast`（超语速）、`too_brief`（<0.4s）、`split`、`partially_deleted`，
  在导出前就显示给用户，而不是等播放时才发现。

### 验证

- 后端 377 测试（新增 22 条：存档往返、字幕映射、吸附单调性）。
- 存档往返实测：5 句 / 61 token / 17 条操作 / 音频重连，**逐字段一致**。
- 字幕映射实测：删除后时间轴压缩 2302ms，字幕同步前移。
- `mypy --strict` 零错误，ruff 干净，前端 tsc + vite build 通过。

### 修掉 `media.sha256` 全局唯一导致项目「没有音频」（2026-11-06）

用户报「这个项目没有登记音频文件」。排查发现是**约束与归属模型自相矛盾**：

```sql
sha256 VARCHAR(64) NOT NULL UNIQUE   -- 全局唯一
project_id VARCHAR(64) NOT NULL      -- 但按项目归属
```

执行链：

1. `register_media` 把音频**复制**到 `data/projects/<project_id>/media/`，
   说明媒体行是项目私有的。
2. 但查找复用时按 `sha256` 全局匹配 → 载入存档时命中了**另一个项目**的行，
   直接 `return`。
3. 结果：新项目的 `version.media_id` 指向别人的 media 行，自己一条都没有。
4. `/api/projects/{id}/media` 按 `project_id` 过滤 → 返回 `[]`。
5. 前端看到空列表 → 「这个项目没有登记音频文件」。

**修复**：
- 约束改为 `UNIQUE (project_id, sha256)`，`sha256` 单独建索引。
- `register_media` 与存档恢复的复用查询都加上 `project_id` 过滤。
- 迁移用原始 SQL 重建表（SQLite 不支持 `DROP CONSTRAINT`，且原约束是
  **未命名内联**的，Alembic 无法按名删除）。

**为什么这不只是数据脏**：只要用户把同一段音频用在两期节目（试听样本、
片头音乐、同一期重剪），旧约束就必然让第二个项目失去自己的媒体行。
这是设计层面的矛盾，不是偶发。

**验证**：存档往返后新项目 `media=1`、`duration=17608ms`、5 句一致；
前端启动序列经 Vite 代理全部 200，音频 Range 返回 `206 audio/wav`。

## 词级编辑与说话人（2026-11-06）

### 决策：从字级退回词级

需求原话：「咱们彻底不要求字级了，就只管词级。字幕区点击文字换成是按一个一个词来框选。」

**推翻的旧设计**：`_split_units` 把每个 ASR 词按字符切开，词的时长按字符数比例插值，
每个字得到一段**观测不出来的时间**，标 `derived=1`。

**为什么必须推翻**（两层，第二层才是决定性的）：

1. 字级时间不存在。字级粒度误差 = 词时长 / 字数，中文典型 60–200ms/字。
2. 因此"改字"变成一次**重新插值**：新旧字数不同就必然重排窗口内部的时间点。
   实测 `14800–14960 / 14960–15200` 被改成 `14800–15000 / 15000–15200`——
   用户改了一个错别字，他正在听的剪切点挪了 40ms。这类 bug 无法靠加断言根治，
   因为**边界本来就该动**：字数变了，比例分配就得变。

词级把这类 bug 从"需要小心"变成"结构上不存在"：一个 token 就是一个观测区间，
替换永远 1 token → 1 token，两条边都是真的，内部没有可移动的点。

**代价**：选择粒度变粗，改不了半个词；想剪掉词中间只能波形框选。

**连带结论（用户提问的直接回答）**：「改文字是否就不用严格要求字数一致了？」
——**是的**。词级下替换文本可以任意长（"人工智能" → "AI" 合法）。唯一保留的是
一个**可读性提示**：新文本按原词时长算出来的语速超过 ~14 字/秒时告诉用户
"要塞进 90ms，语速会明显快于原音"，但不阻止。

### 决策：修正与删除都用贴着选区的气泡

参照 rescript：选中文字 → 浮出小气泡 → **✂ 删除** / **✎ 修正**。

- 删掉了旧的「改文字」输入框。表单是第二个状态（打开 / 取消 / 目标），
  和选区状态容易不同步；旧实现里那个按钮曾把 `undefined` 当提案发出去。
- 气泡**贴在词的下方**而不是上方：词的上一行是句头（时间、说话人、标签），
  气泡盖住它等于盖住用户正在判断的上下文。只有下方放不下时才翻到上方。
- 气泡位置按**中心**做视口钳制（元素是 `translateX(-50%)`）。
  早期按左边缘钳制，靠近右边距时气泡有一半在屏幕外。

### 决策：状态用颜色表达，并且落库

| 状态 | 表现 | 存储 |
|---|---|---|
| 已删除 | 红字 + 删除线 + 淡红底 | `deletion` 行 |
| 部分删除 | 红字 + 点线 + 半格底色 | `deletion` 行（只盖住词的一部分） |
| 已修正 | 蓝字 + 淡蓝底 + 下方蓝线 + ✎ | `token.revised = 1` |

`revised` 放在 token 行上，而不是从 `edit_op` 反推：操作日志按 `idx` 记录，
而文本编辑会重排 `idx`；而且"这个词被人改过"在撤销之后**依然是事实**。

**部分删除必须单独一档**：边界拖动会啃掉词的一边。整词画删除线是在声称
"这段音频没了"，而它其实还有一半能听见——这是会让人不信任工具的谎报。

### 决策：说话人改名只碰一行

`sentence.speaker_id` 存**项目内局部整数**，不是 `speaker.id`。于是：

- 改名 = 改 `speaker` 表一行，所有句子立刻跟着变；
- 模型不做分人时也能用：手动 `POST /speakers` 建一个，再把句子指过去；
- 删除说话人**不清空句子的标注**——那是用户没要求的改写。视图会给孤立的
  `local_id` 合成默认名字，所以 UI 上"删掉"表现为名字恢复成「说话人N」。

实测 `qwen-audio-3.1-asr-flash-filetrans` 对 17.6s 单人样本返回 **0 个 speaker**、
所有 `speaker_id` 为 NULL，所以手动路径不是兜底，是这类音频的唯一路径。

### 顺带修掉的三个真 bug

1. **词级合并迁移可重复执行**：`3c1a7d90e5b2` 的"孤儿检测"用
   `covered = sum(end-start) < len(tokens)`，这个条件**恒为真**（合并必然减少 token 数），
   于是第二次执行把整份稿子又插了一遍：31 → 62 个 token，句子 4 覆盖 `[24,62)`。
   改为按 `src_word_idx` 连续段消费的 `consumed: set[int]`，并加幂等前置检查。
2. **`sentence_count` 从不更新**：转写时写一次，之后任何另起/合并都不改。
   顶栏显示「5 句 · 31 词」里的"5"曾经是"3"。现在 `_persist_spans` 同步写回，
   并加迁移 `9d21c4a7f0be` 重算存量行。
3. **切点吸附会往内挪**：删到 6010ms、静音谷 5991–6100，结果只删到 5991ms，
   19ms 用户删掉的音频回来了。改为**只向外吸附**（起点只左移、终点只右移）。

### 验证

- 后端 411 用例（含 9 个迁移测试、19 个说话人测试）。
- `node --test` 57 个前端逻辑用例（`transcript.ts` 直接 import，Node 24 原生剥离 TS）。
- 真机 CDP 脚本 `scripts/check-edit-loop.mjs`：点词 → 点删除 → 读回
  `rgb(185,28,28)` + `line-through` → 点修正 → 读回 `rgb(29,78,216)` + 600 字重。
- 视觉走查用 `apps/web/harness.html?scene=bubble|correct|speaker|rename|drag|multi`，
  它用固定数据挂载 `SentenceList`，因此每个状态都能单独截图——真实工作台里
  截不到"气泡打开着"这种瞬间。

## 热词真正生效、波形缩放与中文路径（2026-11-06）

这一轮修的是五条用户报的缺陷。前两条看起来是"界面没做"，其实是**数据没到**；
第三条是根因性的；只有第五条纹波是纯交互工作。

### 缺陷 1：中文文件名识别不了

现象是"用中文路径上传失败，改成英文就好了"。听上去像编码校验或文件选择器的问题，
实际是 `subprocess.run(..., text=True)`。

`text=True` 等价于用 **locale 编码**解码子进程输出。这台机器是中文 Windows，
`locale.getpreferredencoding(False)` 是 **cp936**，而 `sys.getfilesystemencoding()`
是 utf-8——两个编码不一致，问题就藏在这个缝里：

1. ffprobe 的 JSON 里会把它读到的**文件名原样回显**；
2. 文件名含中文 → UTF-8 字节 → 用 cp936 解码 → 解码线程抛 `UnicodeDecodeError`；
3. `subprocess.run` 把这个异常**吞掉**，`returncode` 仍是 0，但 `stdout` 变成 `None`；
4. `probe()` 拿到 `None` 去 `json.loads("{}")` → `streams == []` →
   报"文件里没有音轨"。

也就是说：文件是好的，ffprobe 也是成功的，报错信息完全指向错误的方向。
真机脚本 `probe_text_mode.py` 的对照结果：`text=True -> rc 0, stdout is None: True`，
`encoding="utf-8" -> rc 0, streams: 1`。

修法：`media/ffmpeg.py`、`media/render.py`、`media/probe.py` 一律
`capture_output=True, encoding="utf-8", errors="replace", check=False`。
`errors="replace"` 是刻意的：文件名被替换成 `?` 不该成为拒绝这段音频的理由。
`media/peaks.py` 本来就停留在字节模式（`s16le` PCM），不受影响。

回归测试 `tests/test_media_paths.py` 覆盖中文文件名、中文目录名、中文目标路径，
外加一条 **AST 级**的"谁都不许再用 `text=True`"检查——不是字符串匹配，因为那三个
模块的注释里正好写着 `text=True` 这个反面教材，纯 grep 会误伤自己的文档。

### 缺陷 2 & 3：热词从来没被发出去

用户的原话："加了全局热词「漫物质」「马小褂」，依然识别成「漫无日」「马小挂」，
需要排除一下，增加的热词是否在生效？"——**没有生效，一次都没有。**

链路追下来是这样断的：

```
NewProjectDialog  → POST /transcribe { use_hotwords: true }
TranscribeIn      → 没有 use_hotwords 字段 → pydantic 静默丢弃
TranscribeOptions → vocabulary = None
AsrRequest        → vocabulary = {}
_build_body       → parameters 里没有 vocabulary
```

`existing_vocabulary()` 早就在 `services/hotwords.py` 里写好，`routers/hotwords.py`
和 `routers/proofread.py` 都在用，只有**转写这条路没调**。
数据库里 `transcribe_job.params_json` 记的是 `"vocabulary": {}`，是自证。

两处根因，都得修：

1. `TranscribeIn` 加 `use_hotwords: bool = False`；路由里在
   `vocabulary is None and use_hotwords` 时调用
   `hotword_service.effective_vocabulary(...)`，并且**在路由层合并**而不是在
   service 层，因为 job 快照要记录"这次实际发了什么"。
2. 加了 `VersionOut.hotword_count`，把实际条数返回给前端。0 是一个有意义的答案：
   它把"热词关了"和"热词开了但表是空的"区分开，UI 才能说清楚。

验证用 `scripts/verify_hotwords.py`（真后端 + 真百炼 + 真 SQLite）：

```
episode hotwords: 马小褂
vocabulary preview: {"漫物质": 3, "马小褂": 3}
version: hotword_count = 2
params.vocabulary: {"漫物质": 3, "马小褂": 3}
转写文本：「各位听众大家好，欢迎收听本期的漫物质，我是今天的主持马小褂。」
```

同一段音频、同一句话，在热词生效前是"漫无日 / 马小挂"。

### 缺陷 4：热词编辑搬到该在的地方

原来的流程是"先转写完，进了工作台，才有热词面板"——热词是识别的**输入**，
等识别完了再提供编辑入口，等于把唯一有用的时机错过了。勾选框指向一个用户
还没法填的列表，是这个缺陷最直白的表现。

- 抽出 `HotwordEditor`（`apps/web/src/HotwordEditor.tsx`），
  一处实现、三处复用：工作台侧栏、新建项目对话框、设置里。
  三份拷贝迟早会漂移，何况其中一份还要处理"还没有 media_id"的中间态。
- 新建项目对话框：勾上"转写时启用热词"就在下方展开编辑器。
  **本期热词先存本地草稿**——它只能挂到 media_id 上，而 media 要等项目创建后才有；
  提前建项目会在用户取消时留下孤儿项目。所以在 `create()` 里、
  **转写之前**才 `setEpisodeHotwords(...)` 落库，然后才发识别请求。
- 设置菜单拆成两项：「模型与参数…」和「热词与校对…」，打开同一个对话框、
  滚到不同 section（`SettingsSection`）。"热词与校对"里放全局热词、本期热词、
  `use_hotwords`、`use_proofread`、`hotword_weight`、`proofread_chunk_size`。
- 顺带发现一个 API 设计缺陷：`POST /media/{id}/hotwords` 是**整体替换**语义
  （大纲提取需要重建整张表），前端"添加一个词"也复用它，就会把已有的词全删掉。
  界面上表现为"加了第二个嘉宾名，第一个没了"。修法是加两个**窄操作**端点：
  `POST .../hotwords/append` 和 `DELETE .../hotwords/{text}`，
  `addEpisodeHotword` 改打 append。`tests/test_hotwords_episode.py` 用 9 条用例
  把"追加"和"替换"两种语义钉开，其中一条专门测"替换端点仍然是替换"，
  免得后人把它也"修"成追加。

### 缺陷 5：波形滚动与滚轮缩放

波形一直是"整段压到容器宽度"，一小时节目等于一个像素 4 秒，什么都看不出来。

关键决策：**缩放是窗口，不是更宽的画布**。3 小时、64 buckets/s 是 69 万个桶，
8 倍缩放就是 550 万个，浏览器会先被 JSON 噎死，一个像素都画不出来。所以画布宽度
保持容器宽，只画 `[from_ms, to_ms)`——这正是后端 `GET /waveform?from_ms=&to_ms=
&buckets_per_second=` 早就支持的形状，**后端一行没改**。

- 滚轮缩放锚定**指针位置**（`zoomAt`），不是窗口中心。锚在中心的话，
  每滚一格光标下的词就往边上滑，用户得用滚动条追它。
- 滚动条是**自绘滑块**而不是原生 overflow：没有更宽的元素可以滚。
  滑块可拖、轨道可点击翻页、双击回到全览。
- 请求按窗口宽度重算 `buckets_per_second`（`bucketsForWindow`），并做 90ms 防抖；
  不重算的话，放大后波形会变成台阶。滚到底时放行 `deltaY`，页面还能正常滚动。
- 缩放算术抽成 `apps/web/src/waveView.ts` 纯函数，19 条 `node --test` 用例覆盖：
  边界钳制、锚点不动、放大再缩小回到原窗口、"零倍缩放"不除零、
  文件比最小窗口还短、滑块不会跑出轨道。
- 播放头跟着窗口走（只缩小到 1 倍时不跟随），否则放大后红线会走出屏幕。

### 验证

- 后端 **432 用例**（新增 `test_transcribe_hotwords.py` 5 条、
  `test_media_paths.py` 7 条、`test_hotwords_episode.py` 9 条），
  `mypy --strict` 59 文件干净，`ruff` 干净。
- 前端 `node --test` **77 条**（新增 `waveView.test.mjs` 19 条），`tsc --noEmit` 干净，
  `vite build` 通过。
- 真机 CDP 三个新脚本：
  - `check-waveform-zoom.mjs`：滚轮 4 格 → 滑块 `40.97%`、读数 `17.6s → 7.2s`
    （75s 音频时 `1.3 分钟 → 30.8 秒`）→ 拖滑块 → `aria-valuenow` 4402
    → 点「全部」→ 滑块回到 100% 且 `idle`；再开设置并断言出现
    `["模型","热词与校对","剪辑参数","导出"]` 且编辑器里有 `漫物质/马小褂`。
  - `check-new-project-hotwords.mjs`：取消勾选 → 编辑器消失；重新勾选 → 出现；
    输入「马小褂」→ 本期 1 个、提示从"2 个热词"变"3 个热词"；
    切到全局页 → 看到全局的两个词。
  - `check-hotword-panel.mjs`：加一个临时词 → 原有的「马小褂」还在 →
    删掉临时词 → 恢复原状（这条专门盯住上面那个"追加变成替换"的缺陷）。
- 数据库里留了一个可复现的项目：「热词验证 · 马小褂/漫物质」，
  75s 真音频、167 词、10 句，热词生效后「漫物质」「马小褂」都正确。

## 一键清理（2026-11-06）

参考 Rescript 的 [One-click cleanup](https://www.getrescript.com/remove-filler-words)，
按它的四条真实教训落地，但把「一点就走」改成「先给清单再执行」。

### 为什么拆成三类，而不是一个大按钮

Rescript 的清理实际是两条独立管线（填充词 + 静音），他们漏了第三条，而第三条
对中文播客是最关键的：**识别会把整个「嗯」吃掉**。Whisper 这样，百炼也这样。
音频里是一段语音，稿子里一个字都没有——纯文本匹配永远找不到它。

所以是本模块的三类：

| 类别 | 依据 | 为什么必须独立 |
|---|---|---|
| 口癖词 `filler` | token 文本对词表 | 用户可能只想删这个，不动静音 |
| 未转写语流 `disfluency` | **语音 − token 覆盖** | 稿子里根本没有这个字，只能从音频摸 |
| 静音 `silence` | 检测器的静音区间 | 有独立的阈值和 padding，语义不同 |

关键实现：**语音 = 静音清单的补集**。不需要引入 VAD，因为我们已经有
`ffmpeg silencedetect`；它的补集就是语音。再减去 token 覆盖，剩下的就是
「有声但没字」的片段。零新依赖。

### 四条从 Rescript 源码学到的规则

1. **不跨词合并切割**（他们 PR #115 "Keep short words between removed silences"
   修的就是这个）。两个 0.4s 静音之间夹一个 60ms 的「对」，朴素合并会把它一起切掉。
   我们更危险：`core/edl.py` 的 `_drop_slivers` 会丢掉 <80ms 的保留岛，
   于是那个「对」从渲染里消失、却还在稿子上显示——用户看不见自己丢了什么。
   规则写在 `_merge_silence_cuts` 里，`test_short_word_between_two_silences_survives`
   专门盯住它。
2. **默认词表刻意保守**。Rescript 明确不收 like / so / right；我们同理不收
   「就是说」「然后呢」「那个」——它们大多数时候是正常用词。`FORBIDDEN_DEFAULTS`
   是**测试里**的断言，不是注释里的劝告。
3. **两侧留 pad**，防止削掉音素；但**媒体首尾切齐**，否则文件开头结尾会留一段空白。
4. **相邻口癖合并成一次切割**。三个「嗯」是 1 处不是 3 处——用户数的是切割次数。

### 决策：先给清单，再执行

Rescript 是一点就走，我们**计划与执行分离**：

```
GET  /versions/{id}/cleanup/plan          → 只读，返回三类清单 + 计数 + 时长
POST /versions/{id}/cleanup {kinds:[...]} → 才真的写 deletion
POST /versions/{id}/cleanup/restore?source=filler|batch=cln_xxx
GET  /versions/{id}/cleanup/counts        → 菜单里「还原口癖词（12）」的数字
```

对话框里每个类别都是可勾选的，显示「3 处 · 840 毫秒」；下面列出**具体切在哪**
（时间、类别、文本、第几句），并可以调阈值重算。`执行清理` 按钮上就写着会删几处。

### 决策：一次清理 = 一条撤销记录

400 条 deletion 不能变成 400 条历史。`deletion.batch_id`（迁移 `4e18c2a9f7b1`）
+ `edit_op.op_type="cleanup"` 的 payload 里存整批 id：

- `undo_op` 现在认两种：`boundary`（恢复快照）和 `cleanup`（把这一批翻回去）；
- 历史面板显示一行「一键清理 / 口癖词 3 · 未转写语流 1」，`undoable: true`；
- 另有按类别的还原入口（`restore?source=`），因为连续清理三次之后，
  用户想要的是「把口癖都还回来」，不是按时间顺序撤销三次。

### 决策：改设置不追溯已应用的切割

这条写进了测试（`test_changing_the_threshold_does_not_alter_applied_cuts`、
`test_removing_a_word_from_the_filler_list_does_not_restore_it`）。
理由很直接：如果阈值联动，用户把 0.3s 改成 0.5s、点保存，就等于**在没有提示的情况下
又删了一轮音频**。设置只喂下一次计划，已应用的切割是历史事实。

### 静音清单缓存（`media/silences.py`）

清理需要**全片**静音清单，而 `build_edl` 每次编辑都会重算——3 小时节目会卡死。
所以照抄 `media/peaks.py` 的形状：`{media_sha}.v1-{noise}db-{min}s.json`，
key 里带**检测器参数**（改阈值必须换 key，否则用户调了滑块却看不到变化）。
真实数据：75 秒音频首次检测 0.22s，命中缓存后 0.012s。

顺带把**检测器参数**和**切割阈值**分开了：
`silence_noise_db` / `silence_min_duration_ms` 决定「哪里算静音」，
`cleanup_min_silence_ms` 决定「多长才值得切」。两者混用会让「吸附切点」
跟着清理阈值一起变，是个隐蔽的耦合。

### 修掉的两个真 bug

1. **设置对话框每渲染一次就重新拉一次 `/api/settings`**。`refreshHotwords` 是
   `useCallback`，但它的依赖链走到一个父组件内联构造的 prop，identity 每次都变；
   把它放进 effect 依赖里，effect 就跟着重跑。后果极其阴险：
   **用户输入几百毫秒后，输入框被服务端的旧值覆盖**——看起来像「设置保存不上」。
   修法是把最新回调放进 `useRef`，effect 只依赖 `open`。这个 bug 在加口癖词表
   （一个长 textarea）之前一直没被发现，因为短输入通常来不及被冲掉。
2. **迁移不是幂等的**：`4e18c2a9f7b1` 第一次写没有 `PRAGMA table_info` 前置检查，
   `test_running_the_migration_twice_does_not_duplicate_tokens` 立刻炸在
   `duplicate column name: batch_id`。加 `_has_column` 守卫后通过——这条测试
   模拟的正是「部署失败后重放」，是真实场景。

### 验证

- 后端 **528 用例**（新增：`test_fillers.py` 25、`test_cleanup_core.py` 32、
  `test_cleanup_service.py` 27、`test_cleanup_settings.py` 12、迁移幂等 1），
  `mypy --strict` 64 文件、`ruff` 干净。
- 前端 77 用例、`tsc --noEmit`、`vite build` 干净。
- 真机 CDP：`check-cleanup-dialog.mjs`（三类计数与「本次会删 14 处」一致、
  全部取消后按钮禁用）、`check-cleanup-settings.mjs`（61 行词表、
  改阈值 → 保存 → 从服务端读回 450 → 复原）。
- **真实音频端到端**：75 秒样本，计划 14 处（口癖 3 · 未转写 1 · 静音 10），
  执行后 `removed_ms=4916`，`render?preview=true` 5.7s 出片 67.95 秒，
  一次 undo 回到 `removed=0 / segments=1`。

## 保存即下载、大纲导入热词、缩放不再被编辑重置（2026-11-06）

用户报的三条，都不是「坏掉了」，而是「做完之后不知道自己做了什么」。

### 1. 点保存项目没有下载，也不知道文件在哪

两个独立缺陷叠在一起，缺一个都解释不通用户的观察：

- 存档写在 `data/projects/<id>/<项目名>.project`，`_to_out` 回的是**相对路径**。
  这个路径只对「站在仓库根目录的人」有意义，「载入项目」从任何别的地方粘进去都失败。
- `run()` 在动作**返回之后**写 `「label」完成`，把动作自己刚写的「已保存到 …」
  覆盖掉了。所以提示是真的出现过，随即被抹掉——用户看到的是「完成」两个字。

修法分三层：

1. 后端 `_to_out` 一律 `path.resolve()`。
2. 前端 `run()` 记一个 `noticeClaimed`，动作自己 `say()` 过的就不再覆盖。
3. **真的要下载**：`GET /archive/file` 已经在，但它只回文件；而
   `POST /archive` 只回元数据。用户要么下载、要么知道路径，二选一。
   现在下载响应的 header 里带上服务端绝对路径（`X-Archive-Path` 等），
   前端 `fetch` 一次拿到 blob + 路径，再把 blob 交给浏览器。

> header 必须百分号编码：HTTP header 是 latin-1，项目名「第 07 期」直接塞进去
> 会在响应阶段抛异常。测试 `test_download_headers_survive_a_chinese_project_name` 盯这个。

「载入项目」对话框现在预填上一次保存的路径——保存完想立刻载回来验证，不用复制粘贴。

### 2. 新建项目时不能从大纲导入热词

大纲抽词端点原来挂在 `/projects/{pid}/media/{mid}/hotwords/suggest` 上。
可是**大纲导入唯一有意义的时刻，是转写之前**，那时项目还不存在——端点把功能
挡在了它唯一有用的地方之外。

补了无状态版 `POST /api/hotwords/suggest`（不需要 project、不需要 media、不落库），
前端 `OutlineImport` 组件：

- 粘贴大纲 → 「提取候选」→ 得到带权重的词条；
- **候选默认全选，但可以逐个取消**——LLM 会给出几个看起来很像的错词，
  而每个错词都会让识别偏向它（§6.1），所以必须先过一遍人工；
- 「加入本期热词（N）」只把勾选的并入草稿；已有词标「已有」并划线，不重复加；
- 权重保留抽取器的排序（标题词 > 一次性提及），不压成默认 3。

草稿在 media 行存在之前只存在于内存，创建时按顺序落库：建项目 → 注册音频 →
写热词 → 写大纲 → 转写。顺序是硬要求：热词必须在转写**之前**进库，否则这一轮
转写根本用不上它们。

### 3. 拖句子边界会重置波形缩放

根因不在 `Waveform`：拖边界提交编辑 → 编辑调 `onReload()` → `load()` 里
`setLoading(true)` → 渲染分支 `if (loading) return "加载中…"` → **整个 `<Desk>` 连同
`Waveform` 被卸载**，组件内 `useState` 的 `view` 自然回到「全片」。

对照 Rescript：它的 zoom 也是组件内 `useState(1)`，从不重置，因为编辑**不会重挂载**
编辑器。所以正确修法是让重载不卸载工作台，而不是往波形里塞持久化。

两处都做了：

- `App` 用 `bootRef` 区分「首次加载」与「编辑后重载」，只有前者清屏；
  渲染门改成 `if (loading && !boot)`。
- `Waveform` 另加一层按 media 的记忆（`VIEW_MEMORY`），覆盖项目切换、HMR 这类
  真的重挂载；全片视图不入表（那是默认值，记住只是噪音）。
  恢复时走 `initialView()` 做 `clampView`——重新导入的音频可能更短，
  直接还原一个 40 分钟开头的窗口会画出空白画布，且没有任何提示。

### 顺带修掉的一个真 bug（工作台的大纲提取）

`generateFromOutline` 调完 `suggest` 就刷新列表，然后报「从大纲提取了 N 个热词」。
但 `suggest` 只**提议**，不写库——真正的写入要调 `suggest/accept`。结果是：
提示说有 N 个词，chip 区还是空的，下一次转写一个热词都没带上。
现在按 `suggest → 人工确认 → accept` 走完。CDP 脚本 `check-workbench-outline.mjs`
断言的就是「chip 数必须变多」，而不是「请求发出去了」。

### 测试与验证

- 后端 **537 用例**（新增 `test_outline_import.py` 9 条：无项目抽词、脏词过滤、
  不落库、空大纲、缺 key 降级；下载是 attachment、header 带可回读的绝对路径、
  中文项目名不炸、计数与存档一致），`ruff`、`mypy --strict` 干净。
- 前端 **82 用例**（`waveView.test.mjs` 新增 5 条：全片视图不记忆、真缩放要记忆、
  无记忆时全片、有记忆时原样恢复、文件变短时 clamp），`tsc --noEmit` 干净。
  顺手把 `npm test` 漏掉的 `waveView.test.mjs` 加进脚本——它之前从来没被跑过。
- 真机 CDP 三个脚本全 PASS：`check-outline-import.mjs`（导入→取消一个→并入草稿，
  且钩住 `HTMLAnchorElement.prototype.click` 证明浏览器真的发起了 `.project` 下载，
  提示里带 `D:\...` 绝对路径）、`check-zoom-and-save.mjs`（4 格滚轮后拖边界，
  thumb 宽度与读数字节不变）、`check-workbench-outline.mjs`（chip 1 → 3）。

## 时间轴对齐（2026-11-09）

用户报告：点「播放这句」时第一个字被吞掉一半，有些句子完全听不到第一个字。

### 诊断：偏差来自百炼，不是我们的链路

逐段验证，每一条都可复现：

| 环节 | 验法 | 结果 |
|---|---|---|
| ffmpeg 转码（原始 → 16k 单声道 mp3） | 两版都解成 PCM 做互相关 | 平移 **0 样本** |
| 入库（ASR JSON → token 表） | 8246 个词逐个比对时间 | **0 处不一致**（6 个空词被丢弃，其余原样） |
| 浏览器播放 | 页面内 `decodeAudioData` 后重测 | 与 Python 一致，−98 ms 两边都对上 |
| MP3 编码延迟（576+529 样本 = 69 ms） | 同一段 60 s，mp3 与 wav 各识别一次 | **差 0 ms**（中位数 +0，最大 ±20），模型做了 gapless 补偿 |

实测口径：两个真实项目、997 个「前面确有 ≥250 ms 停顿」的句首词，声学起声减去模型 `begin_time`：

    中位数 −65 ms   平均 −86 ms   p25 −125   p75 −35
    |偏差| ≤ 40 ms 的句子只占 31 %
    偏晚 > 100 ms 占 27 %

句中词同样偏晚（中位数 −60 ms，n=1691）。所以是**整条时间轴**偏晚，不是只有句首。

**为什么不用逐词校正**：模型时间戳粒度是 10 ms（末位全是 0），单句误差不可从音频稳定复现；能量法在连读里分不清词界。一个常数才是这类系统性偏差的正解。

### 决策：API 收「目标值」而不是「步长」

`POST /api/versions/{id}/time-shift {shift_ms: -80}` 的含义是「让这条时间轴相对模型原始时间偏 −80」，服务端施加的是 `target - current`。后果有两个，都是想要的：

- 同一个请求点两次不会变成 −160，面板上的「撤销」就是「对齐到 0」，用户不需要知道自己点过几次；
- 只有**差值**被施加，所以用户手动拖过的边界保留相对位置。若从 `transcripts/*.raw.json` 全量重算，那些拖动会被静默抹掉（`edit_op` 里有 36 条 `boundary_time`）。

`transcript_version.time_shift_ms` 只是**记录**：token / deletion 行里已经是平移后的时间。没有这一列的话，「当前偏移多少」不可观测——而「再减 80」不是一个值，是对施放过几次的猜测。

### 决策：夹紧差值，不夹紧行

要求 −1000 ms 而首个词在 240 ms 时，服务端把**差值**降到位移后最早时间仍 ≥ 0，并回 `clamped: true` 与原因。逐行夹会让录音开头被压扁、改变内部相对时序；夹差值保证整条轴刚性。同理上界按 `duration_ms`。

### 决策：token 与 deletion 一起移

删除区间挂在词边界上。只移 token 会让每个剪切点滑到相邻音素——音频剪错位置，而且用户听不出来为什么。520 行 deletion 一起平移。

### 面板：立即生效，不用「保存」

其余六个面板都是「下次生效」的偏好，改完按保存。这个面板改的是已经存在的转写，所以按钮按下就写库，底栏直接写「对齐立即生效，与本面板的「保存」无关」，并把「保存」按钮换成「关闭」。一个按钮两种提交语义，是用户按了取消却发现时间轴已经变了的经典成因。

导航里排在最后，摘要写明「直接改写当前版本」（其余六项都是「下次生效」）。

**自动对齐**（逐句用波形能量定位）放面板里但按钮置灰，实际实现记在仓库根 `todolist.md`：它要处理人工拖过的边界、删除区间映射、渲染切点和「撤销回到基线」四件事，不是加一个函数。

### 验证

- 后端 **651 用例**（新增 `test_align.py` 13 条：目标值语义、撤销、deletion 同移、夹紧、人工拖动保留、HTTP 400 带 hint、版本载荷暴露偏移），`ruff` / `mypy --strict` 干净。
- 前端 133 用例、`tsc --noEmit`、`vite build` 干净。
- 真实项目 `ver_1m6v2q5javqm1j82xsg`：应用 −80 后 8240 字 / 5 处删除一起移动（时间轴 5.10–3693.57s → 5.02–3693.49s），撤销后逐毫秒还原。
