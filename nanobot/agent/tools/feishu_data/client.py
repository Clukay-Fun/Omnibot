"""飞书数据客户端：提供连接至飞书 OpenAPI 的 HTTP 客户端，集成令牌管理、重试与统一错误处理。"""

import asyncio
import time
from typing import Any, Callable

import httpx
from loguru import logger

from nanobot.agent.tools.feishu_data.errors import FeishuDataAPIError
from nanobot.agent.tools.feishu_data.token_manager import TenantAccessTokenManager
from nanobot.config.schema import FeishuDataConfig

# region [HTTP 客户端]

class FeishuDataClient:
    """
    负责与飞书 OpenAPI 交互的高级 HTTP 客户端。
    内置了自动获取与附加令牌、请求重试以及统一的飞书 API 错误解析功能。
    """

    def __init__(
        self,
        config: FeishuDataConfig,
        token_manager: TenantAccessTokenManager | None = None,
        http_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ):
        self.config = config
        self.http_client_factory = http_client_factory or httpx.AsyncClient
        self.token_manager = token_manager or TenantAccessTokenManager(
            config=config,
            http_client_factory=self.http_client_factory,
        )

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        bearer_token: str | None = None,
        auth_mode: str = "app",
    ) -> dict[str, Any]:
        """
        发送携带认证信息的 HTTP 请求到飞书 API。
        自动附加 Bearer Token，并在遇到网络或限流等错误时执行重试策略，
        同时将飞书特有的业务错误包装为 `FeishuDataAPIError` 抛出。
        """
        token = bearer_token
        if not token:
            if auth_mode == "app":
                token = await self.token_manager.get_token()
            else:
                raise FeishuDataAPIError(-1, f"Missing bearer token for auth_mode={auth_mode}")

        req_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        if headers:
            req_headers.update(headers)

        url = self.config.api_base.rstrip("/") + path
        timeout = float(self.config.request.timeout)
        max_retries = self.config.request.max_retries
        retry_delay = float(self.config.request.retry_delay)

        last_error = None

        logger.debug(f"Feishu API Request: {method} {url}")
        start_time = time.time()
        for attempt in range(max_retries + 1):
            try:
                async with self.http_client_factory(timeout=timeout) as client:
                    response = await client.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=req_headers
                    )

                    duration = time.time() - start_time
                    if response.status_code >= 400:
                        logger.error(f"Feishu API Error: {method} {url} - Status {response.status_code} - Duration {duration:.2f}s")
                        try:
                            error_data = response.json()
                            raise FeishuDataAPIError(
                                error_data.get("code", response.status_code),
                                error_data.get("msg", response.text),
                                error_data
                            )
                        except ValueError:
                            raise FeishuDataAPIError(response.status_code, response.text)

                    data = response.json()
                    # Check Feishu API business code
                    if "code" in data and data["code"] != 0:
                        logger.error(f"Feishu API Business Error: {method} {url} - Code {data.get('code')} - Msg {data.get('msg')} - Duration {duration:.2f}s")
                        raise FeishuDataAPIError(data["code"], data.get("msg", "Unknown API error"), data)

                    logger.debug(f"Feishu API Success: {method} {url} - Duration {duration:.2f}s")
                    return data
            except httpx.HTTPError as e:
                last_error = e
                duration = time.time() - start_time
                logger.warning(f"Feishu API Retry: {method} {url} - Attempt {attempt+1}/{max_retries+1} - Error {e} - Duration {duration:.2f}s")
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)

        raise FeishuDataAPIError(-1, "重试耗尽后的网络错误 (Network error after retries)", str(last_error))

# endregion
