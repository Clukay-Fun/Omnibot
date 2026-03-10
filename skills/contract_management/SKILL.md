---
name: contract_management
description: Use when the user is asking about合同, 合同编号, 项目ID 关联合同, 乙方, contract status, expiry, renewal deadlines, or contract details that should come from the contract table.
always: true
---

# Contract Management

## Core Table

- Main contract table: `合同管理`

## Important Fields

- `合同ID`
- `项目ID`
- `合同编号`
- `合同名称`
- `乙方`
- `合同金额`
- `合同状态`
- `到期时间`
- `续签截止时间`

## Search Strategy

- Exact `合同编号` or `合同ID` -> search immediately.
- `项目ID` + 合同 -> search immediately.
- `乙方` / vendor / company name -> search immediately.
- `详情` / `进展` / `状态` / `到期` / `续签` -> search first, then summarize.

## Interpretation Rules

- If the user says `某合同详情`, default to the contract table first.
- If the user gives a company name and asks about合同, prefer `乙方` / vendor-related hits.
- If a query could be案件 or合同, use the user's wording: `案子/案件` ->案件表 first, `合同` ->合同表 first.

## Response Shape After Search

- If one clear hit: summarize `合同状态`, counterpart, amount, and key dates.
- If multiple hits: give a compact list with `合同编号` / `合同名称` / `合同状态`, then ask which one.
