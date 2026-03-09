"""Utility functions for nanobot."""

import re
import shutil
from datetime import datetime
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """~/.nanobot data directory."""
    return ensure_dir(Path.home() / ".nanobot")


def get_state_path() -> Path:
    """~/.nanobot/state directory for runtime state and SQLite files."""
    return ensure_dir(get_data_path() / "state")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Resolve and ensure workspace path. Defaults to ~/.nanobot/workspace."""
    path = Path(workspace).expanduser() if workspace else Path.home() / ".nanobot" / "workspace"
    workspace_path = ensure_dir(path)
    bootstrap_workspace_dirs(workspace_path)
    return workspace_path


def timestamp() -> str:
    """Current ISO timestamp."""
    return datetime.now().isoformat()


_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*]')

def safe_filename(name: str) -> str:
    """Replace unsafe path characters with underscores."""
    return _UNSAFE_CHARS.sub("_", name).strip()


def migrate_legacy_path(source: Path, target: Path, *, related_suffixes: tuple[str, ...] = ()) -> bool:
    """Move a legacy runtime file and optional sidecars if target does not exist."""

    def _related_path(path: Path, suffix: str) -> Path:
        if suffix.startswith(".sqlite3"):
            return path.with_suffix(suffix)
        return Path(f"{path}{suffix}")

    try:
        if source.resolve() == target.resolve():
            return False
    except FileNotFoundError:
        pass

    if not source.exists() or target.exists():
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))

    for suffix in related_suffixes:
        legacy_path = _related_path(source, suffix)
        target_path = _related_path(target, suffix)
        if not legacy_path.exists() or target_path.exists():
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_path), str(target_path))

    source_parent = source.parent
    while source_parent != source_parent.parent:
        try:
            source_parent.rmdir()
        except OSError:
            break
        source_parent = source_parent.parent

    return True


def sync_workspace_templates(workspace: Path, silent: bool = False) -> list[str]:
    """Sync bundled templates to workspace. Only creates missing files."""
    from importlib.resources import files as pkg_files
    try:
        tpl = pkg_files("nanobot") / "templates"
    except Exception:
        return []
    if not tpl.is_dir():
        return []

    added: list[str] = []

    def _write(src, dest: Path):
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(src.read_text(encoding="utf-8") if src else "", encoding="utf-8")
        added.append(str(dest.relative_to(workspace)))

    for item in tpl.iterdir():
        if item.name.endswith(".md"):
            _write(item, workspace / item.name)
    _write(tpl / "runtime_texts.yaml", workspace / "runtime_texts.yaml")
    _write(tpl / "memory" / "MEMORY.md", workspace / "MEMORY.md")
    _write(tpl / "memory" / "MEMORY.md", workspace / "memory" / "MEMORY.md")
    _write(None, workspace / "memory" / "HISTORY.md")
    try:
        feishu_tpl = tpl / "feishu" / "bitable_rules.yaml"
        if feishu_tpl.is_file():
            _write(feishu_tpl, workspace / "feishu" / "bitable_rules.yaml")
    except Exception:
        pass

    try:
        extract_tpl = pkg_files("nanobot") / "skills" / "extract_templates"
        if extract_tpl.is_dir():
            for item in extract_tpl.iterdir():
                if item.name.endswith((".yaml", ".yml")):
                    _write(item, workspace / "extract" / item.name)
    except Exception:
        pass

    try:
        table_registry_tpl = pkg_files("nanobot") / "skills" / "table_registry.yaml"
        if table_registry_tpl.is_file():
            _write(table_registry_tpl, workspace / "skills" / "table_registry.yaml")
    except Exception:
        pass

    (workspace / "skills").mkdir(exist_ok=True)
    bootstrap_workspace_dirs(workspace)

    if added and not silent:
        from rich.console import Console
        for name in added:
            Console().print(f"  [dim]Created {name}[/dim]")
    return added


def bootstrap_workspace_dirs(workspace: Path) -> None:
    """Create runtime directories required by current features."""
    ensure_dir(workspace / "skillspec")
    ensure_dir(workspace / "memory" / "users")
    ensure_dir(workspace / "memory" / "feishu" / "users")
    ensure_dir(workspace / "memory" / "feishu" / "chats")
    ensure_dir(workspace / "memory" / "feishu" / "threads")
    ensure_dir(workspace / "extract")
    ensure_dir(workspace / "feishu")
    for legacy_dir in ("prompts", "routing", "templates"):
        target = workspace / legacy_dir
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
