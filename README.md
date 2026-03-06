# nanobot

Feishu single-card streaming configuration (Card 2.0 default)

Feishu channel reuses one Card 2.0 interactive card for progress updates (`metadata._progress == true`) and final response updates.

Add these fields under `channels.feishu`:

```yaml
channels:
  feishu:
    enabled: true
    app_id: "cli_xxx"
    app_secret: "xxx"
    react_enabled: false
    reply_to_message: true
    reply_in_thread: false

    # Single-card streaming
    stream_card_enabled: true
    stream_card_min_update_ms: 120
    stream_card_ttl_seconds: 600
    stream_card_print_frequency_ms: 50
    stream_card_print_step: 2
    stream_card_print_strategy: fast
    stream_card_summary: ""
    stream_card_header_title: ""
    stream_card_show_thinking: true
    stream_answer_warmup_chars: 24
    stream_answer_warmup_ms: 300

    # Activation gate
    activation_private_policy: always
    activation_group_policy: mention
    activation_topic_policy: always
    activation_admin_open_ids: []
    activation_admin_prefix_bypass: "/bot"

    # Onboarding（新用户引导）
    onboarding_enabled: true
    onboarding_reentry_commands: ["/setup", "重新设置"]
    onboarding_role_options: ["律师", "助理", "实习生"]
    onboarding_team_options: ["诉讼组", "合同组", "招投标组", "综合组"]
```

Notes:

- `stream_card_enabled`: master switch for single-card progress updates.
- `react_enabled`: enable/disable emoji reaction on inbound user messages (default `false`).
- `reply_to_message`: use `im.v1.message.reply` when source `message_id` is available.
- `reply_in_thread`: default `reply_in_thread` behavior when using `im.v1.message.reply`.
  Set to `false` for in-chat direct replies; thread messages and `/session new` can still force thread replies.
- `stream_card_min_update_ms`: throttle interval between progress updates for the same interaction.
- `stream_card_ttl_seconds`: TTL for stale stream states to avoid unbounded in-memory growth.
- `stream_card_print_frequency_ms` / `stream_card_print_step` / `stream_card_print_strategy`: Card 2.0 streaming typing effect controls.
- `stream_card_summary`: Card 2.0 summary shown in chat preview.
- `stream_card_header_title`: Card 2.0 header title; empty string means no header title.
- `stream_card_show_thinking`: show/hide thinking section in the streaming card.
- `stream_answer_warmup_chars` / `stream_answer_warmup_ms`: first answer streaming trigger threshold (lower values start earlier, reducing first-screen large chunk).
- `activation_private_policy` / `activation_group_policy` / `activation_topic_policy`: inbound activation policy (`always`, `mention`, `off`). Default policy is private always, group mention, topic always.
- `activation_admin_open_ids` + `activation_admin_prefix_bypass`: optional bypass when group policy is `mention`; listed admins can trigger processing with a prefix such as `/bot`.
- `onboarding_enabled`: 是否启用飞书新用户引导流程（2 张卡片 + 1 条引导消息）。
- `onboarding_reentry_commands`: 重新触发引导的命令，默认支持 `/setup` 与 `重新设置`。
- `onboarding_role_options` / `onboarding_team_options`: 引导卡片中的职位与团队选项。
- 群聊 `mention` 门控下，`继续`/`展开` 会被视为上下文续传指令并放行。
- 推荐平衡配置：`stream_answer_warmup_chars=24`、`stream_answer_warmup_ms=300`、`stream_card_min_update_ms=120`、`stream_card_print_frequency_ms=50`、`stream_card_print_step=2`。
- Thinking section uses quoted markdown style for a lighter look; exact font-size values are controlled by Feishu client and are not configurable in this payload mode.
- Runtime path is Card 2.0 first (`id_convert` + `card_element.content` update). The same card maintains a subtle quoted thinking block and an answer block.
- Feishu Card 2.0 streaming payload does not accept custom `action` elements in this mode.
- If `stream_card_show_thinking=false`, progress messages like “正在思考中” / “思考完成” are suppressed in Feishu cards.
- For update failures, the channel falls back to `im.v1.message.update/patch`; if that also fails, it sends a new card to avoid losing output.

## Built-in commands

- `/help` 或 `/commands`：显示全部指令与简介。
- `/new`：归档并清空当前会话。
- `/stop`：停止当前会话中的进行中任务。
- `/session`：查看会话子命令。
- `/session new [标题]`：从当前消息创建飞书话题会话（thread），缺省标题为 `会话-YYYYMMDD-HHMM`。
- `/session list`：列出当前聊天下的会话（主会话 + 话题会话）。
- `/session del [id|main]`：删除当前或指定会话。

## Built-in skillspec assets

- Built-in query skillspec files are stored in `nanobot/skills/skillspec/`.
- The current built-in set includes case/task/contract query specs plus deadline overview.
- Runtime loads skillspec files with this precedence (high to low):
  - `workspace/skillspec/*.yaml`
  - `workspace/skillspec/managed/*.yaml`
  - `nanobot/skills/skillspec/*.yaml`
- `workspace/skillspec/managed/` is intended for centrally managed specs that should override bundled defaults but still be overridable by local workspace specs.
- Skillspec `response` supports deterministic rendering knobs for runtime safety:
  - `template` + `field_mapping` for query output formatting.
  - `sensitive: true` to mark group replies for private delivery to sender.
  - `confirm_required` + `confirm_respect_preference` to control write confirmation flow (default remains manual confirm).

## Document pipeline hardening

- Document pipeline failures now use explicit error categories to improve triage:
  - `[UNSUPPORTED_FORMAT]`
  - `[FILE_NOT_FOUND]`
  - `[LOW_QUALITY_EXTRACTION]`
  - `[API_TIMEOUT]`
  - `[API_ERROR]`
- Extract template precedence is deterministic and override-safe:
  - bundled defaults: `nanobot/skills/extract_templates/*.yaml`
  - optional workspace managed layer: `workspace/skillspec/extract/*.yaml`
  - workspace local override (highest priority): `workspace/extract/*.yaml`
- Document skills can opt into write-confirm bridge via `action.write_bridge`, reusing existing `确认 <token> / 取消 <token>` pending-write flow.

## Skillspec embedding router (Phase D)

Optional embedding-assisted ranking can be enabled for skillspec routing fallback. Deterministic rules should still run first.

Routing priority is fixed and deterministic: `explicit > regex > keyword > embedding`.

```yaml
agents:
  skillspec:
    embedding_enabled: false
    embedding_top_k: 3
    embedding_model: "text-embedding-3-small"
    embedding_timeout_seconds: 10
    embedding_cache_ttl_seconds: 600
    embedding_min_score: 0.15
    route_log_enabled: false
    route_log_top_k: 3
providers:
  siliconflow:
    api_key: "${SILICONFLOW_API_KEY}"
    api_base: "https://api.siliconflow.cn/v1" # optional
```

Notes:

- `embedding_enabled=false` keeps lexical-only behavior (runtime-compatible default).
- `embedding_min_score` gates low-confidence embedding routes; low-score candidates fall back to the normal LLM loop.
- If SiliconFlow embedding config is missing or provider calls fail, router falls back to lexical scoring.
- `embedding_cache_ttl_seconds` applies to both skill index vectors and recent query vectors.
- `route_log_enabled=true` adds lightweight route diagnostics (`skillspec_route`, optional top-k candidates) to message metadata and debug logs without exposing chain-of-thought to users.

## Reminder MVP

- Built-in reminder skillspec assets:
  - `reminder_set`
  - `reminder_list`
  - `reminder_cancel`
  - `daily_summary`
- Reminder data is persisted in `workspace/reminders.json` for deterministic local runtime behavior.
- Reminder bridges are best-effort and never roll back the persisted reminder record:
  - `record_bridge` writes reminder snapshots to a record table (prefer `bitable_create`)
  - `calendar_bridge` optionally creates calendar events when configured and callable
  - `summary_cron_bridge` can maintain a daily summary cron job (MVP includes add path + simple dedupe)
- Failure/status signaling:
  - primary reminder write always lands first in local store
  - bridge failures/unavailable states are returned under `bridges.*.status`
  - legacy `calendar` status from reminder runtime remains backward compatible

## Feishu table aliases and schema audit

- Built-in SkillSpec table aliases are defined in `nanobot/skills/table_registry.yaml`.
- Runtime override location is `~/.nanobot/workspace/skills/table_registry.yaml` (same alias keys, workspace value wins).
- This avoids hardcoding app/table IDs directly in skillspec files and makes table migration safer.
- Use `bitable_sync_schema` to fetch current tables + fields and persist a snapshot to `~/.nanobot/workspace/skills/table_schema_snapshot.json` for review.
- Use `bitable_list_fields` to inspect one table quickly before adjusting field aliases.

## CI and release gates

- GitHub Actions CI is defined in `.github/workflows/ci.yml` and runs lint, tests, and packaging checks.
- Tag-based release automation is defined in `.github/workflows/release.yml` (build + `twine check` + GitHub Release; optional PyPI publish when `PYPI_API_TOKEN` is configured).
- Use `RELEASE_CHECKLIST.md` before tagging/publishing to keep release flow consistent.
