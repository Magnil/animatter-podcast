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
> 少掉的库没有变成技术债——自绘波形和自研虚拟列表反而各自绕开了一个坑（§7.1、§7.6）。

### 3.3 依赖红线

- 禁止：`dashscope`、`pydub`（会隐式调 ffmpeg 且慢）、`librosa`、`moviepy`
  （实测 22.3 ms/帧，1 小时成片要 40 分钟，比直接拼 ffmpeg 慢一个量级）。
- 允许：`httpx`、`tenacity`、`numpy`、`srt`（或自写 SRT 序列化）、`python-docx`、
  `psd-tools`（**只解析** PSD，用来编译模板；它写不了文字层）、`Pillow`。
- 前端禁止引入完整编辑器框架（ProseMirror/Slate）做字幕编辑——用**自研虚拟列表**，见 §7.6。

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
│ storage/     │ template.py  │ script.py     文稿结构                    │
│  temp_oss.py │ video.py     │ video_anchor.py 句子边界吸附网格          │
│              │              │                                          │
│  ↑ template.py 编译 PSD → 背景/框/槽位；video.py 拼 filtergraph      │
│    video_anchor.py 纯函数：句子起点网格、吸附、跨锚点检查（§19.3）      │
├──────────────┴──────────────┴──────────────┴───────────────────────────┤
│  services/  用例编排（事务边界在这一层）                                 │
│   TranscribeService · EditService · RenderService · ScriptService      │
│   VideoTemplateService · VideoRenderService（视频导出 + 静帧预览）       │
├──────────────┴──────────────┴──────────────┴───────────────────────────┤
│  jobs/  进程内 asyncio 队列 + jobs 表（可重启恢复）                      │
├────────────────────────────────────────────────────────────────────────┤
│  store/  SQLite(WAL) + data/projects/<id>/ 文件树                       │
│         data/projects/<id>/video/ ← 模板编译产物、静帧预览、视频导出      │
└────────────────────────────────────────────────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │ 阿里百炼               │
                    │ 临时OSS · ASR · Qwen  │
                    └───────────────────────┘
```

**视频管线的一条跨服务边**：视频导出**不重新编码音频**，最后一步是从已经渲染好的成片音频
`-c copy` 混流（实测 0.24 s / 60 s）。所以它依赖 `RenderService` 的产物，而不是自己算一遍
EDL 音频——否则一条 105 分钟的片子要多烧十几分钟 CPU。见 §19.5。

**分层铁律**

1. `core/` 必须是纯函数：输入 dataclass，输出 dataclass，**不碰 DB、不碰文件、不发网络**。所有时间轴算法在这里，可被单测穷举。
2. `providers/` 只做协议适配，不做业务判断（重试放 provider，语义判断放 service）。
3. `services/` 是唯一的事务边界，一个用例一个事务。
4. `routers/` 只做参数校验与权限（本地无鉴权，但要有 CSRF/Origin 校验）。
5. 前端不得直接拼 ffmpeg 参数，也不得持有 API Key。

### 4.1 模块清单（`core/` / `media/` / `services/`）

上面那张图只画到 `template.py` / `video.py` 这一层——§19 的模块数量在两轮提速之后翻了一倍，
所以单独列一张表。只写**名字不能说明的那句**，名字能说明的不写。

| `core/`（纯函数） | 一句话 |
|---|---|
| `layout.py` | 用户挪过的位置（§19.38）：`scale_art` / `scale_font` / `cascade_name_plate`，编译几何是默认值 |
| `textslot.py` | 哪些 PSD 文字层变成可编辑槽，以及它们的默认值（§19.12） |
| `textspan.py` | `**...**` 标出的变色段（§19.13） |
| `video_box.py` | 一个时间轴块的时长怎么分给它的几张图（§19.3.1） |
| `video_avatar.py` | 谁说谁亮：lit/dim 的算术（§19.5） |
| `video_workers.py` | 按空闲内存决定开几个 ffmpeg（§19.34） |
| `deck.py` / `slide.py` | 文稿→页面切分、页面文字排版（§19.6） |
| `video_anchor.py` | 句子起点网格、吸附、跨锚点检查（§19.3） |

| `media/`（ffmpeg 与 PIL） | 一句话 |
|---|---|
| `template.py` | PSD → 底图/底板/描边/头像槽/文字槽（§19.2、§19.35） |
| `plate_bake.py` | 把不变的三层焊成一张 PNG，以及被头像盖住的文字（§19.30） |
| `box_track.py` | 素材框内容：很多段进、**一个** overlay 出（§19.5） |
| `avatar_track.py` | lit/dim 静帧拼成一条 RGBA 片（§19.5） |
| `video_chunks.py` | 把整片切成一个 ffmpeg 进程能渲染的块（§19.27） |
| `text_overlay.py` / `fonts.py` | 可编辑文字画成透明 PNG / 字体发现（含 `data/fonts/`） |
| `subtitle_ass.py` / `cue_png.py` | 烧进画面的字幕（ASS）/ 预览用的单条字幕条带 PNG |
| `backdrop.py` / `slide_render.py` | `blur_fill` 底衬 / 演示稿页面 PNG |
| `memory.py` | 编码前的内存闸门（低内存时降并行而不是 OOM） |

| `services/`（事务边界） | 一句话 |
|---|---|
| `video_templates.py` | 模板生命周期：导入、编译、确认、槽几何（§19.2） |
| `video_template_package.py` | `.template` 包：导出、导入、七类拒绝（§19.36） |
| `video_layout.py` | 有效几何的唯一读取点：编译几何 + 用户覆盖（§19.38） |
| `video_edits.py` | 文字覆盖与换头像（§19.12） |
| `video_avatars.py` | 槽→说话人绑定，以及它隐含的亮暗时间线（§19.5） |
| `video_plan.py` | 整片计划：前端实时预览的数据模型（§19.31.2） |
| `video_render.py` | 视频导出与静帧预览（后台任务，可取消） |
| `video_assets.py` / `video.py` / `video_anchors.py` | 素材库 / 时间轴块与吸附 / 编辑后修锚点（§19.3、§19.4） |

三个边界值得单独记一句：

* `video_layout.py` 是**唯一**把 `slots_json["texts"|"avatars"][*]["rect"]` 读成
  「现在摆在哪儿」的 service——别处要位置就问它，否则「用户挪过」这件事会有一半代码不知道
  （§19.38.1）。`video_templates.py` 也会读 `slots_json["art"][*]["rect"]`，但那是**编译产物**
  自己的尺寸，不随用户拖动变化；
* `template.py` 与 `plate_bake.py` 只被 `services/` 调用，不碰数据库；
* `core/` 里唯一需要外部数字的是 `video_workers.py`，它的入参是空闲内存 GB，不是 `psutil`。

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
      derived INT,                               -- 1 = 时间由插值估算
      revised INT DEFAULT 0,                     -- 1 = 人改过文字（蓝字）
      hidden INT DEFAULT 0)                      -- 1 = 只在字幕里隐去，音频还在

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

-- 视频（§19）：模板 / 素材库 / 演示稿 / 时间轴。完整字段见 docs/video.md §19.4，
-- 这里只列两条容易记错的——
-- video_asset.original_name：素材落盘按 sha256 改名，界面要显示导入时的文件名，
--   所以名字必须存下来（文件名反推不出来）。空串 = 这一行早于这一列，显示时退回哈希名。
-- video_template.edits_json：**三个写者共享一个 JSON 列**（文字覆盖 / 换头像 / 布局覆盖），
--   每个写者都必须保留另外两个键；布局项与编译值差 ≤1 px 时会被丢掉，这样 is_default 才诚实。
```

### 5.2 文件树

```
data/
├─ app.db                        # SQLite（WAL）
├─ fonts/                        # 模板包带进来的字体（§19.36），fonts.set_private_dir() 加进扫描目录
└─ projects/<project_id>/
   ├─ <项目名>.project           # 轻量快照，可读（文件名跟项目名走）
   ├─ media/
   │   └─ original.<ext>        # 原始，只读，永不变（剪辑永远读它）
   ├─ transcripts/
   │   ├─ v1.raw.json           # 服务端原样，存档
   │   └─ v1.normalized.json    # 归一化 token 流快照（供 diff）
   ├─ peaks/
   │   └─ <sha256>.pkc          # 多级波形峰值，二进制
   ├─ silences/
   │   └─ <sha256>.v1--35db-0.3s.json   # 按媒体 sha + 参数缓存；改了门限就是另一个文件
   ├─ derived/
   │   └─ asr/asr_input.mp3     # 16k 单声道，仅供上传（转写完可以删，下次转写会重建）
   ├─ edits/
   │   └─ ops.jsonl             # 【未实现】最初设计的追加式事件日志；撤销实际走 edit_op 表
   ├─ exports/
   │   ├─ <ver_id>-<edlhash12>.m4a / .wav     # 成片音频（拷到用户文件夹后这份仍留着）
   │   └─ exp_<id>.srt          # 导出过的时间轴文本
   ├─ logs/
   └─ video/                    # §19 的全部产物，四类各一个目录
      ├─ assets/<sha256>.<ext>  # 素材库：拷进来的图与视频，按内容哈希改名
      ├─ decks/<deck_id>/       # 演示稿：每页一张 PNG
      ├─ out/                   # 静帧预览、chunk、中间图层、导出缓存
      └─ templates/
         ├─ source-<sha12>.psd        # 导入时拷的 PSD 副本（原文件可以删）
         ├─ build-<sha12>/            # 编译产物（§19.35：底图/底板/描边各一张，各带 rect）
         │  ├─ background{,_open,_bare,_bare_open}.png
         │  ├─ plate.png  overlay.png
         │  ├─ avatar<N>_{lit,dim,frame,mask}.png
         │  └─ edits/                 # 换头像写这里；asset_path() 也只查这两个目录
         └─ packages/<sha12>/         # 导入 .template 解出来的内容（manifest.json 当 source_path）
```

**`video/` 下四个目录不是同一种东西**：`assets/` 是用户素材（可删，删了时间轴会警告）、
`decks/` 是可重建的派生物、`out/` 全是缓存（删了只会重算）、`templates/` 是唯一「删了就要重新
导入 PSD」的东西。清理磁盘时按这个顺序动手。

**原则：原始媒体 immutable，所有编辑是"对原文的引用"。** 用户删了 3 小时音频，磁盘上也只多一个 EDL。

---

