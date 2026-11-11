# 生成中文测试音频（Windows SAPI，无需联网）

`speech.wav` 是 17.6 秒的中文朗读样本，用于端到端验证转写链路。

它**不入库**：属于可再生的二进制产物，仓库里只保留生成方式。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/make_sample.ps1
```

## 为什么要有这个脚本

早期手写 PowerShell 命令生成样本时踩过一个坑：**PowerShell 5.1 按 ANSI 读取脚本文件**，
脚本里的中文会变成乱码，TTS 于是把乱码当字符名称念出来，音频内容变成
「Å、¾、版权符号」之类，ASR 忠实转写后看起来像识别失败。

生成脚本必须用 **UTF-8 with BOM** 保存，否则中文输入不可信。

`tests/test_live_transcribe.py` 内部用同样的方式现场合成音频，不依赖本目录的文件。

## 本地临时素材

`test.mp3`（75 秒真实播客片段）和它的中文名副本 `测试中文.mp3` 是本地验证用的，
同样**不入库**（`.gitignore` 里忽略了 `samples/*.mp3`）。中文名副本的用途很具体：
验证文件名含中文时 `ffprobe` 那条路径不会坏（见 AGENT.md「热词真正生效」一节）。

## 视频模板 PSD

`播客模板.psd`（2560×1440 / 58 层，10.9 MB）**不入库**——它是用户自己的设计文件，
不是可以再生的测试夹具。

依赖它的两个测试会**跳过而不是失败**：

- `tests/test_video_template.py` — 真实编译（box_rect、头像位、overlay）
- `tests/test_video_template_assets.py` — 编译产物是否真的能被取到

所以 `pytest` 在没有这个文件的机器上会多两条 skip，其余全绿。要把它们跑起来，把任意
16:9 的 PSD 放到这个路径即可；模板编译的**规则**（几何找素材框、按 smartobject 找头像、
按覆盖率找底板）本身有独立的单元测试，不依赖这个文件。

实测基准（有文件时）：编译 6.3 秒，box_rect `{x:240,y:209,w:1440,h:810}`，4 个可见头像位。

