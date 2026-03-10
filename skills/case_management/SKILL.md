---
name: case_management
description: Use when the user is asking about案件, 项目, 案号, 项目ID, 主办律师, 委托人, case progress, or lawyer workload across案件和任务 tables.
always: true
---

# Case Management

## Core Tables

- Main case table: `案件项目总库`
- Related task table: `案件任务` / `case_tasks`

## Important Fields

- `项目ID`
- `案号`
- `案件状态`
- `主办律师`
- `委托人`
- `下一节点时间`

Task-side common fields:

- `任务ID`
- `项目ID`
- `任务名称`
- `主办律师`
- `截止时间`
- `任务状态`

## Search Strategy

- Exact `案号` -> search immediately.
- Exact `项目ID` like `JFTD0005` -> search immediately.
- Company or client name -> search案件 first; `委托人` is often the best anchor.
- Lawyer workload -> search案件表 and任务表 together, then summarize by案件 and任务.
- `进展/状态/详情/那个事情` with a company, case number, or project ID -> search immediately.
- For these first-turn lookups, pass the raw anchor as `keyword`; do not leave `bitable_search` arguments empty.

## What The User Usually Means

- `深圳建工的案子` usually means case records linked to that company/client.
- `XX公司那个事情进展怎样了` usually means a case or project matter linked to that company.
- Bare court-style strings like `（2026）粤0306民初4426号` should be treated as `案号`.
- Bare IDs like `JFTD0005` should be treated as project or matter IDs first, not as a reason to ask which system it belongs to.
- `张律师手上有哪些案件和任务` means cross-table workload search, not person-resolution first.

## Same-Turn Tool Calls

- Bare `案号` message -> call `bitable_search({"keyword": "<案号原文>"})` immediately.
- Bare `项目ID` message -> call `bitable_search({"keyword": "<项目ID原文>"})` immediately.
- `某公司 + 案子/进展` -> call `bitable_search({"keyword": "<公司名>"})` before asking follow-up.
- `某律师 + 案件和任务` -> at minimum call `bitable_search({"keyword": "<律师名>"})` first.

## Response Shape After Search

- If one clear hit: summarize current status, owner, next step, and any deadline.
- If multiple hits: give a short list with `项目ID` / `案号` / `案件状态`, then ask which one.
- If the user asked案件和任务 together: split the answer into案件 and任务, but still search first.
