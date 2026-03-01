"""飞书数据异常：飞书 API 请求相关的自定义异常类。"""

from typing import Any

# region [异常定义]

class FeishuDataAPIError(RuntimeError):
    """
    当飞书远程 API 返回业务级错误码或发生 HTTP 级别的网络异常时引发。
    包含远程响应的状态码、高可读性的错误消息以及可选的详细响应载荷。
    """

    def __init__(self, code: int, message: str, detail: Any | None = None):
        super().__init__(f"[{code}] {message}: {detail}")
        self.code = code
        self.message = message
        self.detail = detail

# endregion

