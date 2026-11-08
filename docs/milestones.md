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
| M6 | 稳定性：崩溃恢复、错误提示、成本面板、打包 | 断电重开不丢编辑 |

### 进度一览（2026-11-09 订正）

```
M0 ✅ 骨架          M1 ✅ 转写闭环       M2a ✅ 热词库
M2b ✅ LLM 校对     M2c ⏸  影子测试(推迟)  M3 ✅ 校正台 UI
M4 ✅ 剪辑+时间轴   M5a ✅ 字幕导出       M5b ✅ 文稿(md)
M6 ⬜ 稳定性
工作台 ✅   时间轴对齐 ✅
```

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

