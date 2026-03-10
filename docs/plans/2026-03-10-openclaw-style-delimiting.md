# OpenClaw-Style De-Limiting Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the pre-LLM restrictions that stop the model from freely calling tools, then retire the Skillspec runtime in staged batches without breaking reminders, table metadata, or document infrastructure.

**Architecture:** First broaden main-session tool exposure and detach prompt building from Skillspec blueprints. Next re-home the non-Skillspec helpers currently living under `skill_runtime/`. Finally delete Skillspec routing/execution and simplify the loop down to deterministic interceptors plus one main LLM loop.

**Tech Stack:** Python 3.11, pytest, `AgentLoop`, `ToolRegistry`, Feishu channel tools, packaged prompt assets under `nanobot/`, git-managed staged refactor.

---

## Batch 1 (Parallel-safe): De-limit the main runtime

### Task 1A: Remove main-session tool cropping in `ToolRegistry`

**Files:**
- Modify: `nanobot/agent/tools/registry.py`
- Test: `tests/test_feishu_tool_registration.py`
- Test: `tests/test_conversation_coordinators.py`

**Step 1: Write the failing test**

Add/adjust tests to assert that Feishu main chat receives broad read-tool visibility without relying on bitable/domain keyword guesses.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_feishu_tool_registration.py tests/test_conversation_coordinators.py -q`

Expected: FAIL because current registry still crops tools by guessed intent.

**Step 3: Write minimal implementation**

- Simplify `_select_feishu_tools()` so `main_chat_readonly` and `main_feishu_query` no longer depend on narrow intent routing for read tools.
- Keep execution-time authorization intact.
- Do not change write-confirmation behavior in this task.

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_feishu_tool_registration.py tests/test_conversation_coordinators.py -q`

Expected: PASS

### Task 1B: Stop prompt-side dependency on Skillspec blueprints

**Files:**
- Modify: `nanobot/agent/context.py`
- Test: `tests/test_context_prompt_cache.py`

**Step 1: Write the failing test**

Adjust prompt tests so the system prompt no longer depends on `SkillSpecRegistry.blueprints`.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_context_prompt_cache.py -q`

Expected: FAIL because prompt assembly still reads Skillspec blueprints.

**Step 3: Write minimal implementation**

- Remove `_build_business_capabilities_context()`’s dependency on `SkillSpecRegistry`.
- Replace it with either a direct static section or no business-capability section for now.

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_context_prompt_cache.py -q`

Expected: PASS

### Task 1C: Re-home table metadata helpers out of `skill_runtime`

**Files:**
- Create: `nanobot/agent/table_runtime/__init__.py`
- Create: `nanobot/agent/table_runtime/table_registry.py`
- Create: `nanobot/agent/table_runtime/table_profile_cache.py`
- Create: `nanobot/agent/table_runtime/table_profile_synthesizer.py`
- Modify: imports in `nanobot/agent/tools/feishu_data/bitable.py`
- Modify: any tests importing old paths

**Step 1: Write the failing test**

Add/adjust a test that imports the new table-runtime path and verifies Bitable table metadata still resolves.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_table_registry.py tests/test_feishu_readonly_tools.py -q`

Expected: FAIL once imports are switched before files exist.

**Step 3: Write minimal implementation**

- Move the table metadata helpers into a non-Skillspec namespace.
- Update imports only; keep behavior identical.

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_table_registry.py tests/test_feishu_readonly_tools.py -q`

Expected: PASS

## Batch 2 (Parallel-safe after Batch 1): Re-home remaining non-Skillspec helpers

### Task 2A: Re-home reminder runtime helpers

**Files:**
- Create: `nanobot/agent/reminders/`
- Move: `bitable_reminder_engine.py`
- Move: `reminder_runtime.py`
- Modify: `nanobot/channels/feishu.py`
- Test: `tests/test_bitable_reminder_engine.py`
- Test: `tests/test_heartbeat_service.py`

### Task 2B: Re-home document pipeline helpers

**Files:**
- Create: `nanobot/agent/documents/`
- Move: `document_extractor.py`
- Move: `document_classifier.py`
- Move: `document_pipeline.py`
- Move: `mineru_client.py`
- Test: `tests/test_document_extractor.py`
- Test: `tests/test_mineru_client.py`

### Task 2C: Re-home any remaining generic helper not tied to Skillspec routing

**Files:**
- Evaluate: `user_memory.py`
- Modify only if still needed outside Skillspec execution

## Batch 3 (Sequential): Remove Skillspec runtime and entry points

### Task 3A: Remove Skillspec from `AgentLoop`

**Files:**
- Modify: `nanobot/agent/loop.py`
- Modify: `nanobot/config/schema.py`
- Modify: `nanobot/cli/commands.py`
- Test: `tests/test_message_tool_suppress.py`
- Test: `tests/test_skillspec_loop_integration.py`

**Step 1: Write the failing test**

Convert legacy `/skill` and continuation tests into removal tests that assert there is no Skillspec runtime entry anymore.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py tests/test_skillspec_loop_integration.py -q`

Expected: FAIL because loop still initializes and routes through Skillspec.

**Step 3: Write minimal implementation**

- Delete Skillspec initialization from `AgentLoop`
- Remove `/skill` handling from `_process_message()`
- Remove Skillspec continuation handling
- Remove render-time rewrite logic used only by Skillspec results
- Remove now-unused config fields if no other runtime path needs them

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py tests/test_skillspec_loop_integration.py -q`

Expected: PASS

### Task 3B: Delete Skillspec runtime modules and packaged runtime assets

**Files:**
- Delete: `nanobot/agent/skill_runtime/registry.py`
- Delete: `nanobot/agent/skill_runtime/executor.py`
- Delete: `nanobot/agent/skill_runtime/matcher.py`
- Delete: `nanobot/agent/skill_runtime/embedding_router.py`
- Delete: `nanobot/agent/skill_runtime/param_parser.py`
- Delete: `nanobot/agent/skill_runtime/spec_schema.py`
- Delete: `nanobot/skills/skillspec/*.yaml`
- Modify: `pyproject.toml`
- Modify: tests/docs accordingly

**Step 1: Write the failing test**

Update tests so they no longer expect packaged Skillspec inventory or runtime registry behavior.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_skillspec_registry.py tests/test_skillspec_assets.py tests/test_skillspec_executor.py -q`

Expected: FAIL because Skillspec-specific tests still exist.

**Step 3: Write minimal implementation**

- Delete the Skillspec-only runtime code and packaged runtime assets
- Delete or replace the Skillspec-only tests with tests for surviving runtime pieces

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_skillspec_registry.py tests/test_skillspec_assets.py tests/test_skillspec_executor.py -q`

Expected: those tests are removed or replaced, and the suite no longer depends on Skillspec.

## Batch 4 (Sequential): Simplify loop and final verification

### Task 4A: Remove leftover pre-LLM overfitting from `loop.py`

**Files:**
- Modify: `nanobot/agent/loop.py`
- Modify: `nanobot/agent/runtime_texts.py`
- Test: `tests/test_conversation_coordinators.py`
- Test: `tests/test_message_tool_suppress.py`

**Step 1: Write the failing test**

Adjust loop-focused tests to reflect the final architecture: deterministic interceptors only, then one main LLM loop.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_conversation_coordinators.py tests/test_message_tool_suppress.py -q`

Expected: FAIL while old helper logic still exists.

**Step 3: Write minimal implementation**

- Remove leftover Skillspec/coordinator-specific helper logic that no longer participates in the active path
- Keep only deterministic interceptors that still matter

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_conversation_coordinators.py tests/test_message_tool_suppress.py -q`

Expected: PASS

### Task 4B: Final regression verification

**Step 1: Run focused verification**

Run: `python3.11 -m pytest tests/test_feishu_tool_registration.py tests/test_conversation_coordinators.py tests/test_context_prompt_cache.py tests/test_table_registry.py tests/test_document_extractor.py tests/test_bitable_reminder_engine.py -q`

Expected: PASS

**Step 2: Run full suite**

Run: `python3.11 -m pytest`

Expected: PASS
