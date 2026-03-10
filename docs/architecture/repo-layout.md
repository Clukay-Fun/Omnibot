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
- `nanobot/skills/skillspec/`: built-in SkillSpec definitions.

Rule of thumb: if runtime code loads it with `importlib.resources`, it belongs under `nanobot/` rather than the repository root.

## External workspace layout

The runtime workspace keeps its user-facing paths stable even though the packaged source assets are grouped by domain.

- Root persona files remain at workspace root: `AGENTS.md`, `BOOTSTRAP.md`, `HEARTBEAT.md`, `IDENTITY.md`, `MEMORY.md`, `SOUL.md`, `TOOLS.md`, `USER.md`, and `runtime_texts.yaml`.
- Runtime data stays under dedicated folders such as `memory/`, `skills/`, `skillspec/`, `extract/`, and `feishu/`.
- Workspace overrides continue to win over packaged defaults:
  - `skills/table_registry.yaml` overrides the built-in registry.
  - `extract/*.yaml` overrides built-in extraction templates.
  - `skillspec/*.yaml` and `skillspec/managed/*.yaml` override built-in SkillSpec assets by precedence.

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
