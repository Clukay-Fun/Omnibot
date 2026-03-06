"""描述:
主要功能:
    - 验证消息工具在缺少目标上下文时的返回行为。
"""

import pytest

from nanobot.agent.tools.message import MessageTool


#region 消息工具测试


@pytest.mark.asyncio
async def test_message_tool_returns_error_when_no_target_context() -> None:
    """用处，参数

    功能:
        - 校验未设置 channel/chat_id 时返回错误文本。
    """
    tool = MessageTool()
    result = await tool.execute(content="test")
    assert result == "Error: No target channel/chat specified"


#endregion
