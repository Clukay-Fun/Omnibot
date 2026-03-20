"""Structured doctor checks and safe local fixes for nanobot CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from nanobot.config.loader import _migrate_config, get_config_path, save_config
from nanobot.config.schema import Config
from nanobot.utils.helpers import sync_workspace_templates

DoctorStatus = Literal["ok", "warn", "error", "fixed", "skipped"]

_REQUIRED_WORKSPACE_FILES = (
    "AGENTS.md",
    "HEARTBEAT.md",
    "WORKLOG.md",
    "memory/MEMORY.md",
    "memory/HISTORY.md",
)


@dataclass(slots=True)
class DoctorFinding:
    """One doctor check result."""

    key: str
    status: DoctorStatus
    summary: str
    detail: str = ""
    fixable: bool = False
    restart_required: bool = False


@dataclass(slots=True)
class DoctorReport:
    """Aggregated doctor report."""

    config_path: Path
    workspace_path: Path | None = None
    findings: list[DoctorFinding] = field(default_factory=list)

    @property
    def restart_required(self) -> bool:
        return any(item.restart_required for item in self.findings)

    @property
    def has_remaining_issues(self) -> bool:
        return any(item.status in {"warn", "error"} for item in self.findings)


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    """Resolve the effective config path without mutating global loader state."""
    if config_path is None:
        return get_config_path().expanduser().resolve()
    return Path(config_path).expanduser().resolve()


def run_doctor(config_path: str | Path | None = None, *, fix: bool = False) -> DoctorReport:
    """Inspect local nanobot health and optionally apply safe repairs."""
    path = resolve_config_path(config_path)
    report = DoctorReport(config_path=path)

    config, raw_original, raw_migrated = _load_validated_config(path, report, fix=fix)
    if config is None:
        return report

    report.workspace_path = config.workspace_path

    needs_save = False
    normalization_needed = False
    if raw_original is None:
        report.findings.append(
            DoctorFinding(
                key="config.file",
                status="fixed",
                summary=f"Created default config at {path}.",
                fixable=True,
                restart_required=True,
            )
        )
        needs_save = True
        raw_original = {}
        raw_migrated = {}
    else:
        report.findings.append(
            DoctorFinding(
                key="config.file",
                status="ok",
                summary=f"Loaded config from {path}.",
            )
        )
        normalized = config.model_dump(by_alias=True)
        normalization_needed = raw_original != raw_migrated or raw_migrated != normalized
        if normalization_needed:
            if fix:
                report.findings.append(
                    DoctorFinding(
                        key="config.normalization",
                        status="fixed",
                        summary="Normalized config to the current schema and aliases.",
                        fixable=True,
                        restart_required=True,
                    )
                )
                needs_save = True
            else:
                report.findings.append(
                    DoctorFinding(
                        key="config.normalization",
                        status="warn",
                        summary="Config can be normalized to the current schema and aliases.",
                        fixable=True,
                    )
                )
        else:
            report.findings.append(
                DoctorFinding(
                    key="config.normalization",
                    status="ok",
                    summary="Config already matches the current schema.",
                )
            )

    workspace_needs_creation = not config.workspace_path.exists()
    if workspace_needs_creation:
        if fix:
            config.workspace_path.mkdir(parents=True, exist_ok=True)
            report.findings.append(
                DoctorFinding(
                    key="workspace.path",
                    status="fixed",
                    summary=f"Created workspace at {config.workspace_path}.",
                    fixable=True,
                )
            )
        else:
            report.findings.append(
                DoctorFinding(
                    key="workspace.path",
                    status="error",
                    summary=f"Workspace is missing: {config.workspace_path}",
                    fixable=True,
                )
            )
    else:
        report.findings.append(
            DoctorFinding(
                key="workspace.path",
                status="ok",
                summary=f"Workspace exists: {config.workspace_path}",
            )
        )

    missing_templates = _missing_workspace_templates(config.workspace_path)
    if missing_templates:
        if fix:
            sync_workspace_templates(config.workspace_path, silent=True)
            report.findings.append(
                DoctorFinding(
                    key="workspace.templates",
                    status="fixed",
                    summary=f"Restored {len(missing_templates)} missing workspace template(s).",
                    detail=", ".join(missing_templates),
                    fixable=True,
                )
            )
        else:
            report.findings.append(
                DoctorFinding(
                    key="workspace.templates",
                    status="warn",
                    summary=f"Workspace is missing {len(missing_templates)} template file(s).",
                    detail=", ".join(missing_templates),
                    fixable=True,
                )
            )
    else:
        report.findings.append(
            DoctorFinding(
                key="workspace.templates",
                status="ok",
                summary="Workspace template baseline is present.",
            )
        )

    provider_finding = _check_provider_baseline(config)
    report.findings.append(provider_finding)

    heartbeat_raw = _get_heartbeat_raw(raw_migrated)
    heartbeat_enabled_ok = isinstance(heartbeat_raw.get("enabled"), bool)
    if heartbeat_enabled_ok:
        report.findings.append(
            DoctorFinding(
                key="heartbeat.enabled",
                status="ok",
                summary=f"Heartbeat enabled flag is set to {config.gateway.heartbeat.enabled}.",
            )
        )
    else:
        if fix:
            config.gateway.heartbeat.enabled = bool(config.gateway.heartbeat.enabled)
            report.findings.append(
                DoctorFinding(
                    key="heartbeat.enabled",
                    status="fixed",
                    summary="Repaired heartbeat enabled flag to a valid boolean.",
                    fixable=True,
                    restart_required=True,
                )
            )
            needs_save = True
        else:
            report.findings.append(
                DoctorFinding(
                    key="heartbeat.enabled",
                    status="warn",
                    summary="Heartbeat enabled flag is missing or not a boolean in config.",
                    fixable=True,
                )
            )

    interval_raw = heartbeat_raw.get("intervalS")
    interval_ok = isinstance(interval_raw, int) and interval_raw > 0
    if interval_ok:
        report.findings.append(
            DoctorFinding(
                key="heartbeat.interval",
                status="ok",
                summary=f"Heartbeat interval is {config.gateway.heartbeat.interval_s}s.",
            )
        )
    else:
        if fix:
            config.gateway.heartbeat.interval_s = Config().gateway.heartbeat.interval_s
            report.findings.append(
                DoctorFinding(
                    key="heartbeat.interval",
                    status="fixed",
                    summary=f"Reset heartbeat interval to {config.gateway.heartbeat.interval_s}s.",
                    fixable=True,
                    restart_required=True,
                )
            )
            needs_save = True
        else:
            report.findings.append(
                DoctorFinding(
                    key="heartbeat.interval",
                    status="warn",
                    summary="Heartbeat interval is missing or not a positive integer in config.",
                    fixable=True,
                )
            )

    report.findings.append(_check_feishu_baseline(config))

    legacy_files = _find_legacy_heartbeat_files(config.workspace_path)
    if legacy_files:
        if fix:
            for item in legacy_files:
                item.unlink(missing_ok=True)
            report.findings.append(
                DoctorFinding(
                    key="heartbeat.legacy_sessions",
                    status="fixed",
                    summary=f"Removed {len(legacy_files)} legacy heartbeat session file(s).",
                    detail=", ".join(str(item.relative_to(config.workspace_path)) for item in legacy_files),
                    fixable=True,
                )
            )
        else:
            report.findings.append(
                DoctorFinding(
                    key="heartbeat.legacy_sessions",
                    status="warn",
                    summary=f"Found {len(legacy_files)} legacy heartbeat session file(s).",
                    detail=", ".join(str(item.relative_to(config.workspace_path)) for item in legacy_files),
                    fixable=True,
                )
            )
    else:
        report.findings.append(
            DoctorFinding(
                key="heartbeat.legacy_sessions",
                status="ok",
                summary="No legacy heartbeat session files found.",
            )
        )

    if needs_save:
        save_config(config, path)

    return report


def _load_validated_config(path: Path, report: DoctorReport, *, fix: bool) -> tuple[Config | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Load config without silently swallowing parse/schema failures."""
    if not path.exists():
        if not fix:
            report.findings.append(
                DoctorFinding(
                    key="config.file",
                    status="error",
                    summary=f"Config file not found: {path}",
                    fixable=True,
                )
            )
            return None, None, None
        return Config(), None, None

    try:
        raw_original = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.findings.append(
            DoctorFinding(
                key="config.file",
                status="error",
                summary=f"Config file is not valid JSON: {path}",
                detail=str(exc),
                fixable=False,
            )
        )
        return None, None, None

    try:
        raw_migrated = _migrate_config(json.loads(json.dumps(raw_original)))
        config = Config.model_validate(raw_migrated)
    except Exception as exc:
        report.findings.append(
            DoctorFinding(
                key="config.schema",
                status="error",
                summary="Config file could not be validated against the current schema.",
                detail=str(exc),
                fixable=False,
            )
        )
        return None, None, None

    return config, raw_original, raw_migrated


def _missing_workspace_templates(workspace: Path) -> list[str]:
    return [relative for relative in _REQUIRED_WORKSPACE_FILES if not (workspace / relative).exists()]


def _check_provider_baseline(config: Config) -> DoctorFinding:
    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    if provider_name:
        return DoctorFinding(
            key="providers.default",
            status="ok",
            summary=f"Default model '{model}' resolves to provider '{provider_name}'.",
        )
    if model.startswith("bedrock/"):
        return DoctorFinding(
            key="providers.default",
            status="ok",
            summary=f"Default model '{model}' relies on external Bedrock credentials.",
        )
    return DoctorFinding(
        key="providers.default",
        status="error",
        summary=f"Default model '{model}' has no usable configured provider or credentials.",
        fixable=False,
    )


def _check_feishu_baseline(config: Config) -> DoctorFinding:
    feishu = config.channels.feishu
    if not feishu.enabled:
        return DoctorFinding(
            key="channels.feishu",
            status="skipped",
            summary="Feishu channel is disabled.",
        )
    missing: list[str] = []
    if not feishu.app_id:
        missing.append("appId")
    if not feishu.app_secret:
        missing.append("appSecret")
    if missing:
        return DoctorFinding(
            key="channels.feishu",
            status="error",
            summary=f"Feishu is enabled but missing required config: {', '.join(missing)}.",
            fixable=False,
        )
    return DoctorFinding(
        key="channels.feishu",
        status="ok",
        summary="Feishu channel has the required baseline credentials.",
    )


def _get_heartbeat_raw(raw_config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_config, dict):
        return {}
    gateway = raw_config.get("gateway")
    if not isinstance(gateway, dict):
        return {}
    heartbeat = gateway.get("heartbeat")
    if not isinstance(heartbeat, dict):
        return {}
    return heartbeat


def _find_legacy_heartbeat_files(workspace: Path) -> list[Path]:
    sessions_dir = workspace / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(item for item in sessions_dir.rglob("*.jsonl") if "heartbeat" in item.name.lower())
