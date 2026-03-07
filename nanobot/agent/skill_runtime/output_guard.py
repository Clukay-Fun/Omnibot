"""描述:
主要功能:
    - 提供大段输出的截断保护与续传缓存。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

#region 缓存结构定义

@dataclass(slots=True)
class GuardResult:
    """
    用处: 携带截断保护处理后的最终输出结构。

    功能:
        - 记录经过处理的正文内容及是否被截断的状态。
        - 包含用于查询缓存接续段落的标识符与剩余项信息。
    """
    content: str | list[Any]
    truncated: bool
    continuation_token: str | None = None
    remaining_chars: int = 0
    remaining_items: int = 0


@dataclass(slots=True)
class _CacheEntry:
    """
    用处: 承载分页接续数据项的容器类。

    功能:
        - 封装尚未发送的接续内容及其预期过期时间。
    """
    payload: str | list[Any]
    expires_at: float

#endregion

#region 输出截断与缓存处理

class ContinuationCache:
    """
    用处: 在内存中持有限时有效的接续响应内容数据。

    功能:
        - 将剩余的长文本或列表暂存并绑定唯一查询令牌。
        - 配置存活时长机制，定时剔除已过期记录。
    """

    def __init__(self, ttl_seconds: int = 600, now_fn: Callable[[], float] | None = None):
        """
        用处: 初始化接续缓存。参数 ttl_seconds: 缓存数据的总有效期秒数，now_fn: 获取当前时间的指针方法。

        功能:
            - 设定生命周期阈值与底层字典。
        """
        self.ttl_seconds = ttl_seconds
        self._now_fn = now_fn or time.monotonic
        self._entries: dict[str, _CacheEntry] = {}

    def put(self, payload: str | list[Any]) -> str:
        """
        用处: 将内容推入缓存并产生提取令牌。参数 payload: 因截断被遗留的数据（文本或列表）。

        功能:
            - 清理过期项并生成基于 UUID 的唯一键，放入内部字典。
        """
        self._purge_expired()
        token = uuid.uuid4().hex
        self._entries[token] = _CacheEntry(payload=payload, expires_at=self._now_fn() + self.ttl_seconds)
        return token

    def pop(self, token: str) -> str | list[Any] | None:
        """
        用处: 使用已有令牌读取并销毁被缓存的接续数据。参数 token: 临时存储标识键。

        功能:
            - 从字典中取走目标记录，拦截已过期的数据，返回真实遗留信息。
        """
        self._purge_expired()
        entry = self._entries.pop(token, None)
        if not entry:
            return None
        if entry.expires_at <= self._now_fn():
            return None
        return entry.payload

    def _purge_expired(self) -> None:
        """
        用处: 执行惰性过期淘汰策略。

        功能:
            - 遍历内部字典清除所有时间戳失效的项目。
        """
        now = self._now_fn()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)


class OutputGuard:
    """
    用处: 为技能回复作安全防身与格式切分的上层封装类。

    功能:
        - 防止超大字数和长队列导致的发送失败或刷屏，执行切面管控并将余料委托给 ContinuationCache 模块。
    """

    def __init__(self, continuation_cache: ContinuationCache | None = None):
        """
        用处: 初始化输出守护器。参数 continuation_cache: 自定义的延续缓存实例，可缺省。

        功能:
            - 绑定底下的持久化依赖缓存对象。
        """
        self.continuation_cache = continuation_cache or ContinuationCache()

    def guard_text(self, content: str, *, max_chars: int) -> GuardResult:
        """
        用处: 对过长文本内容做守护与截断处理。参数 content: 拟发送完整文本，max_chars: 允许的最大输出字符数。

        功能:
            - 审视字符串总长度，超出时切割出可见段及截断标志。
        """
        if len(content) <= max_chars:
            return GuardResult(content=content, truncated=False)

        visible = content[:max_chars]
        remaining = content[max_chars:]
        token = self.continuation_cache.put(remaining)
        return GuardResult(
            content=visible,
            truncated=True,
            continuation_token=token,
            remaining_chars=len(remaining),
        )

    def guard_items(self, items: list[Any], *, max_items: int) -> GuardResult:
        """
        用处: 对数据长列表列作守护与截断处理。参数 items: 拟发送列表全集，max_items: 允许列出的最大项目数。

        功能:
            - 评估列表总规模并执行分割操作，生成对应的接续令牌。
        """
        if len(items) <= max_items:
            return GuardResult(content=items, truncated=False)

        visible = items[:max_items]
        remaining = items[max_items:]
        token = self.continuation_cache.put(remaining)
        return GuardResult(
            content=visible,
            truncated=True,
            continuation_token=token,
            remaining_items=len(remaining),
        )

    def continue_from(self, token: str) -> str | list[Any] | None:
        """
        用处: 自接续令牌唤出后续的待发数据。参数 token: 用户接续动作中的缓存访问标识。

        功能:
            - 借助底层缓存类查询相应的段落片段或列组信息返回。
        """
        return self.continuation_cache.pop(token)

#endregion
