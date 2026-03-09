# Feishu Core Table Adaptation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add dual-path Feishu person resolution and schema-driven adaptation for three core tables in one Bitable app so the Feishu bot can choose tables, resolve people, and prepare writes much more like an internal Feishu AI.

**Architecture:** Reuse the current coordinator-first runtime, Feishu data client, and write-preview flow, but add a new Feishu member resolver, a schema-to-profile cache, and three-table-specific routing/slot-filling logic. Keep writes guarded by existing `dry_run + confirm` and resource-scoped authz.

**Tech Stack:** Python 3.11, pytest, Feishu OpenAPI, `AgentLoop`, `ToolRegistry`, `FeishuDataClient`, `TableRegistry`, loguru.

---

### Task 1: Add dual-path Feishu person resolution

**Files:**
- Modify: `nanobot/agent/tools/feishu_data/endpoints.py`
- Modify: `nanobot/agent/tools/feishu_data/person_resolver.py`
- Modify: `nanobot/agent/tools/feishu_data/registry.py`
- Modify: `nanobot/agent/tools/feishu_data/bitable_write.py`
- Modify: `nanobot/agent/tools/feishu_data/directory.py`
- Test: `tests/test_feishu_person_resolver.py`
- Test: `tests/test_feishu_write_tools.py`

**Step 1: Write the failing tests**

- Add a test that resolving a person name uses app-level member lookup first.
- Add a test that app-level permission failure falls back to user OAuth lookup.
- Add a test that multiple matches returns a disambiguation result instead of a silent write.
- Add a write-tool test showing a person field can be normalized without `workspace/feishu/bitable_rules.yaml -> directory`.

**Step 2: Run tests to verify they fail**

Run: `python3.11 -m pytest tests/test_feishu_person_resolver.py tests/test_feishu_write_tools.py -q`

**Step 3: Write minimal implementation**

- Add Feishu member-search endpoints.
- Extend `person_resolver.py` to support:
  - direct `open_id`
  - self aliases
  - app-level lookup
  - OAuth fallback lookup
  - multiple-match response
- Wire `user_token_manager` through `build_feishu_data_tools(...)` into the resolver call sites.
- Update write-field normalization to consume the new resolver result shape safely.

**Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_feishu_person_resolver.py tests/test_feishu_write_tools.py -q`

### Task 2: Add schema-driven core-table profile cache

**Files:**
- Modify: `nanobot/agent/skill_runtime/table_registry.py`
- Modify: `nanobot/agent/tools/feishu_data/bitable.py`
- Modify: `nanobot/agent/loop.py`
- Create: `nanobot/agent/skill_runtime/table_profile_cache.py`
- Test: `tests/test_table_registry.py`
- Test: `tests/test_feishu_write_tools.py`

**Step 1: Write the failing tests**

- Add a test that a core-table schema can produce a cached profile keyed by `schema_hash`.
- Add a test that a schema change invalidates the cached profile.
- Add a test that the profile exposes aliases and field-role guesses for one of the core tables.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_table_registry.py tests/test_feishu_write_tools.py -q`

**Step 3: Write minimal implementation**

- Add a small workspace-backed cache for generated profiles.
- Use existing field list payloads as the profile source.
- Keep profile generation deterministic and compact.
- Limit the first version to the three named core tables in the shared app.

**Step 4: Run tests to verify it passes**

Run: `python3.11 -m pytest tests/test_table_registry.py tests/test_feishu_write_tools.py -q`

### Task 3: Add three-table table-choice routing

**Files:**
- Modify: `nanobot/agent/tools/registry.py`
- Modify: `nanobot/agent/tools/feishu_data/bitable.py`
- Modify: `nanobot/agent/coordinators/result_selection.py`
- Test: `tests/test_feishu_tool_registration.py`
- Test: `tests/test_conversation_coordinators.py`

**Step 1: Write the failing tests**

- Add a test that案件-related phrasing prefers `案件项目总库`.
- Add a test that合同-related phrasing prefers `合同管理`.
- Add a test that周计划 phrasing prefers `团队周工作计划表`.
- Add a low-confidence test that returns a small candidate list instead of hard-selecting the wrong table.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_feishu_tool_registration.py tests/test_conversation_coordinators.py -q`

**Step 3: Write minimal implementation**

- Use core-table profile aliases and purpose hints to rerank matches.
- Keep the candidate-confirmation flow when confidence is low.
- Do not expand to broad all-table heuristics in this task.

**Step 4: Run tests to verify it passes**

Run: `python3.11 -m pytest tests/test_feishu_tool_registration.py tests/test_conversation_coordinators.py -q`

### Task 4: Add three-table record resolution and slot filling

**Files:**
- Modify: `nanobot/agent/tools/feishu_data/bitable.py`
- Modify: `nanobot/agent/tools/feishu_data/bitable_write.py`
- Modify: `nanobot/agent/skill_runtime/executor.py`
- Test: `tests/test_feishu_write_tools.py`
- Test: `tests/test_skillspec_executor.py`

**Step 1: Write the failing tests**

- Add a test that周计划 phrasing can infer `姓名 + 周次` as the record identity.
- Add a test that合同 phrasing can infer `合同编号` or `合同名称` for a lookup before update.
- Add a test that案件 phrasing can infer `案号` or `项目ID` for a lookup before update.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_feishu_write_tools.py tests/test_skillspec_executor.py -q`

**Step 3: Write minimal implementation**

- Teach the write-prep path to consult the selected profile.
- Infer record identity fields before producing a write preview.
- Ask for missing identity data instead of guessing dangerously.

**Step 4: Run tests to verify it passes**

Run: `python3.11 -m pytest tests/test_feishu_write_tools.py tests/test_skillspec_executor.py -q`

### Task 5: Focused regressions and live checklist update

**Files:**
- Modify: `docs/plans/2026-03-09-launch-readiness-hardening.md`
- Test: `tests/test_feishu_person_resolver.py`
- Test: `tests/test_feishu_tool_registration.py`
- Test: `tests/test_feishu_write_tools.py`
- Test: `tests/test_conversation_coordinators.py`

**Step 1: Run the focused automated suite**

Run: `python3.11 -m pytest tests/test_feishu_person_resolver.py tests/test_feishu_tool_registration.py tests/test_feishu_write_tools.py tests/test_conversation_coordinators.py tests/test_skillspec_executor.py -q`

**Step 2: Update the manual live checklist**

- Add checks for:
  - app-level person resolution
  - OAuth fallback person resolution
  - core-table selection accuracy for案件 / 合同 / 周计划
  - write preview quality for周计划 upsert

**Step 3: Commit the slice**

Use small commits after each task rather than one large commit.
