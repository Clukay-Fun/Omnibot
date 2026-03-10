# OpenClaw-Style De-Limiting Design

**Status:** approved for implementation

**Goal:** Remove the pre-LLM intention stack that currently guesses user intent, crops tools, and routes around the main model, so the assistant behaves more like OpenClaw: a small deterministic entry layer followed by one main LLM loop with broad tool visibility.

## Problem

The current runtime still layers multiple decision systems in front of the main model:

- command/mode handling
- coordinator routing
- skillspec routing and continuation
- Feishu tool exposure cropping based on heuristics
- ad hoc query inheritance helpers

This creates the exact failure mode seen in Feishu:

- the model promises to query a table
- the runtime has already hidden the relevant tools or diverted the turn
- the model replies in natural language instead of issuing a tool call

## Decision

Move toward an OpenClaw-style structure:

```text
message -> small deterministic interceptors -> main LLM with broad tools -> execute
```

Key decisions:

1. Delete Skillspec as a runtime execution layer.
2. Stop using registry heuristics to crop main-session tools by guessed intent.
3. Keep only deterministic pre-LLM interceptors:
   - slash commands that still matter
   - write confirmation callbacks
   - pagination continuation
   - explicit preference updates
4. Keep write safety in execution-time guards, not pre-LLM gating.

## Important Scope Correction

`nanobot/agent/skill_runtime/` cannot be deleted in one shot because the directory currently mixes:

- Skillspec-specific runtime pieces
- still-needed reminder/table/document infrastructure

So the deletion must happen in two stages:

### Stage A: Remove Skillspec-specific runtime

- `registry.py`
- `executor.py`
- `matcher.py`
- `embedding_router.py`
- `param_parser.py`
- `spec_schema.py`
- loop `/skill` and continuation entry points
- prompt-side blueprint loading from Skillspec registry

### Stage B: Re-home keepers, then delete the directory shell

Move these out before deleting the remainder:

- `table_registry.py`
- `table_profile_cache.py`
- `table_profile_synthesizer.py`
- `reminder_runtime.py`
- `bitable_reminder_engine.py`
- `document_extractor.py`
- `document_classifier.py`
- `document_pipeline.py`
- `mineru_client.py`
- `user_memory.py` if still needed independently

## Target Runtime

```text
message enters
-> deterministic interceptors only
-> build context + full/broad tool list
-> one main LLM call
-> execute tools
-> output guard / pagination / card rendering
```

## What Stays Deterministic

- write confirm/cancel callbacks
- pagination continuation (`继续` / `展开`)
- explicit preference commands like “叫我XX”
- onboarding bootstrap detection
- slash commands that still have product value

## What Gets Deleted or Greatly Reduced

- skillspec runtime routing
- `/skill` as a runtime entry path
- skillspec continuation path
- skillspec LLM rewrite path
- most Feishu intent-based tool cropping in `ToolRegistry`
- query-specific pre-LLM overfitting beyond minimal context helpers

## Tool Exposure Direction

- Main Feishu chat should broadly expose read tools by default.
- Write tools can remain visible or semi-visible, but write execution stays guarded by confirmation.
- The registry should stop trying to decide whether the user “really means” bitable/calendar/task/doc access.

## Prompt Direction

- `context.py` remains the main system-prompt builder.
- `runtime_texts.py` remains the home for static phrasing and lightweight trigger lists.
- Business capability hints should come from direct static prompt content or a simpler config source, not a Skillspec execution engine.

## Success Criteria

- Feishu turns like “查看团队周工作计划表所有内容” go straight to the main LLM with table tools visible.
- Follow-ups like “概览 / 全量 / 最近20条 / 需要 / 好的 / 继续” are handled either by history-aware LLM behavior or a tiny deterministic pending-query layer, not by hard routing stacks.
- `/skill` and Skillspec runtime are gone.
- `nanobot/agent/skill_runtime/` no longer contains any Skillspec-only machinery.
- Full test suite remains green.
