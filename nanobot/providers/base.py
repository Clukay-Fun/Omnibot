"""描述:
主要功能:
    - 定义模型提供方的统一响应结构与抽象接口。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#region 响应数据结构

@dataclass
class ToolCallRequest:
    """用处，参数

    功能:
        - 表示模型返回的单次工具调用请求。
    """
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """用处，参数

    功能:
        - 封装模型回复文本、工具调用和用量信息。
    """
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    thinking_blocks: list[dict] | None = None  # Anthropic extended thinking

    @property
    def has_tool_calls(self) -> bool:
        """用处，参数

        功能:
            - 判断响应中是否包含工具调用。
        """
        return len(self.tool_calls) > 0


#endregion

#region 提供方抽象接口


class LLMProvider(ABC):
    """用处，参数

    功能:
        - 约束不同模型提供方的公共行为。
    """

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        """用处，参数

        功能:
            - 保存提供方鉴权与地址配置。
        """
        self.api_key = api_key
        self.api_base = api_base

    @staticmethod
    def _sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """用处，参数

        功能:
            - 清洗空内容消息，避免部分提供方返回 400。
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")

            if isinstance(content, str) and not content:
                clean = dict(msg)
                clean["content"] = None if (msg.get("role") == "assistant" and msg.get("tool_calls")) else "(empty)"
                result.append(clean)
                continue

            if isinstance(content, list):
                filtered = [
                    item for item in content
                    if not (
                        isinstance(item, dict)
                        and item.get("type") in ("text", "input_text", "output_text")
                        and not item.get("text")
                    )
                ]
                if len(filtered) != len(content):
                    clean = dict(msg)
                    if filtered:
                        clean["content"] = filtered
                    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                        clean["content"] = None
                    else:
                        clean["content"] = "(empty)"
                    result.append(clean)
                    continue

            result.append(msg)
        return result

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_name: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """用处，参数

        功能:
            - 发送对话请求并返回统一响应对象。
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """用处，参数

        功能:
            - 返回当前提供方默认模型标识。
        """
        pass


#endregion
