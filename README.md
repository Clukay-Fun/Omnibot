# nanobot

## Feishu single-card streaming configuration (Card 2.0 default)

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
- Runtime should prefer workspace overrides under `workspace/skillspec/` when the same skillspec `id` exists, and fall back to built-in assets otherwise.
