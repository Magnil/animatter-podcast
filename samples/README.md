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
