"""Feishu bitable reminder rule engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml

from nanobot.agent.skill_runtime.reminder_runtime import ReminderRuntime
from nanobot.agent.tools.feishu_data.client import FeishuDataClient
from nanobot.agent.tools.feishu_data.person_resolver import BitablePersonResolver
from nanobot.config.schema import FeishuDataConfig
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule

PersonResolver = Callable[[Any], Awaitable[str | None]]


@dataclass(slots=True)
class ReminderRule:
    rule_id: str
    enabled: bool
    app_token: str
    table_id: str
    on_create: bool
    on_update: bool
    time_fields: list[str]
    recipient_fields: list[str]
    title_fields: list[str]
    content_fields: list[str]
    status_in: list[str]
    field_equals: dict[str, Any]
    reminders: list[int]
    dedupe_policy: str
    overwrite_existing: bool
    changed_fields_any: list[str]
    changed_fields_all: list[str]
    cancel_status_in: list[str]
    cancel_on_deleted: bool


class BitableReminderRuleEngine:
    """Evaluate multi-table bitable rules and schedule reminders."""

    def __init__(
        self,
        workspace: Path,
        *,
        reminder_runtime: ReminderRuntime,
        cron_service: CronService,
        person_resolver: PersonResolver | None = None,
        feishu_data_config: FeishuDataConfig | None = None,
    ):
        self.workspace = workspace
        self.reminder_runtime = reminder_runtime
        self.cron_service = cron_service
        self.person_resolver = person_resolver
        self.feishu_data_config = feishu_data_config or FeishuDataConfig()
        self.rules_path = workspace / "feishu" / "bitable_rules.yaml"
        self.state_path = workspace / "memory" / "feishu" / "bitable_reminder_state.json"
        self.audit_path = workspace / "memory" / "feishu" / "bitable_audit.log"
        self.schema_cache_path = workspace / "memory" / "feishu" / "bitable_schema_cache.json"
        self._directory_resolver: BitablePersonResolver | None = None

    def _read_yaml(self) -> dict[str, Any]:
        if not self.rules_path.exists():
            return {"version": 1, "tables": []}
        data = yaml.safe_load(self.rules_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {"version": 1, "tables": []}

    def _directory_config(self) -> dict[str, Any]:
        payload = self._read_yaml()
        directory = payload.get("directory")
        return dict(directory) if isinstance(directory, dict) else {}

    def _ensure_person_resolver(self) -> PersonResolver | None:
        if self.person_resolver is not None:
            return self.person_resolver
        if not self.feishu_data_config.enabled:
            return None
        directory = self._directory_config()
        if not directory.get("app_token") or not directory.get("table_id"):
            return None
        if self._directory_resolver is None:
            client = FeishuDataClient(self.feishu_data_config)
            self._directory_resolver = BitablePersonResolver(
                self.feishu_data_config,
                client=client,
                directory=directory,
            )
        return self._directory_resolver.resolve

    def load_rules(self) -> list[ReminderRule]:
        payload = self._read_yaml()
        rules: list[ReminderRule] = []
        for index, item in enumerate(payload.get("tables", []), start=1):
            if not isinstance(item, dict):
                continue
            triggers = item.get("triggers") if isinstance(item.get("triggers"), dict) else {}
            conditions = item.get("conditions") if isinstance(item.get("conditions"), dict) else {}
            when = item.get("when") if isinstance(item.get("when"), dict) else {}
            schedule = item.get("schedule") if isinstance(item.get("schedule"), dict) else {}
            cancel_on = item.get("cancel_on") if isinstance(item.get("cancel_on"), dict) else {}
            reminders = [max(0, int(value)) for value in schedule.get("reminders", []) if str(value).strip()]
            if not reminders:
                reminders = [max(0, int(item.get("remind_before_minutes", 0)))]
            rules.append(
                ReminderRule(
                    rule_id=str(item.get("id") or f"rule-{index}"),
                    enabled=bool(item.get("enabled", True)),
                    app_token=str(item.get("app_token") or "").strip(),
                    table_id=str(item.get("table_id") or "").strip(),
                    on_create=bool(triggers.get("on_create", True)),
                    on_update=bool(triggers.get("on_update", True)),
                    time_fields=[str(v) for v in item.get("time_fields", []) if str(v).strip()],
                    recipient_fields=[str(v) for v in item.get("recipient_fields", []) if str(v).strip()],
                    title_fields=[str(v) for v in item.get("title_fields", []) if str(v).strip()],
                    content_fields=[str(v) for v in item.get("content_fields", []) if str(v).strip()],
                    status_in=[str(v) for v in conditions.get("status_in", []) if str(v).strip()],
                    field_equals=dict(conditions.get("field_equals", {}) or {}),
                    reminders=reminders,
                    dedupe_policy=str(item.get("dedupe_policy") or "update").strip().lower(),
                    overwrite_existing=bool(item.get("overwrite_existing", False)),
                    changed_fields_any=[str(v) for v in when.get("changed_fields_any", []) if str(v).strip()],
                    changed_fields_all=[str(v) for v in when.get("changed_fields_all", []) if str(v).strip()],
                    cancel_status_in=[str(v) for v in cancel_on.get("status_in", []) if str(v).strip()],
                    cancel_on_deleted=bool(cancel_on.get("deleted", True)),
                )
            )
        return rules

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _append_audit(self, line: str) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(UTC).isoformat()} {line}\n")

    @staticmethod
    def _pick_first(fields: dict[str, Any], candidates: list[str]) -> Any:
        for field in candidates:
            value = fields.get(field)
            if value not in (None, "", [], {}):
                return value
        return None

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, dict):
                    out.append(str(item.get("name") or item.get("text") or item.get("id") or item.get("email") or "").strip())
                else:
                    out.append(str(item).strip())
            return ", ".join(part for part in out if part)
        if isinstance(value, dict):
            return str(value.get("name") or value.get("text") or value.get("id") or value.get("email") or "").strip()
        return str(value or "").strip()

    @staticmethod
    def _parse_due_at(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            raw = float(value)
            if raw > 10_000_000_000:
                raw /= 1000
            return datetime.fromtimestamp(raw, tz=UTC)
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    async def _resolve_recipients(self, fields: dict[str, Any], rule: ReminderRule) -> list[str]:
        raw_value = self._pick_first(fields, rule.recipient_fields)
        if raw_value in (None, "", [], {}):
            return []
        items = raw_value if isinstance(raw_value, list) else [raw_value]
        recipients: list[str] = []
        resolver = self._ensure_person_resolver()
        for item in items:
            resolved: str | None = None
            if isinstance(item, dict):
                resolved = str(item.get("open_id") or item.get("id") or "").strip() or None
            elif isinstance(item, str) and item.startswith("ou_"):
                resolved = item
            elif resolver is not None:
                resolved = await resolver(item)
            if resolved and resolved not in recipients:
                recipients.append(resolved)
        return recipients

    @staticmethod
    def _extract_status(fields: dict[str, Any]) -> str:
        return BitableReminderRuleEngine._normalize_text(fields.get("状态") or fields.get("status"))

    def _matches_rule(
        self,
        rule: ReminderRule,
        event_type: str,
        app_token: str,
        table_id: str,
        fields: dict[str, Any],
        changed_fields: list[str],
    ) -> tuple[bool, str]:
        if not rule.enabled:
            return False, "disabled"
        if rule.app_token and rule.app_token != app_token:
            return False, "app_token_mismatch"
        if rule.table_id and rule.table_id != table_id:
            return False, "table_id_mismatch"
        if event_type == "create" and not rule.on_create:
            return False, "create_disabled"
        if event_type == "update" and not rule.on_update:
            return False, "update_disabled"
        if rule.changed_fields_any and changed_fields and not set(rule.changed_fields_any).intersection(changed_fields):
            return False, "changed_fields_filtered"
        if rule.changed_fields_all and changed_fields and not set(rule.changed_fields_all).issubset(set(changed_fields)):
            return False, "changed_fields_filtered"
        if rule.status_in:
            value = self._extract_status(fields)
            if value and value not in rule.status_in:
                return False, "status_filtered"
        for key, expected in rule.field_equals.items():
            if fields.get(key) != expected:
                return False, f"field_filtered:{key}"
        if self._pick_first(fields, rule.time_fields) in (None, ""):
            return False, "missing_due_time"
        return True, "matched"

    def _should_cancel(self, rule: ReminderRule, event_type: str, fields: dict[str, Any]) -> bool:
        if event_type == "delete" and rule.cancel_on_deleted:
            return True
        status = self._extract_status(fields)
        return bool(status and status in rule.cancel_status_in)

    @staticmethod
    def _state_prefix(app_token: str, table_id: str, record_id: str, rule_id: str) -> str:
        return f"{app_token}:{table_id}:{record_id}:{rule_id}:"

    def _cancel_existing(self, state: dict[str, Any], *, app_token: str, table_id: str, record_id: str, rule: ReminderRule) -> list[dict[str, Any]]:
        prefix = self._state_prefix(app_token, table_id, record_id, rule.rule_id)
        results: list[dict[str, Any]] = []
        for external_key in [key for key in state if key.startswith(prefix)]:
            item = state.pop(external_key, {})
            cron_job_id = str(item.get("cron_job_id") or "")
            if cron_job_id:
                self.cron_service.remove_job(cron_job_id)
            self.reminder_runtime.cancel_by_external_key(external_key=external_key)
            results.append({
                "rule_id": rule.rule_id,
                "status": "cancelled",
                "recipient": item.get("recipient"),
                "cron_job_id": cron_job_id,
            })
            self._append_audit(f"bitable_record_cancel rule={rule.rule_id} record={record_id} external_key={external_key}")
        return results

    async def handle_field_changed(self, event: dict[str, Any]) -> None:
        cache = self._read_json(self.schema_cache_path)
        app_token = str(event.get("app_token") or "")
        table_id = str(event.get("table_id") or "")
        cache[f"{app_token}:{table_id}"] = {
            "field_id": event.get("field_id"),
            "field_name": event.get("field_name"),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._write_json(self.schema_cache_path, cache)
        self._append_audit(f"bitable_field_changed app_token={app_token} table_id={table_id}")

    async def handle_record_changed(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        app_token = str(event.get("app_token") or "").strip()
        table_id = str(event.get("table_id") or "").strip()
        record_id = str(event.get("record_id") or "").strip()
        event_type = str(event.get("event_type") or "update").strip().lower()
        fields = dict(event.get("fields") or {})
        changed_fields = [str(item).strip() for item in event.get("changed_fields", []) if str(item).strip()]
        results: list[dict[str, Any]] = []
        state = self._read_json(self.state_path)

        for rule in self.load_rules():
            if self._should_cancel(rule, event_type, fields):
                cancelled = self._cancel_existing(
                    state,
                    app_token=app_token,
                    table_id=table_id,
                    record_id=record_id,
                    rule=rule,
                )
                if cancelled:
                    results.extend(cancelled)
                    continue

            matched, reason = self._matches_rule(rule, event_type, app_token, table_id, fields, changed_fields)
            if not matched:
                self._append_audit(f"bitable_record_skip rule={rule.rule_id} record={record_id} reason={reason}")
                results.append({"rule_id": rule.rule_id, "status": "skipped", "reason": reason})
                continue

            due_at = self._parse_due_at(self._pick_first(fields, rule.time_fields))
            recipients = await self._resolve_recipients(fields, rule)
            if due_at is None:
                results.append({"rule_id": rule.rule_id, "status": "skipped", "reason": "invalid_due_time"})
                continue
            if not recipients:
                results.append({"rule_id": rule.rule_id, "status": "skipped", "reason": "no_recipient"})
                continue

            title = self._normalize_text(self._pick_first(fields, rule.title_fields)) or record_id
            body = self._normalize_text(self._pick_first(fields, rule.content_fields)) or title

            for recipient in recipients:
                for remind_before in rule.reminders:
                    external_key = f"{app_token}:{table_id}:{record_id}:{rule.rule_id}:{recipient}:{remind_before}"
                    reminder_text = f"{title} - {body}" if body != title else title
                    existing_job_id = str(state.get(external_key, {}).get("cron_job_id") or "")
                    if rule.dedupe_policy == "skip" and external_key in state:
                        results.append({
                            "rule_id": rule.rule_id,
                            "status": "skipped",
                            "reason": "deduped",
                            "recipient": recipient,
                        })
                        continue
                    if existing_job_id:
                        self.cron_service.remove_job(existing_job_id)
                        if rule.dedupe_policy == "replace":
                            self.reminder_runtime.cancel_by_external_key(external_key=external_key)

                    reminder_result = self.reminder_runtime.upsert_reminder(
                        external_key=external_key,
                        user_id=recipient,
                        chat_id=recipient,
                        channel="feishu",
                        text=reminder_text,
                        due_at=due_at.isoformat(),
                        overwrite=rule.overwrite_existing or rule.dedupe_policy in {"overwrite", "update", "replace"},
                    )

                    scheduled_at = due_at - timedelta(minutes=remind_before)
                    cron_job = self.cron_service.add_job(
                        name=f"bitable:{rule.rule_id}:{record_id}:{remind_before}",
                        schedule=CronSchedule(kind="at", at_ms=int(scheduled_at.timestamp() * 1000)),
                        message=reminder_text,
                        deliver=True,
                        channel="feishu",
                        to=recipient,
                        delete_after_run=True,
                    )
                    state[external_key] = {
                        "cron_job_id": cron_job.id,
                        "reminder_id": reminder_result["reminder"]["id"],
                        "record_id": record_id,
                        "rule_id": rule.rule_id,
                        "recipient": recipient,
                        "remind_before_minutes": remind_before,
                    }
                    self._append_audit(
                        f"bitable_record_match rule={rule.rule_id} record={record_id} recipient={recipient} cron={cron_job.id} remind_before={remind_before}"
                    )
                    results.append({
                        "rule_id": rule.rule_id,
                        "status": "scheduled",
                        "recipient": recipient,
                        "cron_job_id": cron_job.id,
                        "reminder_id": reminder_result["reminder"]["id"],
                        "remind_before_minutes": remind_before,
                    })

        self._write_json(self.state_path, state)
        return results
