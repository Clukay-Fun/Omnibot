---
name: feishu-ocr
description: Deprecated compatibility alias for Feishu OCR. Prefer feishu-workspace and its perception.ocr capability.
metadata: {"nanobot":{"emoji":"🧾","deprecated":true,"replacement":"feishu-workspace","capability":"perception.ocr"}} 
---

# Feishu OCR Compatibility Shim

这是旧入口兼容壳。默认不要再主推这个 skill；优先使用 `feishu-workspace` 的 `perception.ocr` capability。

如果用户显式提到 `feishu-ocr`，把它当成 `feishu-workspace -> perception.ocr` 的别名处理。

读取路径：

- `{baseDir}/../feishu-workspace/SKILL.md`
- `{baseDir}/../feishu-workspace/perception/ocr/CAPABILITY.md`

核心边界不变：

- 当前消息没有图片时，停止并请用户重发图片。
- 多张图片只处理第一张。
- 不要编造模糊或不可见文字。
- 不要使用外部 OCR 工具或联网核验企业真伪。
