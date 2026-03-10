#!/usr/bin/env python3
"""Create and rotate Feishu sqlite backups using config retention settings."""

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from nanobot.config.loader import load_config  # noqa: E402


def _copy_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def main() -> int:
    config = load_config()
    storage = config.integrations.feishu.storage

    db_path = config.resolve_feishu_state_db_path()
    if not db_path.exists():
        print(f"skip: sqlite file does not exist: {db_path}")
        return 0

    backup_dir_raw = str(storage.sqlite_backup_dir or "").strip()
    if backup_dir_raw:
        backup_dir = Path(backup_dir_raw).expanduser()
        if not backup_dir.is_absolute():
            backup_dir = (config.workspace_path / backup_dir).resolve()
    else:
        backup_dir = db_path.parent / "backup"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"{db_path.stem}-{stamp}{db_path.suffix}"
    backup_file = backup_dir / base_name
    backup_dir.mkdir(parents=True, exist_ok=True)

    copied_main = _copy_if_exists(db_path, backup_file)
    copied_wal = _copy_if_exists(db_path.with_suffix(db_path.suffix + "-wal"), backup_dir / (base_name + "-wal"))
    copied_shm = _copy_if_exists(db_path.with_suffix(db_path.suffix + "-shm"), backup_dir / (base_name + "-shm"))

    retention_days = max(1, int(storage.sqlite_backup_retention_days))
    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = 0
    for entry in backup_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            modified = datetime.fromtimestamp(entry.stat().st_mtime)
        except OSError:
            continue
        if modified < cutoff:
            try:
                entry.unlink()
                deleted += 1
            except OSError:
                continue

    print(
        "backup completed",
        f"db={db_path}",
        f"backup={backup_file}",
        f"copied_main={copied_main}",
        f"copied_wal={copied_wal}",
        f"copied_shm={copied_shm}",
        f"retention_days={retention_days}",
        f"deleted={deleted}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
