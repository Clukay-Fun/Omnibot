---
name: tool_usage
description: Use when handling first-turn Feishu business queries about cases, contracts, project IDs, case numbers, lawyers, companies, progress, details, or workload that should be answered from Bitable data instead of memory.
always: true
---

# Tool Usage

## Core Rule

- For first-turn business lookup requests, call a read tool before asking a clarifying question whenever the user already gave a usable anchor.
- Default read tool is `bitable_search`.
- Do not answer from memory when the user is clearly asking for live business data.
- Do not say "我先去查" or "我先帮你检索" unless the same turn already contains a real tool call.
- For identifier-first queries, the next assistant action should be a `bitable_search` call in the same turn, not a natural-language preamble.

## Treat These As Search Anchors

- Company or client names: `深圳建工`, `XX公司`, `乙方`, `委托人`
- Case identifiers: `（2026）粤0306民初4426号`, other `案号`, court-style strings
- Project identifiers: `JFTD0005`, other project or matter IDs
- Contract identifiers: `HT-001`, `合同编号`, `合同ID`
- People: `张律师`, `王律师`, other lawyer or assignee names

## Query Triggers

If the message contains any of these, search first:

- `案子`, `案件`, `合同`, `任务`, `详情`, `进展`, `状态`, `最新情况`
- `那个事情`, `那个案子`, `手上有哪些`, `有哪些案件和任务`

## Default Behavior

- `案号` / `项目ID` / `合同编号` given directly: call `bitable_search` immediately.
- Company + `案子/事情/进展`: call `bitable_search` immediately; do not first ask which company variant unless search results force disambiguation.
- Lawyer + `案件和任务`: search first, usually against both案件表 and任务表.
- `详情` or `进展`: search first, summarize after results.
- When you already have one concrete anchor, pass it as `keyword` instead of leaving tool arguments empty.

## Tool Routing

- Use `bitable_search` for case, contract, task, weekly-plan, and cross-table business lookups.
- Use `table_ids` for cross-table workload queries when the user asks about multiple business object types in one turn.
- Use `bitable_search_person` only when the user is explicitly asking to resolve a Feishu person identity itself, not when they are asking for that person's cases or tasks.

## Minimal Call Shape

- Company query -> `bitable_search({"keyword": "深圳建工"})`
- Case-number query -> `bitable_search({"keyword": "（2026）粤0306民初4426号"})`
- Project-ID query -> `bitable_search({"keyword": "JFTD0005"})`
- Lawyer workload query -> `bitable_search({"keyword": "张律师"})`

If you do not yet know better filters, use the raw anchor as `keyword`. Do not call `bitable_search({})` for these first-turn lookups.

## Do Not Stall On These

- Do not ask whether `（2026）粤0306民初4426号` is a case number; treat it as a case lookup key.
- Do not ask which system `JFTD0005` belongs to before trying the core tables.
- Do not ask whether the user wants summary or detail before the first search.
- Do not ask for full lawyer identity before the first workload search unless the name is obviously unusable.
- Do not send a pure text reply for bare `案号` or bare `项目ID`; those should trigger a same-turn `bitable_search` call.

## Target Examples

- `找一下深圳建工的案子` -> call `bitable_search({"keyword": "深圳建工"})`
- `XX公司那个事情进展怎样了` -> call `bitable_search({"keyword": "XX公司"})`
- `（2026）粤0306民初4426号` -> call `bitable_search({"keyword": "（2026）粤0306民初4426号"})`
- `JFTD0005 的详情` -> call `bitable_search({"keyword": "JFTD0005"})`
- `张律师手上有哪些案件和任务` -> call `bitable_search({"keyword": "张律师"})`
