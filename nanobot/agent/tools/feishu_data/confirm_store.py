"""确认令牌存储：为写入操作提供进程内存级别的一次性确认令牌管理。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

# region [确认令牌存储]


class ConfirmTokenStore:
    """
    进程内存级别的一次性确认令牌存储。

    - 令牌通过 `create()` 生成，绑定操作负载的哈希摘要。
    - 令牌通过 `consume()` 消费，仅当 payload 哈希匹配且未过期时才返回 True。
    - 令牌一旦消费即销毁，不可重复使用。
    - 过期令牌在每次 `create()` / `consume()` 时自动清理。
    """

    def __init__(self, ttl_seconds: int = 300):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[str, float]] = {}  # token -> (payload_hash, expires_at)

    @staticmethod
    def _hash_payload(payload: dict[str, Any]) -> str:
        """将操作负载序列化后取 SHA-256 摘要，用于绑定验证。"""
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _gc(self) -> None:
        """清理已过期的令牌。"""
        now = time.time()
        expired = [k for k, (_, exp) in self._store.items() if exp <= now]
        for k in expired:
            del self._store[k]

    def create(self, payload: dict[str, Any]) -> str:
        """
        生成一个与 payload 绑定的一次性确认令牌。

        Args:
            payload: 操作负载，用于后续验证时比对。

        Returns:
            新生成的令牌字符串。
        """
        self._gc()
        token = uuid.uuid4().hex
        payload_hash = self._hash_payload(payload)
        self._store[token] = (payload_hash, time.time() + self._ttl)
        return token

    def consume(self, token: str, payload: dict[str, Any]) -> bool:
        """
        消费令牌。成功消费后令牌即被销毁。

        Args:
            token: 待验证的令牌字符串。
            payload: 当前操作负载，必须与创建时的 payload 哈希一致。

        Returns:
            True 表示验证通过并已消费；False 表示令牌无效/过期/不匹配。
        """
        self._gc()
        entry = self._store.pop(token, None)
        if entry is None:
            return False

        stored_hash, expires_at = entry
        if time.time() > expires_at:
            return False

        return stored_hash == self._hash_payload(payload)


# endregion
