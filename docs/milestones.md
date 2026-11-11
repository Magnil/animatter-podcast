# 里程碑与交付清单

> 读这份回答：**做到哪了、每个里程碑验收时是什么标准。**
> 只想知道「现在还差什么」的话看 `todolist.md` 更快；这份是历史验收记录。
>
> 章节号 `§N` 全局唯一，源码注释里的 `AGENTS.md §N` 指的就是这里。

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
| **M5b ✅** | 文稿生成：ShowNotes / 对话稿 / 文章稿，Markdown 导出 | ✅ `routers/script.py` + `core/script.py`；**只出 `.md`**，DOCX 未做 |
| **§ 工作台** | 菜单栏、三栏布局、设置、存档、操作记录 | ✅ 见「工作台（2026-11-05）」 |
| **§ 时间轴对齐** | 整条时间轴整体偏移，可撤销 | ✅ 见 CHANGELOG「时间轴对齐（2026-11-09）」 |
| **§ 视频 P1 ✅** | 视频剪辑第一步：PSD 模板编译、句子边界锚点、静帧预览、导出 mp4 | ✅ 见下方「视频 P1（2026-11-11）」 |
| **§ 视频 P2 ✅** | 素材库、拖到时间轴、四种 fit、循环/截断、画廊、空档填底 | ✅ 见下方「视频 P2（2026-11-11）」 |
| M6 | 稳定性：崩溃恢复、错误提示、成本面板、打包 | 断电重开不丢编辑 |

### 视频 P1 交付清单（2026-11-11）

设计见 `docs/video.md` §19，完成状态表见 §19.10。

```
会话边界：视频剪辑从这一轮起有实现，不再是提案。

分享日 4 个 commit（cf6e532 文档 → 1a/1b/1d → 1c）之后，这一轮补完接线与真机验证：
  a4fe971  feat(video): 锚点跟随两条会移动网格的编辑（边界编辑、整片平移）
  d421b80  feat(web): 1c 视频区界面——模板确认、静帧预览、导出
  ae41524  fix(video): 长字幕必须自己断行，libass 不会替我们断

apps/api/src/animatter/
  core/video_anchor.py       句子边界网格 + 吸附 + 重吸附（previous 网格决定谁该动）
  services/video.py          块的增删改查；orphaned 与成片时长都在读时算
  services/video_anchors.py  两个会移动网格的入口（边界编辑、整片平移）
  media/template.py          PSD → 背景/框/头像亮暗图（58 层 6.3s）
  media/video.py             filtergraph（写文件传，避开 Windows 32,767 上限）
  media/subtitle_ass.py      ASS + CJK 断行 + 字体存在性断言
  services/video_templates.py  导入/编译/取产物
  services/video_render.py   静帧 + 导出
  routers/video.py           11 条路由
  alembic c4e1a7b3f9d2        video_template / video_asset / deck / slide / video_item

apps/web/src/
  VideoPanel.tsx       视频区（默认收起；静帧、字号/留白滑块、孤儿块、导出进度）
  videoTimeline.ts     纯函数：块布局、吸附、画廊时长、摘要（26 条单测）
```

**故意的边界**：素材块只能通过 API 建，界面上没有拖拽和素材库——那是 P2。
所以 P1 导出的成片只有背景 + 字幕，框内是空的。

### 视频 P2 交付清单（2026-11-11）

```
586c1c5  feat(video): 2a 素材库——文件复制进项目、按哈希去重、删除时拦住占用
11a620f  feat(video): 2b 框内轨——多段素材合成一条链，只 overlay 一次
194bfb5  feat(web): 2c/2d 视频时间轴可拖拽 + 素材库面板

apps/api/src/animatter/
  core/video_box.py          画廊切分（累积取整）+ 四种 fit 的几何
  services/video_assets.py   导入/去重/列表/删除占用的拦截/按项目取文件
  services/video_box.py      块 → 输出时间轴上的段；空档与重叠的处理
  media/box_track.py         段 → 一条链（输入编号、标签、trim、concat）
apps/web/src/
  VideoTrack.tsx             拖动/缩放/落点，块按原始宽度画 + 成片占比条
  AssetLibrary.tsx           路径导入 + 选文件上传 + 拖到时间轴
```

**实测**：竖图 1080×1920 进 1440×810 框 `blur_fill` → 模糊底 + 完整居中；4 秒视频刚好铺满；
两图画廊各 2 秒；空档显示底板。成片 16.4 秒 / 517 KB / 1920×1080。

**还没做**（P3 / P4 时点）：演示稿 `deck`/`slide`、`.pptx` 导入导出。

### 视频 P3 交付清单（2026-11-11）

```
d5f2b8c1e6a3  video_template.bindings_json —— 头像位绑到哪个说话人

apps/api/src/animatter/
  core/video_avatar.py       区间合并（桥接 600ms 的换气）、取反、明暗序列、lit_at
  media/avatar_track.py      lit/dim 静帧 → concat demuxer → 一条 qtrle RGBA 轨
  services/video_avatars.py  绑定读写、说话区间（映射到输出时间）、按槽位统计时长
  media/video.py             AvatarLayer + filtergraph 里的头像 overlay（框之后、字幕之前）
  services/video_render.py   avatar_tracks / _avatar_layers / _avatar_stills / _base_background
apps/web/src/
  VideoPanel.tsx             头像位行：说话人下拉 + 「N 段 · 时长」
  api.ts                     VideoAvatar / videoAvatars / bindVideoAvatar
```

**实测**（真实项目 583 句版本里说话人交替最密的 14 秒）：6 次切换全部落在期望时刻（误差 <
1 帧）；静帧在 3000/8000/11000 ms 的明暗与 `lit_at()` 一致；成片 14.0 秒 / 199 KB。
明暗轨生成成本：600 秒 / 1200 段 → **1.5 秒、55 MB**。

**顺手修掉 P1 的 dim bug**：旧实现按照片矩形压暗，把头像组自己的白承板一起压灰，兜出一个灰
方块。现在用「去掉 smart object 的组」做底、只压颜色或 alpha 变了的像素；只比颜色会漏掉白色
描边（那里底图 RGB 纯白、alpha 0），出来满身白点。承板角落实测 `(129,128,128)` → `(252,247,244)`。

### 视频 P4 交付清单（2026-11-11）

```
core/deck.py                分页：60 s 上限 / 45 s 后可断在停顿上 / 480 字上限
core/slide.py               排版：换行、标点悬挂、拉丁词不劈、自动缩字号
media/slide_render.py       PIL 出图，三版式 title / title_body / quote
services/decks.py           deck 存储、生成、编辑、重排、按框尺寸出图与失效
services/video_box.py       _slide_stretches：页起点过 EDL，被删光的页丢掉
routers/decks.py            GET /projects/{id}/deck、POST /versions/{id}/deck/generate、
                            PATCH /slides/{id}、POST /slides/{id}/move、DELETE、出图
DeckPanel.tsx               左字幕右编辑、缩略图、上移下移、跳字幕、放进素材框
```

**分页规则是这一轮唯一重新设计过的东西。** 第一版「遇到 ≥1.5 s 停顿就断」，实测那期 61.5 分钟
的节目出 **238 页**——每页 15 秒，是幻灯片不是演示稿。成因不是阈值取错，是规则错：那期句间停顿
p50 只有 320 ms，1.5 s 以上的停顿一小时只有 12 次，于是「遇停顿就断」实际退化成「遇长度上限就断」。
改成**按时长**：跑满 60 s 就断，或者已经跑满 45 s 且后面有 ≥700 ms 停顿（断在自然缝上）。
同一份素材现在是 **60 页**，页长 p10 54 s / p50 63 s / p90 71 s。

**排版三处都是量出来的。** 第一版按宽度贪心换行、事后把以标点开头的**整行**并回上一行，实测出现
2.3 倍框宽的行走到了画面右边缘（`ink x 42..1266`，最后一列有墨）。改成在断点处调：闭合标点悬挂、
开引号不留行尾（真实出现过「比如说《」/「BIBBIDIBA》」）、拉丁词不劈（`《BI` / `BBIDIBA》`）、
「，」之后断但窗口只有 10 %（窗口放宽会让每行都停在第一个逗号上，版面参差）。

**两个只有真机才看得见的 bug。** 一是幻灯片 PNG 只按 slide id 缓存，编辑器用 1267×712 请求过一次
之后，1440×810 的素材框拿到的是同一张图，被 overlay 放大——正文软掉并溢出框右缘。改成文件名里带
尺寸，`invalidate` 清掉所有尺寸。二是页面底部与烧入字幕打架：字幕按整帧的 `margin_v` 画，对框里
有什么一无所知，实测最后一行正文离字幕顶端只有 ~10 px。现在底部预留 9 % 框高（810 的 73 px）。

**实测**（真实项目 `ver_1m6v2q5javqm1j82xsg`，583 句 / 17,294 字 / 61.5 分钟）：

| 检查 | 结果 |
|---|---|
| 分页 | 60 页；页长 p10 54 s / p50 63 s / p90 71 s |
| 单页排版 | 293 字 → 36 px / 10 行，占 614 / 628 px，**没有一行超出框宽** |
| 出图 | 1267×712 ≈ 55 ms / 页，PNG 180–230 KB |
| 页起点 | 全部落在句子起点（5100 / 70970 / 132980 ms …），直接当素材块边缘 |
| 成片静帧 | 75 s / 100 s / 140 s / 190 s 四帧，框内分别是第 2/3/4/5 页，与页起点一致 |

**还没做**：`.pptx` 的导入与导出 —— v1 明确不做（§19.6 拍板）。



### 进度一览（2026-11-09 订正）

```
M0 ✅ 骨架          M1 ✅ 转写闭环       M2a ✅ 热词库
M2b ✅ LLM 校对     M2c ⏸  影子测试(推迟)  M3 ✅ 校正台 UI
M4 ✅ 剪辑+时间轴   M5a ✅ 字幕导出       M5b ✅ 文稿(md)
M6 ⬜ 稳定性
工作台 ✅   时间轴对齐 ✅   视频 P1 ✅   P2 ✅   P3 ✅   P4 ✅（2026-11-11）
```

**验证规模（2026-11-11，P4 收尾）**：后端 1118 用例通过 / 1 skipped / 3 deselected，
`mypy --strict` 95 个源文件零错误，`ruff` 干净；前端 219 条 `node --test` 用例，
`tsc --noEmit` 与 `vite build` 干净；真机跑通「60 页演示稿 → 放进素材框 → 静帧四帧页切换正确」。

> 这张表 2026-11-05 之后四天没更新，期间一直把 M5b 记成「待做」，而它已经落地
> （`c3226fb` feat: ShowNotes and 文稿 generation）。**里程碑是自报状态，不是从代码推断的，
> 所以它只会比代码旧**——完成时顺手改一行，比事后核对便宜。

**验证规模（2026-11-09）**：后端 651 用例通过 / 1 skipped / 3 deselected，`mypy --strict` 74 个源文件零错误，`ruff` 干净；
前端 133 条 `node --test` 用例，`tsc --noEmit` 与 `vite build` 干净；真实音频端到端跑通（17.6s 样本 + 两个 1 小时级真实播客项目）。

### M0 交付清单（2026-11-05）

```
apps/api/src/animatter/
  config.py      配置 + loopback 强校验（拒绝 0.0.0.0）
  log.py         structlog + sk-*/Bearer/signature 脱敏
  ids.py         可排序前缀 ID（prj_…）+ UTC 时间戳
  db.py          SQLite 引擎 + WAL/foreign_keys/busy_timeout pragma
  models.py      16 张表（project/media/speaker/transcribe_job/…/job）  ← 2026-11-09 已增至 18 张
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

