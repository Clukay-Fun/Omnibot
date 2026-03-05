"""Utility functions for nanobot."""

import re
from datetime import datetime
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    """Ensure directory exists, return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """~/.nanobot data directory."""
    return ensure_dir(Path.home() / ".nanobot")


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
    _write(tpl / "memory" / "MEMORY.md", workspace / "memory" / "MEMORY.md")
    _write(None, workspace / "memory" / "HISTORY.md")

    try:
        extract_tpl = pkg_files("nanobot") / "skills" / "extract_templates"
        if extract_tpl.is_dir():
            for item in extract_tpl.iterdir():
                if item.name.endswith((".yaml", ".yml")):
                    _write(item, workspace / "extract" / item.name)
    except Exception:
        pass

    try:
        workspace_tpl = pkg_files("nanobot") / "templates" / "workspace"

        def _copy_tree(src, dest_prefix: Path) -> None:
            for node in src.iterdir():
                dest = dest_prefix / node.name
                if node.is_dir():
                    _copy_tree(node, dest)
                    continue
                if node.name.endswith((".yaml", ".yml", ".json", ".md")):
                    _write(node, workspace / dest)

        if workspace_tpl.is_dir():
            _copy_tree(workspace_tpl, Path())
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
    ensure_dir(workspace / "extract")
    ensure_dir(workspace / "prompts")
    ensure_dir(workspace / "routing")
    ensure_dir(workspace / "templates")
