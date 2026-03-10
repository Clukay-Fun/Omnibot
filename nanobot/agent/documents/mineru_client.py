"""描述:
主要功能:
    - 封装 MinerU 文档解析服务的提交与轮询调用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import httpx

from nanobot.config.schema import MinerUConfig

SUPPORTED_DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".txt",
}


#region 自定义异常体系

class MinerUClientError(RuntimeError):
    """
    用处: MinerU 交互发生错误的通用基类。

    功能:
        - 为该模块外抛的一切常规业务问题提供拦截捕获标准。
    """


class MinerUTimeoutError(MinerUClientError):
    """
    用处: 代表异步轮询任务等待越级的特定错误。

    功能:
        - 标记解析周期因为消耗时间触碰配置上限而被中止的场景。
    """

#endregion

#region 核心请求客户端

class MinerUClient:
    """
    用处: MinerU 文档拆解服务的异步 HTTP 客户端抽象。

    功能:
        - 支持并发下递交文档、获取解析状态以及长时阻塞等待整体流程结束。
    """

    def __init__(
        self,
        config: MinerUConfig,
        http_client_factory: Callable[..., httpx.AsyncClient] | None = None,
    ):
        """
        用处: 构造及注入网络适配依赖。参数 config: 矿石提取相关的设置项对象，http_client_factory: 底层 httpx 构建工厂。

        功能:
            - 初始化客户端实例及其关联的网络基础设施。
        """
        self.config = config
        self.http_client_factory = http_client_factory or httpx.AsyncClient

    async def submit_document(self, file_path: Path) -> str:
        """
        用处: 提交实体文件至解析端，换取任务票据号。参数 file_path: 本地将要上传的文档路径。

        功能:
            - 校验文件合法性与扩展名，组装多段（multipart）发送包并返回生成的主键 ID。
        """
        self._validate_file(file_path)

        url = f"{self.config.api_base.rstrip('/')}/v1/tasks"
        headers = self._headers()

        with file_path.open("rb") as fp:
            files = {"file": (file_path.name, fp, self._guess_content_type(file_path))}
            data = await self._request("POST", url, headers=headers, files=files)

        task_id = data.get("task_id") or data.get("id")
        if not task_id:
            raise MinerUClientError("MinerU submit response missing task id")
        return str(task_id)

    async def get_task_result(self, task_id: str) -> dict[str, Any]:
        """
        用处: 获取已提交任务的进展报文。参数 task_id: 之前阶段获取到的流水凭证。

        功能:
            - 调用检索游标主动请求最新的执行情况报文载体。
        """
        if not task_id.strip():
            raise MinerUClientError("task_id is required")

        url = f"{self.config.api_base.rstrip('/')}/v1/tasks/{task_id}"
        return await self._request("GET", url, headers=self._headers())

    async def wait_for_result(self, task_id: str) -> dict[str, Any]:
        """
        用处: 带有时间护盾的轮询同步观察器。参数 task_id: 用于追溯的凭证 ID。

        功能:
            - 根据配置轮询状态码，如果获取完成状态/异常立刻阻断，一旦溢出安全时长抛出专门错误。
        """
        timeout_s = float(self.config.polling.timeout_seconds)
        interval_s = float(self.config.polling.interval_seconds)
        started = asyncio.get_running_loop().time()

        while True:
            payload = await self.get_task_result(task_id)
            status = str(payload.get("status", "")).lower()

            if status in {"succeeded", "success", "done", "completed"}:
                return payload
            if status in {"failed", "error", "cancelled", "canceled"}:
                reason = payload.get("error") or payload.get("message") or "unknown MinerU error"
                raise MinerUClientError(f"MinerU task {task_id} failed: {reason}")

            elapsed = asyncio.get_running_loop().time() - started
            if elapsed >= timeout_s:
                raise MinerUTimeoutError(
                    f"MinerU task {task_id} timed out after {timeout_s:.1f}s "
                    f"(last status: {status or 'unknown'})"
                )
            await asyncio.sleep(interval_s)

    async def submit_and_wait(self, file_path: Path) -> dict[str, Any]:
        """
        用处: 文件上传与轮询解析结点的整合包裹。参数 file_path: 文档载体路径。

        功能:
            - 流畅串联单一文件的任务触发启动，直抵结束提取获取文本反馈。
        """
        task_id = await self.submit_document(file_path)
        return await self.wait_for_result(task_id)

    def _headers(self) -> dict[str, str]:
        """
        用处: 构建访问头阵列。

        功能:
            - 把鉴权密钥和请求接收意图打包处理至特定字典中。
        """
        headers = {"Accept": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """
        用处: 核心的基础网络连接及重连组件。参数 method: HTTP 方法（GET/POST等），url: 请求地址。

        功能:
            - 配置重试、超时设定，解析服务后端的 JSON 响应防止反序列化奔溃。
        """
        timeout = float(self.config.request.timeout_seconds)
        retries = int(self.config.request.max_retries)
        retry_delay = float(self.config.request.retry_delay_seconds)
        last_error: Exception | None = None

        for attempt in range(retries + 1):
            try:
                async with self.http_client_factory(timeout=timeout) as client:
                    response = await client.request(method, url, **kwargs)
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict):
                    raise MinerUClientError("MinerU response must be a JSON object")
                return data
            except (httpx.HTTPError, ValueError, MinerUClientError) as exc:
                last_error = exc
                if attempt >= retries:
                    break
                await asyncio.sleep(retry_delay)

        raise MinerUClientError(f"MinerU request failed after retries: {last_error}")

    def _validate_file(self, file_path: Path) -> None:
        """
        用处: 把关准备被传输的文件有效性。参数 file_path: 目标处理对象路径。

        功能:
            - 诊断文件是否存在及扩展后缀的解析兼容性，不符预期即果断驳回。
        """
        if not file_path.exists() or not file_path.is_file():
            raise MinerUClientError(f"Document file not found: {file_path}")

        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
            allowed = ", ".join(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))
            raise MinerUClientError(
                f"Unsupported file format '{suffix or '<none>'}' for {file_path.name}. "
                f"Allowed formats: {allowed}"
            )

    @staticmethod
    def _guess_content_type(path: Path) -> str:
        """
        用处: 由文件后缀猜测传输内容的 MIME 类型。参数 path: 实务文件途径。

        功能:
            - 返回对应该素材确切或宽泛的网络标识符（如 application/pdf 等）。
        """
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return "application/pdf"
        if suffix in {".doc", ".docx"}:
            return "application/octet-stream"
        if suffix in {".png"}:
            return "image/png"
        if suffix in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if suffix in {".tif", ".tiff"}:
            return "image/tiff"
        if suffix in {".bmp"}:
            return "image/bmp"
        return "text/plain"

#endregion
