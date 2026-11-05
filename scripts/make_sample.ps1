# 生成 samples/speech.wav —— 中文 TTS 测试音频
#
# 必须保存为 UTF-8 with BOM：PowerShell 5.1 按 ANSI 读取无 BOM 的脚本，
# 会把中文变成乱码，TTS 便逐字念出字符名称（"Å, ¾, 版权符号"），
# 生成出一段听起来像识别失败的诡异音频。

Add-Type -AssemblyName System.Speech

$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "samples\speech.wav"

$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.SetOutputToWaveFile($out)
$synth.Rate = 0
$synth.Speak("今天我们来聊一聊播客剪辑。首先介绍一下嘉宾张三，他在百炼平台负责语音识别。")
$synth.Speak("第二段，我们讨论一下字幕校正的流程，包括句级标签和字级标签。")
$synth.Dispose()

Write-Output "written: $out"
