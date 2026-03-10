# Remove Workflow Modes Design

**Status:** approved for implementation

**Goal:** Remove `/plan` and `/build` from the agent workflow so the main model can always see and call the tools it needs, instead of being blocked by a session mode toggle.

## Problem

The current workflow-mode layer adds a `plan/build` session state in front of the normal tool-calling loop.

- `/plan` stores `workflow_mode=plan`
- `workflow_plan` hides the entire tool menu
- `/skill` is blocked in plan mode
- help/status text teaches users to toggle modes manually

In practice this causes the exact failure pattern seen in Feishu:

- the model promises to query tables
- but the session is still in a restricted mode or shaped by that legacy path
- so it responds in natural language instead of actually calling tools

This is architectural drag from an earlier split between “analysis mode” and “execution mode”. The current product direction is the opposite: the default assistant should be free to reason and act in one loop, with safety enforced at execution time.

## Decision

Delete workflow modes entirely.

Specifically:

1. Remove `/plan` and `/build` command handling.
2. Stop reading and writing `workflow_mode` session metadata.
3. Remove `workflow_plan` tool gating from the registry.
4. Stop blocking `/skill` behind plan mode.
5. Remove workflow-mode references from help/status text and tests.

## Desired Runtime Behavior

- Normal chat always enters the main LLM loop.
- The model always receives the normal tool menu for the current channel/context.
- Feishu queries such as “列出所有表格” can directly call Bitable tools without asking the user to switch modes first.
- Safety remains in execution-time authz and write confirmation, not in an analysis/build mode toggle.

## Non-Goals

- Reworking Feishu intent routing in this change.
- Removing `/skill`, `/status`, `/help`, or `/session`.
- Changing write-confirmation behavior.

## Implementation Notes

- Keep the existing exposure-mode categories like `main_feishu_query`, `main_write_prepare`, and `main_write_commit`; those are still useful for narrowing Feishu tool choices.
- Only remove the workflow-mode branch (`plan/build`) and its special authorization path.
- Treat stale `session.metadata["workflow_mode"]` values as ignored legacy data rather than something that must be migrated.

## Success Criteria

- `/help` no longer mentions `/plan` or `/build`.
- `/status` no longer shows a workflow mode line.
- Legacy `workflow_mode=plan` session metadata does not hide tools.
- `/skill` is no longer blocked by legacy workflow-mode state.
- Focused regression tests pass.
