# Repository Layout

This repository keeps three related layouts aligned: the source tree in git, the packaged `nanobot` Python distribution, and the external user workspace created under `~/.nanobot`.

## Top-level conventions

- `nanobot/`: shipped Python package, including runtime code plus packaged assets that must be available after installation.
- `docs/`: repository-only documentation; never required at runtime.
- `ops/`: operational scripts and deployment assets, including Feishu smoke verification helpers.
- `tests/`: automated verification for package assets, runtime fallback behavior, and product flows.
- `config.example.json`: checked-in example config. Real runtime config lives outside the repo at `~/.nanobot/config.json`.

## Packaged asset layout

Assets that must survive packaging stay under the `nanobot` package so Hatch includes them in wheels and sdists.

- `nanobot/templates/workspace/`: default workspace bootstrap files such as `AGENTS.md`, `BOOTSTRAP.md`, `HEARTBEAT.md`, `SOUL.md`, `USER.md`, and `runtime_texts.yaml`.
- `nanobot/templates/memory/`: packaged memory template content copied into workspace memory files.
- `nanobot/templates/feishu/`: packaged Feishu-specific workspace defaults.
- `nanobot/skills/builtin/`: built-in agent skills shipped with the package.
- `nanobot/skills/extract/`: built-in document extraction templates.
- `nanobot/skills/registry/table_registry.yaml`: built-in table alias registry defaults.
- Runtime helper namespaces are now explicit and no longer live under a generic `skill_runtime/` bucket:
  - `nanobot/agent/reminders/`
  - `nanobot/agent/documents/`
  - `nanobot/agent/table_runtime/`
  - `nanobot/agent/user_state/`

### Breaking import change

The legacy package `nanobot.agent.skill_runtime` has been removed.

Callers must migrate old imports to the new canonical namespaces:

- `from nanobot.agent.skill_runtime import ReminderRuntime` -> `from nanobot.agent.reminders import ReminderRuntime`
- `from nanobot.agent.skill_runtime import BitableReminderRuleEngine` -> `from nanobot.agent.reminders import BitableReminderRuleEngine`
- `from nanobot.agent.skill_runtime import process_document` -> `from nanobot.agent.documents.document_pipeline import process_document`
- `from nanobot.agent.skill_runtime import UserMemoryStore` -> `from nanobot.agent.user_state import UserMemoryStore`

- `nanobot.agent.skill_runtime.reminder_runtime` -> `nanobot.agent.reminders.reminder_runtime`
- `nanobot.agent.skill_runtime.bitable_reminder_engine` -> `nanobot.agent.reminders.bitable_reminder_engine`
- `nanobot.agent.skill_runtime.document_extractor` -> `nanobot.agent.documents.document_extractor`
- `nanobot.agent.skill_runtime.document_classifier` -> `nanobot.agent.documents.document_classifier`
- `nanobot.agent.skill_runtime.document_pipeline` -> `nanobot.agent.documents.document_pipeline`
- `nanobot.agent.skill_runtime.mineru_client` -> `nanobot.agent.documents.mineru_client`
- `nanobot.agent.skill_runtime.table_registry` -> `nanobot.agent.table_runtime.table_registry`
- `nanobot.agent.skill_runtime.table_profile_cache` -> `nanobot.agent.table_runtime.table_profile_cache`
- `nanobot.agent.skill_runtime.table_profile_synthesizer` -> `nanobot.agent.table_runtime.table_profile_synthesizer`
- `nanobot.agent.skill_runtime.user_memory` -> `nanobot.agent.user_state.user_memory`

Rule of thumb: if runtime code loads it with `importlib.resources`, it belongs under `nanobot/` rather than the repository root.

## External workspace layout

The runtime workspace keeps its user-facing paths stable even though the packaged source assets are grouped by domain.

- Root persona files remain at workspace root: `AGENTS.md`, `BOOTSTRAP.md`, `HEARTBEAT.md`, `IDENTITY.md`, `MEMORY.md`, `SOUL.md`, `TOOLS.md`, `USER.md`, and `runtime_texts.yaml`.
- Runtime data stays under dedicated folders such as `memory/`, `skills/`, `extract/`, and `feishu/`.
- Workspace overrides continue to win over packaged defaults:
  - `skills/table_registry.yaml` overrides the built-in registry.
  - `extract/*.yaml` overrides built-in extraction templates.
  - Older workspaces may still contain `skillspec/` as a historical leftover, but runtime code does not read extract overrides from it.
  - If an old workspace still stores custom extract templates under `skillspec/extract/`, move those YAML files to `extract/`.

Compatibility rule: repository/package reorganization must not require users to move existing workspace files unless a migration is explicitly documented.

## Operational paths

- Feishu smoke verification script lives at `ops/feishu/feishu_smoke.py`.
- Maintenance scripts live under `ops/maintenance/`.
- Systemd examples stay under `ops/systemd/`.

## Reorg guardrails

When adding new assets or moving existing ones:

1. Keep runtime-loaded defaults inside `nanobot/` and ensure `pyproject.toml` includes them.
2. Preserve the external workspace contract unless a deliberate migration is added.
3. Update docs and smoke-test paths when operational scripts move.
4. Cover new layout assumptions with tests that exercise both packaged resources and workspace overrides.

## Branch baseline

- Repository/package layout changes are now developed on top of fork branch `mine/main`.
- Legacy remote branches such as `mine/ominibot`, `mine/feishu`, and `mine/feishu-runtime-hardening` are retained only as historical references.
- Branch workflow details live in `docs/guides/DEVELOPMENT_BASELINE.md`.
