# Animatter Podcast Studio

把一期播客（1–3 小时、单人或多人）变成三样东西：**可校正的词级时间轴**、**按词级标签剪出来的成片音频**、**整理好的文稿与字幕**。

> 入口文档：[AGENTS.md](AGENTS.md)（铁律、命令、目录）
> 设计文档：[docs/](docs/)（架构、数据模型、算法、决策记录，按主题分册）
> 进度：[docs/milestones.md](docs/milestones.md)；待办：[todolist.md](todolist.md)

## 快速开始

```powershell
# 1. 配置密钥（转写需要；其余功能不需要）
Copy-Item .env.example .env
#    编辑 .env，填入 DASHSCOPE_API_KEY

# 2. 启动
.\scripts\dev.ps1
```

服务仅监听 `127.0.0.1:8317`。

```powershell
# 健康检查（会告诉你 ffmpeg / 数据库 / 磁盘 / 密钥 的状态）
curl http://127.0.0.1:8317/healthz

# 创建项目
curl -X POST http://127.0.0.1:8317/api/projects `
     -H "Content-Type: application/json" `
     -d '{"name":"S02E07 对话某某"}'
```

## 目录

```
apps/api/            后端（FastAPI + SQLAlchemy + Alembic）
  src/animatter/     core · services · providers · media · routers
  alembic/           数据库迁移
  tests/             单元与集成测试
apps/web/            前端（M3 开始）
data/                运行时数据，已 gitignore
scripts/             开发脚本
```

## 常用命令

| 命令 | 作用 |
|---|---|
| `.\scripts\dev.ps1` | 装依赖 → 迁移 → 带 reload 启动 |
| `.venv\Scripts\python -m animatter check` | 检查 ffmpeg / 目录 / 密钥 |
| `.venv\Scripts\python -m animatter db-upgrade` | 应用数据库迁移 |
| `.venv\Scripts\python -m pytest apps/api` | 跑测试 |
| `.venv\Scripts\python -m ruff check apps/api` | 静态检查 |
| `.venv\Scripts\python -m mypy apps/api/src/animatter` | 类型检查 |

## 环境要求

- Python ≥ 3.11（开发机为 3.13）
- ffmpeg / ffprobe ≥ 6.0 在 PATH 上（`winget install Gyan.FFmpeg`）
- 阿里百炼 API Key（北京地域），并开通「录音文件识别」

## 说明

- 所有时间戳都是**整数毫秒**，区间一律半开 `[start, end)`。
- 原始音频**只读**；剪辑是对原始媒体的一组区间引用（EDL），不是改写文件。
- 未配置 `DASHSCOPE_API_KEY` 时服务仍可启动，`/healthz` 会把该检查标为 `ok: false` 但不返回 503。
