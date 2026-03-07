# Feishu Production Operations

This runbook captures P0 production setup, smoke checks, rollout/rollback, and monitoring baselines for the multi-user Feishu deployment.

## 1) Production Config (P0)

- OAuth ingress and whitelist
  - Set `integrations.feishu.oauth.publicBaseUrl` to your production HTTPS domain.
  - Keep `integrations.feishu.oauth.enforceHttpsPublicBaseUrl=true`.
  - Add allowed hosts in `integrations.feishu.oauth.allowedRedirectDomains`.
- Secrets via environment variables
  - Inject `app_id/app_secret/encrypt_key/verification_token` via env vars.
  - Do not commit real credentials to `config.json`.
- Feishu OAuth scope minimization
  - Start with minimum scopes used by enabled tools only.
  - Recommended split:
    - Bitable-first: only bitable/document scopes.
    - Calendar sync: add calendar scopes when `calendar_enabled=true`.
    - Task sync: add task scopes when `task_enabled=true`.
    - History tool: add IM history scopes when `message_history_enabled=true`.
- SQLite production tuning
  - `integrations.feishu.storage.stateDbPath`
  - `integrations.feishu.storage.sqliteJournalMode` (default `WAL`)
  - `integrations.feishu.storage.sqliteSynchronous` (default `NORMAL`)
  - `integrations.feishu.storage.sqliteBusyTimeoutMs`
  - Backup policy fields:
    - `sqliteBackupDir`
    - `sqliteBackupIntervalHours`
    - `sqliteBackupRetentionDays`
  - Backup executor script: `scripts/ops/sqlite_backup_rotate.py`
- Retention and compliance
  - `channels.feishu.auditEventRetentionDays`
  - `channels.feishu.auditMessageIndexRetentionDays`
  - `channels.feishu.auditCleanupIntervalSeconds`
- Memory writeback policy
  - `channels.feishu.memoryFlushThresholdPrivate`
  - `channels.feishu.memoryFlushThresholdGroup`
  - `channels.feishu.memoryForceFlushOnTopicEnd`
  - `channels.feishu.memoryTopicEndKeywords`
- Feature flags for optional tools
  - `tools.feishuData.featureFlags.calendarEnabled`
  - `tools.feishuData.featureFlags.taskEnabled`
  - `tools.feishuData.featureFlags.bitableAdminEnabled`
  - `tools.feishuData.featureFlags.messageHistoryEnabled`

## 2) Smoke Scripts (P0)

Use `scripts/ops/feishu_smoke.py` with one scenario at a time:

```bash
python3.11 scripts/ops/feishu_smoke.py oauth_smoke --actor-open-id ou_xxx --chat-id oc_xxx
python3.11 scripts/ops/feishu_smoke.py bitable_flow_smoke --fields-json '{"事项":"联调冒烟"}' --cleanup
python3.11 scripts/ops/feishu_smoke.py calendar_task_sync_smoke --cleanup
python3.11 scripts/ops/feishu_smoke.py message_history_smoke --chat-id oc_xxx --sender-open-id ou_xxx
python3.11 scripts/ops/feishu_smoke.py audit_query_smoke --chat-id oc_xxx --retention-days 1
python3.11 scripts/ops/feishu_smoke.py memory_flush_smoke --threshold 3
```

Expected result: each command prints JSON with `"ok": true`.

SQLite backup can be scheduled with cron (example every 24h):

```bash
0 3 * * * /usr/bin/python3.11 /path/to/repo/scripts/ops/sqlite_backup_rotate.py
```

## 3) Rollout and Rollback (P0)

### Three-phase rollout

1. Internal group (24h)
2. Pilot lawyer group (24h)
3. Full tenant rollout

### Release gates before next phase

- OAuth success rate >= 98%
- Tool success rate >= 97%
- P95 processing latency <= 2s
- 429 ratio <= 5%
- Error ratio stable and not rising for 10+ minutes

### Rollback priority

1. Disable optional tools with feature flags (`calendar/task/history/admin`).
2. Downgrade memory flush policy (raise thresholds, disable topic-end force flush).
3. Roll back app version.

### Data rollback point

- Create SQLite snapshot before rollout.
- On rollback failure, restore snapshot and restart gateway.

### Runtime protection

- If Feishu API 429/5xx spikes, temporarily disable non-core tools and keep Bitable main flow only.

### 30-minute executable rollback checklist

1. Freeze rollout and notify owner/oncall.
2. Flip feature flags to disable optional tools.
3. Restart gateway and verify health.
4. If still degraded, restore SQLite snapshot and restart.
5. If still degraded, revert to last stable release.
6. Confirm core path (`Bitable` + chat response) is healthy.

## 4) Monitoring and Alerts (P0/P1)

Reference threshold template: `ops/alerts/feishu-alert-thresholds.yaml`.

- Ingress
  - Event receive success rate
  - Signature verification failure rate
  - Event handling latency (P50/P95/P99)
- OAuth
  - Authorization start count
  - Callback success rate
  - Token refresh success rate
  - Auth failure reason distribution
- Tool layer
  - Success rates for Calendar/Task/Bitable/History
  - 429 ratio
  - 5xx ratio
  - Retry counts
- Memory layer
  - Writeback queue depth
  - Flush batch size
  - Flush failure rate
  - Max queue age
- Audit layer
  - Audit write throughput
  - Audit query latency
  - Cleanup task duration and deleted rows
- Business layer
  - Bitable write success rate
  - Derived sync success rate
  - Duplicate reminder ratio
  - Cross-user data contamination alerts
