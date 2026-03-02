# nanobot

## Feishu single-card streaming configuration

Feishu channel now supports reusing one interactive card for progress updates (`metadata._progress == true`) and final response updates.

Add these fields under `channels.feishu`:

```yaml
channels:
  feishu:
    enabled: true
    app_id: "cli_xxx"
    app_secret: "xxx"

    # Single-card streaming
    stream_card_enabled: true
    stream_card_use_cardkit: true
    stream_card_min_update_ms: 300
    stream_card_ttl_seconds: 600
```

Notes:
- `stream_card_enabled`: master switch for single-card progress updates.
- `stream_card_use_cardkit`: prefer CardKit APIs (`id_convert` + card update). If unavailable or update fails, it falls back to `im.v1.message.update`.
- `stream_card_min_update_ms`: throttle interval between progress updates for the same interaction.
- `stream_card_ttl_seconds`: TTL for stale stream states to avoid unbounded in-memory growth.
