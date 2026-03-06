"""描述:
主要功能:
    - 验证 CLI 交互输入读取与会话初始化流程。
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prompt_toolkit.formatted_text import HTML

from nanobot.cli import commands


#region 夹具与辅助


@pytest.fixture
def mock_prompt_session():
    """用处，参数

    功能:
        - 提供可控的全局 prompt 会话替身。
    """
    mock_session = MagicMock()
    mock_session.prompt_async = AsyncMock()
    with patch("nanobot.cli.commands._PROMPT_SESSION", mock_session), \
         patch("nanobot.cli.commands.patch_stdout"):
        yield mock_session


#endregion

#region 交互输入测试


@pytest.mark.asyncio
async def test_read_interactive_input_async_returns_input(mock_prompt_session):
    """用处，参数

    功能:
        - 验证异步读取会返回用户输入内容。
    """
    mock_prompt_session.prompt_async.return_value = "hello world"

    result = await commands._read_interactive_input_async()
    
    assert result == "hello world"
    mock_prompt_session.prompt_async.assert_called_once()
    args, _ = mock_prompt_session.prompt_async.call_args
    assert isinstance(args[0], HTML)  # Verify HTML prompt is used


@pytest.mark.asyncio
async def test_read_interactive_input_async_handles_eof(mock_prompt_session):
    """用处，参数

    功能:
        - 验证 EOFError 会转换为 KeyboardInterrupt。
    """
    mock_prompt_session.prompt_async.side_effect = EOFError()

    with pytest.raises(KeyboardInterrupt):
        await commands._read_interactive_input_async()


#endregion

#region 会话初始化测试


def test_init_prompt_session_creates_session():
    """用处，参数

    功能:
        - 验证初始化后会创建全局 prompt 会话。
    """
    # Ensure global is None before test
    commands._PROMPT_SESSION = None
    
    with patch("nanobot.cli.commands.PromptSession") as MockSession, \
         patch("nanobot.cli.commands.FileHistory") as MockHistory, \
         patch("pathlib.Path.home") as mock_home:
        
        mock_home.return_value = MagicMock()
        
        commands._init_prompt_session()
        
        assert commands._PROMPT_SESSION is not None
        MockSession.assert_called_once()
        _, kwargs = MockSession.call_args
        assert kwargs["multiline"] is False
        assert kwargs["enable_open_in_editor"] is False


#endregion
