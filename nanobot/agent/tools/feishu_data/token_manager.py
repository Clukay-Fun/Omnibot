"""飞书访问令牌管理：处理 tenant_access_token 的获取、缓存、提前刷新及并发锁控制。"""

import asyncio
import time
from typing import Callable

import httpx

from nanobot.agent.tools.feishu_data.endpoints import FeishuEndpoints
from nanobot.agent.tools.feishu_data.errors import FeishuDataAPIError
from nanobot.config.schema import FeishuDataConfig

# region [令牌管理器]

class TenantAccessTokenManager:
    """
    飞书企业自建应用访问令牌 (tenant_access_token) 的生命周期管理器。
    提供内存级别的令牌缓存，利用 asyncio.Lock 避免并发请求重叠，并支持接近过期时提前刷新。
    """

    def __init__(self, config: FeishuDataConfig, http_client_factory: Callable[..., httpx.AsyncClient] | None = None):
        self.config = config
        self.http_client_factory = http_client_factory or httpx.AsyncClient
        self._token: str | None = None
        self._expire_time: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """
        获取一个有效的 tenant_access_token。
        当当前令牌已过期或处于即将过期的提前刷新窗口内时，将自动获取新令牌。
        通过内部锁确保多并发调用时仅产生一次真实的网络请求。
        """
        now = time.time()
        # If valid and not within refresh window
        if self._token and now < (self._expire_time - self.config.token.refresh_ahead_seconds):
            return self._token

        async with self._lock:
            # Double check inside lock
            now = time.time()
            if self._token and now < (self._expire_time - self.config.token.refresh_ahead_seconds):
                return self._token

            token, expire_in = await self._fetch_token()
            self._token = token
            self._expire_time = now + expire_in
            return self._token

    async def cache_snapshot(self) -> dict[str, int | bool]:
        """提供当前令牌状态的快照，该快照通常可用于状态诊断或探活返回。"""
        now = time.time()
        return {
            "has_token": self._token is not None,
            "expires_in_seconds": max(0, int(self._expire_time - now)) if self._token else 0
        }

    async def _fetch_token(self) -> tuple[str, int]:
        """执行底层 HTTP 请求以向飞书服务器请求新令牌。"""
        url = self.config.api_base.rstrip("/") + FeishuEndpoints.tenant_token()
        payload = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret
        }

        async with self.http_client_factory(timeout=float(self.config.request.timeout)) as client:
            try:
                response = await client.post(url, json=payload)
                response.raise_for_status()
            except httpx.HTTPError as e:
                raise FeishuDataAPIError(-1, "HTTP exception during token fetch", str(e))

        data = response.json()
        if data.get("code") != 0:
            raise FeishuDataAPIError(data.get("code", -1), data.get("msg", "Unknown error fetching token"), data)

        return data["tenant_access_token"], data["expire"]

# endregion

