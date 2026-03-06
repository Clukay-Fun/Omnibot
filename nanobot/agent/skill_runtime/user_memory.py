"""描述:
主要功能:
    - 提供按用户维度的本地记忆读写与更新。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.utils.helpers import ensure_dir, safe_filename


#region 记忆存储管理

class UserMemoryStore:
    """
    用处: 提供以频道和发送者 ID 为主键的用户记忆存储服务，将数据持久化到文件系统。

    功能:
        - 初始化记忆存储目录。
        - 根据用户信息读写本地 JSON 文件。
    """

    def __init__(self, workspace: Path):
        """
        用处: 构造函数，初始化存储目录。参数 workspace: 工作空间路径。

        功能:
            - 创建并确保 `workspace/memory/users` 目录存在。
        """
        self.root = ensure_dir(workspace / "memory" / "users")

    def path_for(self, channel: str, sender_id: str) -> Path:
        """
        用处: 生成特定用户的记忆文件路径。参数 channel: 频道来源，sender_id: 发送者标识。

        功能:
            - 通过安全的字符串转换，拼接出该用户的 JSON 文件绝对路径。
        """
        channel_key = safe_filename(channel)
        sender_key = safe_filename(sender_id)
        return self.root / f"{channel_key}__{sender_key}.json"

    def read(self, channel: str, sender_id: str) -> dict[str, Any]:
        """
        用处: 读取指定用户的记忆数据。参数 channel: 频道，sender_id: 发送者标识。

        功能:
            - 从指定路径读取并解析 JSON 文件内容，若文件不存在则返回空字典。
        """
        path = self.path_for(channel, sender_id)
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def write(self, channel: str, sender_id: str, profile: dict[str, Any]) -> Path:
        """
        用处: 写入指定用户的记忆数据。参数 channel: 频道，sender_id: 发送者标识，profile: 需要保存的内存数据。

        功能:
            - 将配置字典序列化为 JSON 并存储到对应的本地文件中。
        """
        path = self.path_for(channel, sender_id)
        path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def update(self, channel: str, sender_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """
        用处: 增量更新指定用户的记忆数据。参数 channel: 频道，sender_id: 发送者标识，patch: 需合入的更新数据。

        功能:
            - 读取当前记忆数据并与其进行字典合并（update），再把最终结果写回文件系统。
        """
        current = self.read(channel, sender_id)
        current.update(patch)
        self.write(channel, sender_id, current)
        return current

#endregion
