# 外部约束：百炼 API 的硬事实与官方文档

> 读这份回答：**百炼允许什么、限制是多少、参数叫什么。**
> 这里的数字是硬事实，**不得凭记忆改**。动 ASR / 上传 / 热词相关代码前必读，改完顺手核对官方文档链接。
>
> 章节号 `§N` 全局唯一，源码注释里的 `AGENTS.md §N` 指的就是这里。

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

## 18. 参考

- 百炼录音文件识别 REST：<https://help.aliyun.com/zh/model-studio/paraformer-recorded-speech-recognition-restful-api>
- 百炼非实时语音识别用户指南（模型/限制/上下文/说话人）：<https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide>
- Qwen-Audio-ASR-Filetrans HTTP API（即时热词/context 参数）：<https://help.aliyun.com/zh/model-studio/qwen-audio-asr-filetrans-http-api>
- 提升识别准确率（热词规范、权重、配额、上下文 400 字限制）：<https://help.aliyun.com/zh/model-studio/improve-asr-accuracy>
- 定制热词 HTTP API（创建/更新/删除词表）：<https://help.aliyun.com/zh/model-studio/vocabulary-http-api>
- 上传本地文件获取临时 URL（getPolicy / 1GB / 48h / header）：<https://help.aliyun.com/zh/model-studio/get-temporary-file-url>
- 语音识别模型总览（时长、格式、语言）：<https://help.aliyun.com/zh/model-studio/asr-model>

