---
name: feishu-weekly-report
description: Generate a structured weekly report document from one existing Feishu Bitable and grant the current Feishu requester edit access. Use for weekly report generation, Bitable-to-doc summaries, and controlled docx report output.
metadata: {"nanobot":{"emoji":"🗂️"}}
---

# Feishu Weekly Report

在用户要“根据一张飞书多维表格生成本周周报文档”时使用这个 skill。

它是单用途 skill：只处理 **一张既有 Bitable**，生成 **一份结构化 docx 周报**，并给 **当前飞书发起人** 加 `edit` 权限。

不要把它扩成通用多维表格自动化或通用文档编辑器。

## 触发条件

- 用户明确要从飞书多维表格生成周报。
- 用户给了 Bitable 链接，或给了可解析的 `app_token / table_id`。
- 用户希望最终产出飞书文档，而不是只要一段聊天摘要。

## 开始前必做检查

1. 先确认当前请求来自飞书，或至少能拿到可靠的飞书资源标识。
2. 先读取 `{baseDir}/../feishu-workspace/references/bitable.md` 和 `{baseDir}/../feishu-workspace/references/docs.md` 中与你当前步骤相关的部分。
3. 先运行 `feishu-workspace` 的 `bitable check` / `docs check` 或等价最小读取，确认当前应用能访问表格和文档能力。
4. 如果当前会话来自飞书，优先查看运行时上下文里的 `Feishu User Open ID`；后续授权时优先复用它，不要重复向用户索要自己的 `open_id`。

## 输入契约

- v1 每次只处理一张表。
- 用户可以提供：
  - Bitable 链接
  - `app_token + table_id`
  - 能解析出 `app_token / table_id / view_id` 的链接
- 默认统计 **当前自然周**，按当前会话时区取值。
- 如果用户显式给出开始/结束日期，就覆盖默认周范围。
- 如果资源标识不足，不要猜，先追问资源。

## 工作流程

1. 解析 Bitable 标识，只锁定一张表。
2. 读取字段列表、字段类型和样例记录，建立字段候选。
3. 识别关键字段映射，至少要确定：
   - 日期字段
   - 标题/事项字段
   - 状态或进展字段（如果存在）
   - 风险/阻塞字段（如果存在）
   - 下周计划或待办字段（如果存在）
4. 当有多个日期/时间字段时，优先选择语义更接近“更新 / 修改 / 完成”的字段，而不是“创建时间”。
5. 字段名是拼音、缩写或语义不清时，可以追问；**追问上限 2 轮**。
6. 两轮后仍不明确，就停止猜测，直接要求用户显式确认关键字段映射；至少必须确认日期字段。
7. 字段确认后，按时间范围筛记录，生成结构化周报草稿。
8. 用 `feishu-workspace`：
   - `doc create` 新建文档
   - `doc create_blocks` 以受控模板写入根级 blocks
   - `permission member create` 给当前发起人授 `edit`
9. 返回文档标题、链接、统计区间、字段映射和授权结果。

## 中断与续接

如果中途被打断，恢复时先简短重述：

- 已确认的字段映射
- 当前统计时间范围
- 仍待确认的问题

不要重新从头盘问整张表。

## 周报固定结构

最终文档固定包含这 5 段：

1. `本周概览`
2. `重点进展`
3. `风险/阻塞`
4. `下周计划`
5. `附录/原始条目摘要`

生成 blocks 前，先读取 `{baseDir}/references/report-template.md`，按该模板组织标题、段落、列表和引用。

## 文档写入边界

- 只使用 `feishu-workspace` 的受控 `doc create_blocks`。
- 只允许追加平铺根级 blocks。
- 只使用基础 block：
  - 标题
  - 文本段落
  - 无序列表
  - 有序列表
  - 引用
- 不要尝试图片、表格、嵌套块、任意位置插入、整文替换。

## 授权规则

- 创建文档后，自动给当前飞书发起人授权。
- v1 固定使用 `edit`。
- 优先使用运行时上下文里的 `Feishu User Open ID` 作为 `member_id`。
- 如果拿不到可靠 `open_id`，明确告诉用户“文档已生成，但无法自动授权当前发起人”，不要编造授权成功。
- 授权失败时不回滚文档创建。

## 最终回复要求

最终返回必须包含：

- 文档标题
- 文档链接
- 统计时间范围
- 识别到的关键字段映射
- 是否已完成授权

如果失败，要明确指出失败点，例如：

- 缺少可靠日期字段
- 无法访问目标表
- 文档创建失败
- 授权失败但文档已生成

## 不要做的事

- 不要一次处理多张表。
- 不要在字段不明确时长时间猜测。
- 不要把 `create_blocks` 当成通用 doc 编辑器。
- 不要在用户没确认关键字段时编造周报。
- 不要自动分享给“比尔”或其他第三方。
- 不要申请或使用高于 `edit` 的授权级别。
