# 接口、测试、错误处理、安全、成本

> 读这份回答：**接口该长什么样、错误怎么报、测试怎么分层、钱和密钥怎么管。**
> 加接口、改错误码、写测试前读。§10 §11 里有几处「设计如此、实际未做」，都标了现状。
>
> 章节号 `§N` 全局唯一，源码注释里的 `AGENTS.md §N` 指的就是这里。

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
POST   /api/versions/{vid}/hide                        隐去文字（不动音频）
       body: {token_start:int, token_end:int, hidden:bool=true}
       一个端点两个方向：`hidden:false` 是恢复，省得两个端点各存一半状态
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

> **实际状态（2026-11-09）**：`tests/fixtures/` 里只有一个 `audacity_full_labels.txt`（人工标注参照），
> 三份音频与 token 快照都没做。回归目前靠 651 条单元测试加真实项目手动走查。
> 这在算法改动上是真风险：没有快照 diff，一次「看起来更合理」的改动不会有人挡住。
> 要补就优先补一条——**10 分钟的 token 快照**，比三份音频都值。

---
## 10. 错误处理与可恢复性

| 场景 | 策略 |
|---|---|
| getPolicy 凭证过期（300s） | 重新获取，最多 3 次；大文件先探测上传耗时 |
| OSS 上传中断 | httpx 分块 + 断点（v1 简化为整包重传，>200MB 提示） |
| 识别任务超时 | 45min 上限；超时可"用原 task_id 继续查"，不重复提交（避免重复计费） |
| transcription_url 过期（24h） | 打开旧项目时若 raw.json 缺失 → 明确提示"结果已过期，需重新识别" |
| 进程崩溃 | **现状**：启动时只把上一个进程遗留的 render 标为 failed（`renders_service.fail_orphaned`）——它的线程随进程死了，不标 UI 会永远轮询。`ops.jsonl` 重放**没做**：撤销完全走 `edit_op` 表，DB 是唯一真相，因此也不需要日志重放。 |
| 磁盘满 | 渲染前预估输出大小（码率 × 时长），不足则拒绝并给数字 |
| 素材被移动/删除 | 启动时校验 sha256，缺失则项目置只读并提示重新关联 |

---

## 11. 安全与合规

- API Key **目前就是 `.env` 里的 `DASHSCOPE_API_KEY`**（`Settings` 用 `validation_alias` 读）。原设计存 OS 凭据库（Windows Credential Manager / keyring）**未实现**，`pyproject.toml` 里也没有 `keyring` 依赖。「Key 永不下发前端」这条是执行了的。
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

