# Remove Workflow Modes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the `/plan` and `/build` workflow-mode system so the assistant always runs through the normal tool-calling loop.

**Architecture:** Delete the session-level `workflow_mode` gate and treat tool access as a normal runtime concern handled by existing exposure logic and execution-time authz. Keep Feishu intent shaping and write confirmation, but remove the separate analysis-vs-build mode split.

**Tech Stack:** Python 3.11, pytest, Typer command handling inside `AgentLoop`, tool exposure logic in `ToolRegistry`.

---

### Task 1: Remove command and status/help references

**Files:**
- Modify: `nanobot/agent/loop.py`
- Test: `tests/test_message_tool_suppress.py`

**Step 1: Write the failing test**

Add/adjust tests so that:

- `/help` no longer lists `/plan` or `/build`
- `/status` no longer renders a workflow mode line

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py -k "commands_show_short_descriptions or status" -q`

Expected: FAIL because help/status still mention workflow mode.

**Step 3: Write minimal implementation**

- Remove `/plan` and `/build` command handling from `AgentLoop._process_message()`
- Remove workflow-mode line from `_build_status_text()`
- Remove workflow-mode entries from `_build_commands_help_text()`
- Delete now-unused workflow-mode helper methods if nothing references them anymore

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py -k "commands_show_short_descriptions or status" -q`

Expected: PASS

### Task 2: Remove workflow-mode tool gating

**Files:**
- Modify: `nanobot/agent/loop.py`
- Modify: `nanobot/agent/tools/registry.py`
- Test: `tests/test_message_tool_suppress.py`

**Step 1: Write the failing test**

Add/adjust a regression asserting that legacy `session.metadata["workflow_mode"] = "plan"` no longer hides tools from the provider.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py -k workflow_mode -q`

Expected: FAIL because plan metadata still suppresses tools.

**Step 3: Write minimal implementation**

- Stop deriving `workflow_plan` from session metadata in `_tool_exposure_context_for_message()`
- Remove `workflow_plan` special handling from `ToolRegistry._allowed_tool_names()` and `ToolRegistry.get_definitions()`
- Ensure stale workflow metadata is simply ignored

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py -k workflow_mode -q`

Expected: PASS

### Task 3: Remove `/skill` blocking by workflow mode

**Files:**
- Modify: `nanobot/agent/loop.py`
- Test: `tests/test_message_tool_suppress.py`

**Step 1: Write the failing test**

Adjust the legacy `/skill` test so plan-mode metadata no longer blocks skill execution.

**Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py -k skill_and_legacy_workflow -q`

Expected: FAIL because `/skill` is still blocked by plan mode.

**Step 3: Write minimal implementation**

- Remove the plan-mode branch around `/skill`
- Keep existing skillspec execution behavior otherwise unchanged

**Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py -k skill_and_legacy_workflow -q`

Expected: PASS

### Task 4: Final verification

**Files:**
- Modify: `nanobot/agent/loop.py`
- Modify: `nanobot/agent/tools/registry.py`
- Test: `tests/test_message_tool_suppress.py`

**Step 1: Run focused verification**

Run: `python3.11 -m pytest tests/test_message_tool_suppress.py -q`

Expected: PASS

**Step 2: Run broader regression verification**

Run: `python3.11 -m pytest tests/test_skillspec_loop_integration.py tests/test_feishu_tool_registration.py tests/test_generic_write_confirmation.py tests/test_conversation_coordinators.py -q`

Expected: PASS

**Step 3: Run full suite if the focused regressions pass**

Run: `python3.11 -m pytest`

Expected: PASS
