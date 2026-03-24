---
name: feishu-weekly-report
description: Deprecated compatibility alias for Feishu weekly report generation. Prefer feishu-workspace and its workflows.weekly_report capability.
metadata: {"nanobot":{"emoji":"🗂️","deprecated":true,"replacement":"feishu-workspace","capability":"workflows.weekly_report"}}
---

# Feishu Weekly Report Compatibility Shim

这是旧入口兼容壳。默认不要再主推这个 skill；优先使用 `feishu-workspace` 的 `workflows.weekly_report` capability。

如果用户显式提到 `feishu-weekly-report`，把它当成 `feishu-workspace -> workflows.weekly_report` 的别名处理。

读取路径：

- `{baseDir}/../feishu-workspace/SKILL.md`
- `{baseDir}/../feishu-workspace/workflows/weekly-report/CAPABILITY.md`
- `{baseDir}/references/report-template.md`

核心边界不变：

- v1 每次只处理一张表。
- 追问字段映射上限 2 轮，之后必须要求用户明确确认。
- 文档写入只使用 `feishu-workspace` 的受控 `doc create_blocks`。
- 授权固定使用 `edit`，且不要自动分享给无关第三方。
