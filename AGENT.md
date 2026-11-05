# AGENT.md — 播客转写 / 字幕校正 / 波形剪辑 / 文稿整理

> 本文件是本仓库的**设计基线 + Agent 工作约定**。
> 任何实现、重构、依赖引入都必须先与本文件一致；需要偏离时，先改本文件再改代码，并在"决策记录"里留痕。
> 当前状态：**v0.4 — M0/M1/M2a/M2b 已落地**。转写闭环 + 热词库 + 大纲抽词 + LLM 校对，均用真实 API 验证。下一步 M2c 影子测试，然后 M3 校正台 UI。

---

## 0. 一句话目标

把一段播客音频（1–12 小时、单人或多人、可能分轨）变成三样东西：

1. **可校正的 字级/句级 时间轴**（人工校对成本最低的形态）；
2. **按字级标签删改后的成片音频**（不做音效，只做"剪"）；
3. **一份结构化的文稿 + 配套字幕文件**（时间轴已重映射到成片）。

---

## 1. 范围与非目标

### 1.1 范围内（v1 必须做到）

| # | 能力 | 验收口径 |
|---|---|---|
| R1 | 音频转写走阿里百炼云 API，本地文件经**百炼临时存储**上传取 `oss://` URL | 不落地任何公网可访问文件；失败可重试 |
| R2 | 全局热词 + 当期热词；当期热词可由粘贴的大纲自动生成 | 生成结果可人工增删改权重，最终作用于识别 |
| R3 | 句级标签 + 字级标签；字级标签可多选后重划归属（上句 / 下句 / 另起一句） | 任意划分不丢字、不重字、时间单调 |
| R4 | 通过删除字级标签剪辑音频；也可在波形上框选区间删除 | 输出音频与字幕时间轴严格一致 |
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
| 转写 JSON | `transcripts[].sentences[]`，句含 `begin_time/end_time/text`，句含 `words[]`（词级 `begin_time/end_time/text/punctuation`） | **只有词级**，字级要自己展开（§6.2） |
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
│  项目页 │ 热词页 │ 校正台(波形+字级标签) │ 成片预览 │ 文稿页            │
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

### 6.3 归一化与字级展开（最容易被低估的一步）

ASR 只给**词级**时间戳，且中文词常为 2–4 字、英文按词。字级标签必须由此派生。

```
ASR word: { text: "今天我们来聊", begin: 1200, end: 1900, punctuation: "，" }
        ↓
按 char 切分（中文按字，英文按空格/连字符为一个 token，数字整体一个 token）
        ↓
时间按字符数比例插值：
  char_i.start = w.begin + (w.end - w.begin) * (cum_i / n)
  char_i.end   = w.begin + (w.end - w.begin) * (cum_{i+1} / n)
        ↓
标记 derived = 1（UI 必须标注"字级时间为估算"）
标点单独成 kind='punct' token，时长为 0，挂在上一 token 的 punc_after
```

**必须知道的后果**：

- 字级时间粒度误差 = `词时长 / 字数`，中文典型 60–200ms/字。**删单个字必然切在词中间**。
- 因此引入 **最小可剪单位（min_cut_unit）**：
  - 默认 `word` 级：删字时自动扩展为整个词的时间区间，UI 显示虚线框提示"实际剪掉的是整词"；
  - `strict` 模式：按字级时间剪，但强制吸附到最近的静音谷（§7.4）。
- 这个开关必须在 UI 上可见，否则用户会怀疑"我明明只删了一个字"。

### 6.4 句级 / 字级 标签模型

**内部表示：句 = token 区间**。不存"字属于哪个句"的父子指针，因为重划边界时父子指针会产生大量悬垂引用。

```
sentence = { ord, token_start, token_end }   // 半开区间
不变量 INV-1..4：
  INV-1  同一 version 内，句子按 ord 排序后 token 区间首尾相接、无空洞
  INV-2  首句 token_start == 0；末句 token_end == token_count
  INV-3  每句至少 1 个非 punct token
  INV-4  任何编辑后立即重算 ord，并从 0 重新编号（ord 只是显示序，不是 ID）
```

**用户操作 → 区间操作映射**

| 用户动作 | 语义 | 算法 |
|---|---|---|
| 多选字 [a,b)，**另起一句** | 在 a、b 两处各切一刀 | `split_at(a)` + `split_at(b)` |
| 多选字 [a,b)，**并入上句** | 上句延伸到 b | 若存在覆盖 [a,b) 的句边界，先 `merge_range(a,b)` 再 `merge_with_prev` |
| 多选字 [a,b)，**并入下句** | 下句提前到 a | `merge_with_next` + 对齐 |
| 删句 / 合并相邻两句 | 直接合并区间 | `merge(s_i, s_{i+1})` |
| 拆分 | 句中任意 token 处切 | `split_at(k)` |

所有操作以 `edit_op` 事件落库，**撤销 = 反向事件**，不做快照回滚（快照只用于崩溃恢复）。

**文本纠错是另一维度**：改错字（如"旋枢"→"璇枢"）不改变区间，只改 `token.text`，记 `op_type='retoken_text'`。**必须把"文本纠错"和"时间归属校正"分开记录**，否则统计不准、影响后续热词学习。

### 6.5 剪辑：删除集 → EDL → 渲染

#### 两种输入，一套模型

- 删除字级标签 → 该 token 的 `[start_ms, end_ms)` 加入 `deletion(source='token')`；
- 波形框选 → `deletion(source='range')`。

最终都进入区间代数：

```
1. 规整：按 start 排序，合并重叠/相邻（间隙 < 5ms 视为相邻）
2. 吸附（可选，默认开）：
   - 左右各扩展至最近的静音谷（-40dBFS 以下、≥120ms 的平台中心），
     上限 ±150ms；找不到静音就保持原边界
   - 目的：避免切在元音中间产生"半截字"
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
│ 顶部：项目名 · 版本号 · 播放控制 · 缩放 · 时间码 · 「预览成片」 │
├─────────────────────────────┬─────────────────────────────────┤
│  波形（可以 regions 显示     │  句列表（虚拟滚动）              │
│  删除块=红、说话人色带=下划）│  ┌ 12  00:01:23  嘉宾A          │
│  ─────────────────────────  │  │ 今天我们来聊…  [▶] [删]      │
│  ▲播放头                    │  │  ├ 今 天 我 们 …（字级格子） │
│                             │  └                              │
├─────────────────────────────┴─────────────────────────────────┤
│ 选中 3 个字 → [并入上句] [并入下句] [另起一句] [删除]  (快捷键) │
└───────────────────────────────────────────────────────────────┘
```

### 7.3 字级选择交互

- 句内每个 token 渲染为一个可点格子（不是 contenteditable）。**这是为了能用区间运算，而不是依赖 DOM 选区。**
- 点击 = 单选；Shift+点击 = 连选；拖拽划过 = 范围选；
- 选中态 = `{sentenceId, from, to}`，全局单选组（一次只能一个选择集）；
- 多选跨句时，操作按钮降级为只有"按选区重划"语义；
- 空格键播放/暂停，`[` `]` 上一句/下一句，`Enter` 播放当前句，`Alt+←/→` 播放 0.5s。

### 7.4 删除边界可视

- 字级删除：红块覆盖 token 的 `[start,end)`，若因 `min_cut_unit=word` 扩展了，用**虚线**标出扩展部分；
- 框选删除：自由 region，拖动边缘调整；
- 吸附到的静音谷位置画一条淡蓝色竖线，让用户知道"系统替你挪了 40ms"。

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
POST   /api/versions/{vid}/render                      渲染成片
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
| M2c | 影子测试（采样 A/B）+ 权重推荐 | 待做 |
| M3 | 校正台 UI：波形 + 句列表 + 字级选择 + 重划边界 | 手工校对 2 分钟音频，无卡顿、无数据错乱 |
| M4 | 剪辑：删除集、EDL、渲染、时间轴重映射 | 成片时长与字幕时长一致，无爆音 |
| M5 | 字幕导出 + 文稿生成 + 导出 | SRT 播放器验证；DOCX 可读 |
| M6 | 稳定性：崩溃恢复、错误提示、成本面板、打包 | 断电重开不丢编辑 |

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
  core/tokens.py        word → 字级 token（CJK 按字、ASCII 整词、时间插值、标点归属）
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
apps/api/tests/  119 个用例（+1 live），覆盖字级展开算术、句区间不变量、
                 offset 拼接、切点选择、请求体 wire format、per-key 能力裁剪
samples/speech.wav  17.6s 中文测试音频（SAPI 合成）
```

**真实验证结果**（API Key + 工作空间域名 `ws-n330k7al4hg4i087.cn-beijing.maas.aliyuncs.com`）：

| 项 | 结果 |
|---|---|
| `list_vocabulary` | 200（鉴权与工作空间正确） |
| 端到端转写 17.6s 音频 | 成功：3 句 / 61 字 / `derived_ratio=0.852` |
| 中文文本正确性 | ✅「今天我们来聊一聊播客剪辑。首先介绍一下嘉宾张三…」 |
| 字级切分 | ✅ 逐字拆分，标点挂在末字 `punc_after` |
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
| D3 | 字级删除粒度 | 默认**词级吸附**（删字扩为整词，UI 虚线标注）；提供 strict 开关 | strict 必须同时开启静音吸附，否则切在元音中间 |
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

1. **字级时间戳是插值出来的，不是 ASR 给的。** 删单字必然切在词中间 → 需要最小可剪单位。
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
20. **字级标签要能看见时间**：hover 显示 `00:01:23.450–00:01:23.780`，否则用户没法判断该不该删。
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
| 512 / 4096 | 放大到字级 |

**编辑接口（`services/editing.py`）**：句边界（另起一句/并入上下句/合并）、
文字修改、撤销。句边界与文字修改用不同 `op_type` 分开记录——这是 §6.4 的要求，
否则无法统计「哪类错误最多」。

### 前端要点

- **波形不用 wavesurfer 解码路径**，只用 canvas 画后端给的桶；播放用原生
  `<audio>` + HTTP Range，拖动不重下整文件。
- **字级选择是 token 点击/拖拽**，不是 DOM 选区。所有编辑都归约为 token 区间运算，
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
