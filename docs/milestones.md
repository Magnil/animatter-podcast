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
| **§ 视频 P3 ✅** | 头像位绑定说话人、明暗轨 | ✅ 见下方「视频 P3（2026-11-11）」 |
| **§ 视频 P4 ✅** | 演示稿：按话题分段、逐页编辑、进素材框、与字幕同步 | ✅ 见下方「视频 P4（2026-11-11）」 |
| **§ 视频 P5 ✅** | 模板可编辑（文字/头像可换）、双时间轴界面、S 与全屏、随时导出、分块渲染 | ✅ 见下方「视频 P5 及之后（2026-11-12 → 11-13）」 |
| M6 | 稳定性：崩溃恢复、错误提示、成本面板、打包 | 断电重开不丢编辑（未开始，见 `todolist.md` E2） |

### 视频 P1 交付清单（2026-11-11）

设计见 `docs/video.md` §19，完成状态表见本文件下方「视频 P1–P4 交付清单」。

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

**还没做**：`.pptx` 的导入与导出 —— v1 明确不做（D13 in `docs/decisions.md` 已拍板，别再讨论）。

### 视频 P5 及之后交付清单（2026-11-12 → 11-13，第 96–121 轮）

P1–P4 之后的十几轮都是在这个骨架上迭代。逐轮的「为什么」在 `docs/video.md` §19.13–§19.27 和
`docs/CHANGELOG.md`，这里只记**验收过了什么**。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 模板文字可编辑、头像可换图 | `core/textslot.py`、`media/text_overlay.py`、`services/video_edits.py`、迁移 `b8d4f1c6a2e7` | 改文字不重编译 PSD；未绑定槽连名字一起消失 |
| 双时间轴工作区（抽屉 + 标签页，两条轴共用一个窗口） | `apps/web/src/App.tsx`、`VideoTrack.tsx`、`DeckPanel.tsx`、`TemplateTexts.tsx` | 点视频块不动音频轴的播放头 |
| 第五种填充 `blur_crop`、`cover` 锚左上、Shift+←/→ 拉一边 | `core/video_box.py`、`media/box_track.py` | 验的是像素，不是过滤器字符串 |
| 素材块自己的声音：M（静音）/ S（顶替播客），**恰好一个亮** | `media/video.py` 的 `AudioMix`、迁移 `e3a7c1d5b920` | 验的是振幅和整片相关性 |
| 展现方式：素材框 / 全屏，全屏块在最顶层 | `media/box_track.py` 两条链、`media/video.py` | 逐帧看谁压在谁上面 |
| 演示稿按话题分段、按页命名块、页字体分开设 | `core/deck.py`、`core/deck_outline.py`、`media/slide_render.py`、迁移 `f4b8d1c7a2e9`、`a7c3e9f14b26` | 60 页 / 61.5 分钟，页起点全落在句子起点 |
| 随时可以导出（不再要求先剪） | `services/video_render.py`、`routers/video.py` | 未剪的项目也能出一条带背景+字幕的片子 |
| **分块渲染**：修掉 OOM（根因是分支数，不是分辨率） | `media/video_chunks.py`（新）、`services/video_render.py`、`config.video_chunk_workers=1` | 14 块 `-c copy` 拼接，帧数精确 88702 |
| **默认画布 1080p24**（原 25） | `media/box_track.py`、`config.py` | `r_frame_rate=24/1`，素材边界不再重复一帧 |
| 修「头像框消失」 | `core/video_avatar.pieces_in`、`media/video.py` 头像输入不带 `-ss`/`-t` | 全片 47 个采样点、用户点名的 3 处，两个上方头像位均非空 |
| 修「三个素材块消失」 | `media/box_track.py` 的 `trim=end_frame`（≥1 帧钳制） | 145.6–224.7 s 与 `preview_still` 差 <3/255 |

**第 121 轮的交付物**（用户报的两个 bug 都在这条线上）：

```
D:\Videos\费那奇MV-1080p24-v2.mp4
  1920x1080 / 24 fps / 88,702 帧 / 3,695.917 s / 974.6 MB / aac 48kHz 立体声
  整片解码零错误；逐点复核 47 处头像 + 10 处素材块，0 处异常
```


### 视频 P6 交付清单（2026-11-14，第 123–126 轮）

导出提速与降内存。设计、实测表和排除项在 `docs/video.md` §19.28，决策是 `decisions.md` D18。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| `-filter_complex_threads` 不再跟 ffmpeg 默认（16 片 → OOM） | `config.video_filter_threads = 4`、`media/video.py::render(filter_threads=...)` | 命令里出现在**第一个 `-i` 之前**（它是全局选项），测试钉住 |
| 静帧素材段预合成一张 PNG，只循环它 | `media/video.py::composite_still`、`box_track.with_still_sources`、`BoxSegment.precomposited`、`services/video_render.py::_composite_stills` | chunk 2 生产路径 **33.0 s / 3.03 GB**（改前 65–93 s / 4.7–6.2 GB）；逐帧 PSNR 对实时链 **最小 46.97 dB / 平均 48.97 dB**，2286 帧全部 ≥45 dB |
| 全透明的 top 链不建 | `services/video_render.py::_needs_top_chain` | chunk 8 **267.4 s → 206.4 s**，输出逐帧 **PSNR inf**、字节数不变 |
| 一个开关可逐字节回退 | `config.video_still_composite` | 置 `false` 走原来的实时链，图里仍有 `boxblur`（两条路径都有测试） |
| 预合成残留不会跨导出让复用 | `_render_picture` 每块导出前删 `still-*.png`、收尾再删一遍 | 失败残留与下次同名，不删会被静默复用 |


### 视频 P8 交付清单（2026-11-21，第 137 轮）

模板的**布局变成数据**。设计、四次失败与实测在 `docs/video.md` §19.35，决策 D25。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 底板拆成独立 `plate.png`，描边裁到自身 bbox，两个 rect 进 `slots_json.art` | `media/template.py`、`services/video_templates.py` | 重组后与拆分前 **diff px = 0**（两个真实项目、四个组合） |
| 渲染侧按 rect 摆 art：plate/stroke 各一层 | `services/video_render.py`、`services/video_plan.py`、`routers/video_plan.py` | `render_still` 新旧各渲一张，**PSNR `inf`** |
| 框内空隙从 plate 裁（坐标减 plate 原点） | `services/video_box.py::plan(template_origin=)` | 摘录与旧 poster 裁切 **diff px = 0** |
| 布局进四个缓存键 | 导出签名 / 静帧 digest / plan digest / `plate_bake` digest | 挪 plate rect 后四个键全部变化 |
| 前端 plan 字段 `overlay_url` → `plate_url`/`plate_rect` + `stroke_url`/`stroke_rect` | `apps/web/src/videoPlan.ts`、`DomStage.tsx`、`api.ts` | 层序 `background → plate → stroke → media`，plate 用自己 rect |
| 老行（无 `art`）不迁移也不降级 | `art_of()` 的兼容分支、`poster_of()` 的回退 | `tests/test_video_template_art.py` 8 条 |

### 视频 P9 交付清单（2026-11-22，第 138 轮）

模板**可以交出去**了：一个 `.template` 文件带走海报、底板、描边、四个头像和两套字体。设计、
拒绝表与那个只坏在真机的 bug 在 `docs/video.md` §19.36–§19.37，决策 D26。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 导出 `.template`（zip：`manifest.json` + `art/*.png` + `fonts/*.ttf`） | `services/video_template_package.py::build_package`、`GET …/video/template/package` | 真模板 1,723,077 B / 25 成员；包里**无** `edits_json`、`bindings_json`，`content` 槽被剥 |
| 导入（唯一一条外部字节变磁盘文件的路） | `import_package`、`POST …/video/template/package` | 22 张图**逐字节**相同；再导出 25 成员 `lost=0 changed=0`；五个指针全落新目录 |
| 七类拒绝（炸弹 / 穿越 / 后缀 / 无 manifest / 新 schema / 哈希不符 / 缺 art） | `read_package` / `_safe_name` / `_check_hashes` | `tests/test_video_template_package.py` 17 条，判据是错误码 |
| 默认模板随包发布，新项目自动装 | `defaults/default.template`、`services/projects.py::create_project`、`POST …/template/default` | `test_video_template_default.py` 8 条；内置包装坏时项目仍建成 |
| 占位头像一张人像 + 各槽自身环色 | `.tmp/work/make_default.py`（一次性生成脚本） | 四张 `lit` 内圈一致（8×8 缩略两两差 ≤ 12，实测 5）、环色 ≥ 2 种 |
| 字体跟着包走 | `services/video_template_package.py::_install_fonts`、`media/fonts.py::set_private_dir` | 导入后 `resolve("Baigo")` / `resolve("Alimama ShuHeiTi")` 非空 |
| 界面上五条路 | `apps/web/src/VideoPanel.tsx`、`templatePackage.ts`、`download.ts` | 三条 CDP 走查全通过（按钮在视口内、不重叠、accept 含 `.template`） |

### 视频 P10 交付清单（2026-11-22，第 139 轮）

位置与大小不再是设计师的单方面决定。每个框都能在浏览器里拖，改的是 `edits_json.layout`，
**`slots_json` 不被改写**——重编译不会吞掉用户的布局。设计在 `docs/video.md` §19.38，决策 D27。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 布局覆盖层（`box` / `texts` / `avatars`，每项 `{x,y,w,h,font_size?}`） | `core/layout.py`、`services/video_layout.py` | `tests/test_video_layout.py` 38 条；空布局时每个框逐字段等于编译值 |
| 挪框时底板/描边按比例跟、字号跟两轴小者、头像名按位移平移 | `scale_art` / `scale_font` / `cascade_name_plate` | 同尺寸恒等（`{238,206,1444,815}` 不变）；标题 `617×109→720×130` 时 56→64 px |
| 四个缓存键都带 `layout.signature()` | `video_edits.signature`、`_still_digest`、`_video_signature`、`video_plan._digest` | 移动标题后同刻静帧 URL 换 digest；`sort_keys` 保证顺序无关 |
| 三个端点 + 两类拒绝 | `GET/PUT …/video/layout`、`POST …/layout/reset` | `content` 槽 → `layout_slot_fixed` 409；不存在的 key → `layout_slot_unknown` 404；未就绪 → `template_not_ready` |
| 画布编辑器（模板页签里「编辑布局…」打开的弹窗） | `apps/web/src/LayoutEditor.tsx`、`layoutMath.ts` | `layoutMath.test.mjs` 28 条；`check-r139-layout-editor.mjs` 五组判据 `problems: []` |
| 手势：松手提交 / 方向键 400 ms 防抖 / 缩放不吸附 / 框偶数 / 头像锁比例 | `layoutMath.ts` | 拖动一次一个请求；拖后 `location.reload()`「已改动」仍在 |
| 两个恢复默认 | 单块 `PUT`、全部 `POST …/layout/reset` | 「全部恢复默认」后库里 `is_default=true`、`changed == {}` |
| 画面只动该动的地方 | `video_render` 走 `video_layout` | 移动标题前后静帧 `ImageChops.difference` bbox = `(1002, 82, 1698, 181)` |

第 140 轮把入口从「第五个页签」改成模板页签里的「编辑布局…」按钮 + 模态框：布局一次调完就不再动，页签等于每期都问一句。对话框 980 px，画布实测 944 px，比页签里的 478 px 宽一倍；页脚只有「关闭」。决策 D28。

### 进度一览（2026-11-14 订正）

```
M0 ✅ 骨架          M1 ✅ 转写闭环       M2a ✅ 热词库
M2b ✅ LLM 校对     M2c ⏸  影子测试(推迟)  M3 ✅ 校正台 UI
M4 ✅ 剪辑+时间轴   M5a ✅ 字幕导出       M5b ✅ 文稿(md)
M6 ⬜ 稳定性
工作台 ✅  时间轴对齐(整体) ✅  视频 P1–P4 ✅（2026-11-11）
视频 P5 ✅（2026-11-12 → 11-13）：模板可编辑、双时间轴、S/全屏、分块渲染、1080p24
视频 P6 ✅（2026-11-14）：过滤线程数封顶、静帧预合成（chunk 2 33 s / 3.03 GB）、top 链跳过
真机成片 ✅  3,695.9 秒 / 88,702 帧 / 24 fps（第 121 轮，两个 bug 修完后重导）
视频 P7 ✅ **全部实施完成**（11-17 方案 / 11-18 P0 / 11-19 P1、P2 / 11-20 E1、E2、E3）
        已做：静帧预览 1.02–1.33 s → **0.63–0.93 s**；**DOM 分层实时预览已验收**——
        45.8 fps / 0 丢帧 / 跨块切换 75 ms / 图层粒度 p50 32 ms / 一致性 20 时刻 20/20。
        P2 已做：**双 `<video>` 槽位预热**（切点后 600 ms 内 0 帧无画面，关掉预热则每次切点都有）
        + **四条降级路径**；**720p 代理按实测决定不做**（D23，§19.32）。
        E1 已做：**烘焙 plate** ——整片导出 27.84 → **19.46 min（−30.1 %）**，逐帧 PSNR `inf`，
        两个 mp4 MD5 相同。
        E2/E3 已做：`filter_complex_threads` 4→12（整片 **19.46 → 17.67 min，−9.21 %**，输出**逐位相同**）+ 按内存算的
        并行度（可选，1.09–1.23×）+ `-refs 1 -bf 0` 开关（可选，−4 %，+8 % 体积）；
        **E4 的 4K 代理判定不做**——前提（chunk 峰值 5.52 GB）复现不出来，真实值 2.84 GB。
        **导出侧到这里做完了**：整片 27.84 → **17.67 min（累计 −36.6 %）**。
        设计与实测并入 `docs/video.md` §19.31–§19.34（预览架构、播放与降级、静帧、导出并行与编码），
        决策 D19–D23
视频 P8 ✅（11-21）：模板编译产物拆分——底板/描边各带 rect，**diff px = 0**、PSNR `inf`
视频 P9 ✅（11-22）：模板包（`.template` 导出/导入）+ 默认模板随包发、新项目自带
视频 P10 ✅（11-22）：布局覆盖层——画布编辑器拖框、`edits_json.layout`、四个缓存键带布局签名
```

**P7 的预览侧三档（P0 / P1 / P2）与导出侧的全部档位（E1 / E2 / E3，E4 判定不做）已落地**，
判据分别在下文五份「交付清单」里。**现在已经能做实时预览了**——播放头一动，画面就跟着走，跨块不再露出
空白帧；导出从 **27.84 min 降到 17.67 min**（同一台机器、同一份片子、都是整片实跑），
2 个 worker 打开后还能再快 1.09–1.23×。

### 视频 P7 · P0 交付清单（2026-11-18，第 128 轮）

设计、实测与「为什么不是原方案说的缓存」在 [`docs/video.md`](video.md) §19.33。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 原始时间 → 输出时间的映射 | `apps/web/src/timeMap.ts`（`toOutput` / `toOutputClamped` / `toOriginal`）+ `VideoPanel` 先映射再请求，`App` 传 `edl.keeps` | 10 条单测（`timeMap.test.mjs`）；M3 校正台实测：原 12 000 ms → 输出 10 798 ms，两个 `at_ms` 产生**不同**的静帧文件 ⇒ 映射是承重的 |
| transcript 一次请求只读一次 | `services/transcript_facts.py`（按列投影）+ `build_cues` / `speaking_intervals` / `resolve` / `describe` 接受 `facts=` | `avatar_tracks` 245.8 → **3.7 ms**、`_cues_for` 128.8 → **7.1 ms**、`_ass_for` 152.1 → **13.5 ms**；`test_preview_transcript_reads.py` 用 SQL 计数钉住「resolve 只读一次 token 表」 |
| 静帧编码降级 | `media/video.py::render_still` 的 `-compression_level 1` | 330 → **217 ms**，PSNR **`inf`**（逐像素相同），文件 +17 %；`test_video_render.py` 钉住等级与参数位置 |
| 自动预览 + 过期响应丢弃 + 保留上一帧 | `VideoPanel`（300 ms 去抖、`generation` 计数、`is-stale` 55 % 不透明）、`styles.css` | 真机 CDP（`scripts/check-p0-preview.mjs`）：不点按钮自动出图；换句后 `src` 变化；过渡态 `is-stale` + `aria-busy=true` + 「正在合成这一帧…」 |
| 端到端 | — | **1020–1330 → 634–929 ms（p50 ~730 ms，−28 %）**，ffmpeg 占 88 % |

**没有做 LRU 缓存**（原方案写的是缓存）：拆解发现 414 ms 里 400 ms 是同一份 transcript 在
一次请求里被读 5 遍，改成「读一次」就没有失效面可言。原定验收「第二次 ≤ 550 ms」因此作废——
现在每次都是 ~730 ms，因为没有任何东西被缓存。

### 视频 P7 · P1 交付清单（2026-11-19，第 129 轮）

设计与全部实测在 [`docs/video.md`](video.md) §19.31.5（判据与三个「差一帧」的根因）。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 整片 plan（一次请求描述全片） | `services/video_plan.py`、`routers/video_plan.py`：`GET /versions/{id}/video/plan`、`/video/plan/file/{name}`、`/video/cue/{digest}/{i}.png`、`/video/backdrop/{digest}/{pos}.png` | 64 段、772 条字幕、161 KB；摘要进文件名 ⇒ `immutable` 缓存安全；`test_video_plan.py` 19 条 + `test_video_plan_api.py` 17 条 |
| 按需烘焙（不预烘全片） | `media/cue_png.py`（libass 烘字幕带，76.6 ms/条）、`media/backdrop.py`（`boxblur`+`eq` 底图，**无损 PNG**） | JPEG 的 yuvj420p 往返把模糊环挪 4.5 % ⇒ 改无损后烘图与链自渲 **逐像素相同**（max delta 0） |
| DOM 分层合成 | `apps/web/src/DomStage.tsx`、`videoPlan.ts`（`layersAt` 是**唯一**写图层顺序的地方）、`VideoPanel` 有 plan 就用舞台、无 plan 退回静帧按钮 | 图层顺序**量出来的**：`background → overlay(描边) → box → avatars → text → 全屏 → 字幕`；`plate`/`transparent` 段不画元素 |
| 时钟 | `frameStartMs` / `filmFrame` / `localOffsetMs`；`SyncedVideo` 暂停时精确 seek、播放时 200 ms 容忍 | 前后端用**同一个表达式**（`frame_start_ms(at − segment.start)`），整数运算 |
| 循环周期下发 | `loop_period_ms()` 探测 `stream=duration`（**不是** `format=duration`），随 `period_ms` 进 plan | 190.680 vs 190.763 s 差 83 ms ⇒ 三圈 4 帧；`test_a_looped_clip_publishes_the_stream_period...` 用 2.0 s 视频 + 2.4 s 音频的片段钉住 |
| 静帧帧网格 | `box_track.build_single` 的 trim 界 = `frame_start_ms(offset) − 1/FPS + 1 ms` | 循环段 980 950 ms：**13.82 % → 0.00 %**；`test_video_still_seek.py` 26 帧 25 fps 红绿片段 |
| 常驻验收脚本 | `scripts/check-p1-playback.mjs`、`check-p1-consistency.mjs`、`verify-p1-consistency.py`、`p1-park.mjs` | 见下 |

**验收实测**（2026-11-19）：

| 判据 | 门槛 | 实测 |
|---|---|---|
| 不掉帧 | 0 | **0**（12 s 真播放，286 帧解码） |
| 帧率 | ≥ 24 | **45.8 fps** rAF |
| 推进比 | ≥ 1.0 | **1.000** |
| 跨块切换首帧 | ≤ 300 ms | **75 ms**（1080p）/ **189 ms**（进 4K 段） |
| 画面冻结 | — | **51 ms** |
| 图层更新粒度 | ≤ 50 ms | **p50 32 ms**（改前 **217 ms**，字幕可晚五帧） |
| 一致性 | 20 个时刻全过 | **20/20**，平坦区 ≤ 1.95 %、位移 ≤ 0.48 px |

**验证规模**：后端 **1422 用例通过 / 2 skipped**，`mypy --strict` **108** 文件零错误，`ruff` 干净；
前端 `tsc --noEmit` 干净、`npm test` **291 通过**、`vite build` 成功。
新增测试：`test_video_still_seek.py` +2（循环网格）、`test_video_plan.py` +1（循环周期）。

**已知但按口径通过的残差**：1 473 980 ms 的 ÷16 模糊后仍有 4.75 %（平坦区 0.01 %）。它不是纯
相位差，但同一段素材在相邻时刻的 ÷16 都是 0.00 %，所以不是整段素材的问题。**P2 不要重查它。**

### 视频 P7 · P2 交付清单（2026-11-19，第 130 轮）

设计与全部实测在 [`docs/video.md`](video.md) §19.32（双元素预热、四条降级、代理为什么不做，
以及两条测量教训）。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 双 `<video>` 槽位 | `apps/web/src/mediaDeck.ts`：`deckLayout` 是**唯一**决定「哪个槽放哪一段」的地方，第 *k* 个视频段固定住 `k % 2` 号槽；`DomStage` 在 `layersAt` 给 media 的**那个位置**渲染 | 切点后 600 ms 内「无已呈现帧」的帧数：**预热开 0/0/0，关 1–4 帧/次**；改前冷挂载到出帧 **116 ms**（≈2.8 个影片帧） |
| 提前 2.5 s 预热 | `PREWARM_LEAD_MS`；槽位元素用 `visibility: hidden` 保活（**不能 unmount，也不能 `display: none`**——那会丢掉已解码帧） | 4K 块切换首帧 **189 ms**（P1 已有），预热后切换延迟实测 **0 ms**（元素早已就绪） |
| 降级：素材解不了 | `DomStage` 的 `failed` 分支 + `onLoadedData` 里 `drawImage`→`toDataURL` 存一张首帧；从未解出过帧则显示斜纹 + 「无法实时预览」 | 编码不支持是**确定性失败**，所以**不重试**（重试只会每次切进那一段都黑一下）；`videoDegrade.test.mjs` 钉住「失败后 `<SyncedVideo>` 必须消失」 |
| 降级：导出中 / 内存紧张 | `readDegrade()` 每秒轮询（`DEGRADE_POLL_MS`）→ `dropStandby` + 舞台 **444 px** + 角标；`App` 的 `exportFilm` 在 `finally` 里清标志 | `scripts/check-p2-degrade.mjs` 三态全对：常态 584 px 无角标 → 内存紧张 444 px +「内存紧张：预览降级」→ 导出中 +「导出中：预览降级」→ 撤掉后回 584 px；**在播的元素始终保留** |
| 720p 代理 | **不做**（D23）。触发条件与规格留在 §19.32 | 三段 4K 复测 seek 中位 **194.6 / 244.3 / 153.7 ms**，全在 250 ms 线内；冷启动 95–132 ms |

**验收实测**：一致性 **20/20 PASS**（重跑，与 P1 同口径）；真播放 43.2 fps / 0 丢帧 / 推进比 0.996 /
冻结 62 ms（1080p），42.4–43.1 fps / 0 丢帧（4K，一次 2 帧发生在空闲内存 3.45 GB 时）。

**验证规模**：后端 **1446 用例通过 / 2 skipped**，`mypy --strict` **108** 文件零错误，`ruff` 干净；
前端 `tsc --noEmit` 干净、`npm test` **319 通过**、`vite build` 成功。新增测试：
`mediaDeck.test.mjs` 14 条、`videoDegrade.test.mjs` 8 条、`usePlaybackClock.test.mjs` 6 条。

**两条留给下一次的测量教训**（§19.32 末尾）：毫秒级延迟在同一台机器上重复之间会漂
**2 倍**（4K seek 中位 406 → 154 ms），所以结论不要建立在单次数字上；与机器状态强相关的连续量
（丢帧、帧率）只记录、**不当门槛**——否则下次在别的机器上会以「回归」的名义被误报。

### 视频 P7 · E1 交付清单（2026-11-20，第 131 轮）

设计与实测在 [`docs/video.md`](video.md) §19.30（烘焙 plate，含两个实现细节与两个验收坑）。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 底板烘焙 | `apps/api/src/animatter/media/plate_bake.py`：`bake()` 把底板 + 描边 + 文字合成一张 `plate-<digest>.png`，返回「还要不要叠文字」+ 补丁后的头像 PNG | 单帧 PSNR **`inf`**（r/g/b/a 全 `inf`）；真实样片补丁 **598** 像素 / 2 个槽位（`alpha ≥ 1`；bench 的 582 是 `≥ 9` 口径） |
| 头像补丁 | 同上：先 `ffmpeg scale=177:188` 再在槽位分辨率上合文字，图里那次 `scale` 因此是恒等变换 | 已缩好的 PNG 再 `scale` 一次：`bicubic`/`default`/`neighbor`/`lanczos` **四种 flags 全部 0 像素差**；PIL 缩放则差 8 178 px（54.38 dB） |
| 前提校验 | 每次烘焙现算（不落 `slots.notes`）：`text ∩ box_rect = 0` 且文字像素下的头像 alpha 全 255；不满足退回「只烘底板 + 描边」 | 三条退出路径各有单测（文字进 box / 半透明头像 / 无文字） |
| 图与开关 | `_render_picture` 成功后 `layers=[]`、`text=None`、`background=plate.path`；`_avatar_layers(sources=…)` 换成补丁 PNG；`video_plate_bake=False` 逐字节回退 | 开关关闭时 `_plate_for` 返回 `None`（单测） |
| 缓存与清理 | `plate-<digest>.png` / `avpatch-*.png` **跨导出复用**，清理规则是「写完新的删掉 digest 不匹配的」 | 同一输入两次烘焙得到同一路径；改文字后旧图被删 |

**验收实测**：

| 判据 | 门槛 | 实测 |
|---|---|---|
| 单 chunk 纯静帧（94.1 s / 2 259 帧） | PSNR `inf` | **`inf`（2 259/2 259）**，三次 38.23→21.92 / 40.73→24.64 / 39.5→23.4 s，**−39.5 ~ −42.7 %** |
| 单 chunk 含视频（235.4 s / 5 650 帧） | PSNR `inf` | **`inf`（5 650/5 650）**，110.25 → **66.31 s（−39.9 %）** |
| 全片（3 695.9 s / 88 702 帧） | ≥ 45 dB | **`inf`**（汇总行 `y:inf u:inf v:inf`；帧数双证 **88 702 / 88 702**），两个 mp4 **MD5 相同**（924 297 354 B） |
| 全片耗时 | −30 % 以上 | **1670.7 → 1167.4 s（27.84 → 19.46 min，−30.1 %）** |

**验证规模**：后端 **1458 用例通过 / 2 skipped**（`-n 2`），`mypy --strict` **109** 文件零错误，
`ruff` 干净；前端 `tsc --noEmit` 干净、`npm test` **319 通过**、`vite build` 成功。
新增测试：`test_video_plate_bake.py` **12 条**（其中 2 条是真的 x264 编码对比）。

**一条口径提醒**：等价时间线上的 −36.7 % 是「静帧占 72 %」的
时间线的上界，真实片子（静帧 58.4 %）折扣后约 −30 %——本轮整片实测 **−30.1 %**，量级对上
（这台机器重复之间漂 3 个百分点，所以只能当「≈30 %」）。**拿 −36.7 % 做预算会高估 4 个百分点。**

### 视频 P7 · E2 + E3 交付清单（2026-11-20，第 132 轮）

设计与全部实测在 [`docs/video.md`](video.md) §19.34（峰值口径、`ft`、并行门限、编码参数、4K 代理
判定）；一行摘要式的设计约束在 §19.28.6。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 峰值口径重测 | 14 块逐块 `-f null`（x264 照常编码），chunk 8 另用**真写文件**复核；门限表常驻在 `scripts/verify-e3-workers.py formula` | 6 段块 **2.54–2.87 GB**（纯静帧 2.83 / 含 1080p 2.64 / **含 4K 2.84——无差别**），1–3 段块 1.18–1.80 GB；真写文件 2.83 vs `-f null` 2.84 |
| `ft` 4→12 | `config.video_filter_threads = 12`；`media/video.py::render(filter_threads=…)` 原有的传递链 | **整片实跑 19.46 → 17.67 min（−9.21 %）**，帧数与 MD5 完全不变；逐块交错 A/B：纯静帧块 **−15.0 %**、4K 块 **−11.0 %**、591 s 视频块 **−8.6 %**（14 块求和 −6.6 %，比整片保守 2.6 点）；峰值 2.62–2.87 GB 不变 |
| 按内存算 worker | `apps/api/src/animatter/core/video_workers.py`（纯函数）+ `media/memory.py`（`ullAvailPhys`） | 门限 = 2.5 GB 余量 + 2×单块峰值（满块 **8.3 GB**）；硬上限 2；`inf`（探针失败）走硬上限而不是 0 |
| 并行开关 + 原因 | `ExportOptions.faster` → `plan_workers` → `render.note`（迁移 `c6a2f8d1b4e7`）→ 进度行 | 2 worker：两个静帧短块 **1.227×**，「4K 块 + 静帧块」**1.090×**；面板文字与 `video.workers workers=N` 同值 |
| 编码选项 | `ExportOptions.fast_encode` → `media/video.py` 的 `-refs 1 -bf 0` + API 字段 + 设置项 | 默认关；视频块 −6.4 % 时间 / +8.2 % 体积 / min PSNR 39.7 dB（**不再逐位相同**，故不当默认） |
| **E4 判定不做** | `docs/video.md` §19.34.6；`todolist.md` D7 删除 | 旧说法「含 4K 峰值 5.52 GB」「峰值来自 4K 解码」都被逐块实测否定（2.84 GB；退回旧图 3.58–4.14 GB ⇒ 来自全画布混合） |

**验收实测**：

| 判据 | 门槛 | 实测 |
|---|---|---|
| 整片耗时（E1 → E1+E2a） | 有收益即做 | **1167.4 → 1059.9 s（19.46 → 17.67 min，−9.21 %）**，整片实跑；帧数 88 702 不变、文件 **MD5 逐位相同**（`D96A92BF…`，924 297 354 B） |
| 2 worker 收益 | 原定 ≥1.25× | **1.09–1.23×**（原验收线作废：抬 ft 已经吃掉一部分并行收益） |
| 峰值 | < 6.5 GB | 单块最大 **2.87 GB**，并行最坏 **3.40 GB** |
| OOM | 0 次 | **0 次**（14 块逐块 + 4 次并行组合 + 2 次接线） |
| 接线（不是单测） | 真的开两个进程 | `workers=2`、`note`=「2 块，2 个并行…」、成片 **4 232 帧 = 2 259 + 1 973** |

**验证规模**：后端 **1478 用例通过 / 2 skipped**（`-n 2`），`mypy --strict` **111** 文件零错误，
`ruff` 干净；前端 `tsc --noEmit` 干净、`npm test` **321 通过**、`vite build` 成功。
新增测试：`test_video_workers.py` **20 条**（门限 / 硬上限 / `inf` 哨兵 / 理由文案 / 编码开关的
正反两半 / 接线形态）、`videoExport.test.mjs` **2 条**（`render.note` 替换进度行的兜底文案）。
新增迁移：`c6a2f8d1b4e7_render_note.py`。

**两条口径提醒**：① `ft` 的 −9.21 % 是**整片实跑**（完整 `_render_picture`，14 块 + concat），
2 worker 的 1.09–1.23× 是**成对交错**测的，整片会因为尾部效应略低，而且「2 worker 的整片导出」
仍然没有真机跑过。② 第 127 轮那个 **1.438× 作废**——它是 ft=4 时代的形状，抬 ft 已经吃掉了
一部分；`5.52 GB` 同样作废（真实值 2.84 GB）。


**验证规模（2026-11-18，第 128 轮收尾）**：后端 **1401 用例通过 / 2 skipped**，`mypy --strict`
**104** 个源文件零错误，`ruff` 干净；前端 `tsc --noEmit` 干净、`npm test` **267 通过**、
`vite build` 成功。新增：`test_preview_transcript_reads.py` 7 条、`test_video_render.py` +1、
`timeMap.test.mjs` 10 条。

> 这张表 2026-11-05 之后四天没更新，期间一直把 M5b 记成「待做」，而它已经落地
> （`c3226fb` feat: ShowNotes and 文稿 generation）。**里程碑是自报状态，不是从代码推断的，
> 所以它只会比代码旧**——完成时顺手改一行，比事后核对便宜。
>
> 同一条教训第二次发生：视频 P5 那十几轮（11-12 → 11-13）一直只写进 `docs/video.md` 和
> `CHANGELOG.md`，这张里程碑表直到第 122 轮整理文档才补上。**验收记录属于这里，
> 上面那两份是设计与历史。**

**验证规模（2026-11-09）**：后端 651 用例通过 / 1 skipped / 3 deselected，`mypy --strict` 74 个源文件零错误，`ruff` 干净；
前端 133 条 `node --test` 用例，`tsc --noEmit` 与 `vite build` 干净；真实音频端到端跑通（17.6s 样本 + 两个 1 小时级真实播客项目）。

### 视频 P7 · 导出面板（2026-11-20，第 135 轮）

设计与实测在 [`docs/video.md`](video.md) §19.34.7（停止的实现与集成实测）、
[`docs/frontend.md`](frontend.md) §7.7（两个面板的形状与四条终态）；决策 D24。

| 交付物 | 落在哪 | 验收 |
|---|---|---|
| 视频导出面板 | `apps/web/src/VideoExportDialog.tsx` + `exportPanels.ts`（纯逻辑） | 文件名（默认「期号+本地日期」）、导出到（可手填可「选择…」）、2 个勾选框、进度条；页脚一个按钮「导出 ⇄ 停止」 |
| 音频导出面板 | `apps/web/src/AudioExportDialog.tsx` | 文件名 + 格式下拉**同一行** + 导出到；**没有进度条、没有取消** |
| 停止是真的停 | `media/video.py::_run`（`Popen` + 200 ms 轮询 + `tempfile` 收 stderr）；`services/video_render.py` 的取消登记表与四处检查点 | 真 API：取消 → 终态 **0.4 s**，`cancelled`（**不是** `failed`），取消前 2 个 ffmpeg → 取消后 **0** 个；库里残留 `queued/running` **0** 条 |
| 交付到用户选的路径 | `POST /api/renders/{id}/save-to` + `resolve_export_target` | 真落盘：`成片.m4a`，89 653 243 B 与回报一致；用户打「成片.wav」得到 `成片.m4a`（**不是** `成片.wav.m4a`） |
| 选文件夹 | `POST /api/system/pick-folder`（`tkinter.askdirectory`，`-topmost`） | 无 GUI / 取消返回 409 + 可执行的 hint，输入框仍可手打 |
| 菜单瘦身 | `apps/web/src/App.tsx` | 9 项 → **7 项**；`格式：M4A/WAV` 与「视频导出：加速 / 编码快一点」四项移除，全部进面板 |

**验收实测**（走查脚本，不是目视）：

| 脚本 | 判据 | 结果 |
|---|---|---|
| `scripts/check-r135-export-dialogs.mjs` | 29 条：菜单少 4 项、视频面板 2 文本框 + 2 勾选 + 无进度条、音频面板格式下拉 2 项 + 文件名同排 + 无取消 | **29/29** |
| `scripts/check-r135-export-running.mjs` | 13 条：点导出 → 按钮变「停止」+ 进度条出现 + 输入框锁；点停止 → 「正在停止…」；终态「已停止」**黄色**、按钮回「导出」、输入框解锁 | **13/13** |

**验证规模**：后端 **1490 passed / 2 skipped**（`-n 1`），`mypy --strict` **111** 文件零错误，`ruff`
干净；前端 `tsc --noEmit` 干净、`npm test` **335 通过**、`vite build` 成功。
新增测试：`test_video_cancel.py` **12 条**（真 `Popen` 量取消 / 区分超时 / stderr 仍可读 / 后缀三条
规则 / 终态互斥）、`exportPanels.test.mjs` **20 条**（按钮文字、四种终态、本地日期、交付文案）。

**两条口径**：① 整片的 2 worker 导出仍然没有真机跑过（内存 1.4 GB 那台起不来）；
② 「取消后 0 个 ffmpeg」量的是 `tasklist`，不是 `Popen` 的返回值——返回值只能证明调用过 `kill()`。

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

