# Feishu Core Table Adaptation Design

**Status:** approved for implementation

**Goal:** Make the Feishu agent feel much closer to an internal Feishu AI for three high-frequency tables in one Bitable app: `案件项目总库`, `合同管理`, and `团队周工作计划表`.

## Scope

- Deeply adapt only three core tables in the first phase.
- Replace directory-table-dependent person resolution with dual-path Feishu member resolution.
- Build schema-driven table profiles instead of hand-maintained table docs.
- Keep write safety conservative: `dry_run + confirm`, no broad auto-delete.

## Non-Goals

- Full-library adaptation for every Bitable table.
- Sample-row ingestion in phase 1.
- Fully autonomous high-risk writes.
- Broad planner/subagent expansion beyond what is needed for the three tables.

## Architecture

### 1. Table Profile Cache

- Source of truth is live Feishu schema: table list, field list, field types, and field options.
- On first use of a target table, the runtime generates a structured table profile and caches it.
- Cache key should include `app_token + table_id + schema_hash` so schema changes invalidate old profiles automatically.

Phase-1 profile fields:

- `display_name`
- `aliases`
- `purpose_guess`
- `field_roles`
- `identity_fields_guess`
- `person_fields`
- `time_fields`
- `status_fields`
- `common_query_patterns`
- `common_write_patterns`
- `confidence`

### 2. Three-Table Intent Routing

- Route Feishu table requests toward the three core tables with high precision.
- Prefer explicit candidate confirmation only when confidence is genuinely low.
- Avoid broad, generic whole-app table matching in phase 1.

### 3. Record Resolution

- `案件项目总库`: resolve by `案号 / 项目ID / 委托人 / 主办律师`
- `合同管理`: resolve by `合同编号 / 合同名称 / 乙方`
- `团队周工作计划表`: resolve by `姓名 / 周次`

### 4. Slot Filling

- Convert user wording into target fields before write preview.
- Use profile-derived field roles to infer where values belong.
- Ask for missing fields only when the write preview cannot be formed safely.

### 5. Dual-Path Person Resolution

- `我 / 本人 / 自己` -> current sender `open_id`
- explicit `open_id` -> use directly
- name / email / phone -> Feishu member lookup
- auth order: app-level first, OAuth user-token fallback second
- multiple matches -> disambiguation required

This removes the phase-0 dependency on a workspace directory bitable.

## Runtime Boundaries

- Profiles improve routing, table choice, record matching, and slot filling.
- Profiles do not grant new write permissions.
- Resource-scoped authz remains authoritative for writes.
- Delete and other dangerous changes remain conservative.

## Phase-1 Success Criteria

- The agent reliably chooses the right table for the three core-table intents.
- The agent can resolve common person fields without a directory bitable.
- The agent can produce useful write previews without requiring users to spell out raw field names.
- Follow-up turns retain enough recent table/record context to avoid repeated restarts.

## Phase-2 Candidates

- Read a tiny number of sample rows with summarization and privacy controls.
- Expand adaptation beyond the initial three tables.
- Use profile-aware upsert strategies and stronger cross-table reasoning.
