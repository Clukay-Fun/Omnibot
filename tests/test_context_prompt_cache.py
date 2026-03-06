"""描述:
主要功能:
    - 验证上下文构建中的提示词缓存稳定性。
"""

from __future__ import annotations

from datetime import datetime as real_datetime
from pathlib import Path
import datetime as datetime_module

from nanobot.agent.context import ContextBuilder


#region 测试辅助


class _FakeDatetime(real_datetime):
    """用处，参数

    功能:
        - 提供可控时间用于测试提示词稳定性。
    """
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        """用处，参数

        功能:
            - 返回当前预设的测试时间。
        """
        return cls.current


def _make_workspace(tmp_path: Path) -> Path:
    """用处，参数

    功能:
        - 创建并返回隔离的测试工作目录。
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


#endregion

#region 提示词缓存测试


def test_system_prompt_stays_stable_when_clock_changes(tmp_path, monkeypatch) -> None:
    """用处，参数

    功能:
        - 验证仅时间变化不会影响系统提示词内容。
    """
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    _FakeDatetime.current = real_datetime(2026, 2, 24, 13, 59)
    prompt1 = builder.build_system_prompt()

    _FakeDatetime.current = real_datetime(2026, 2, 24, 14, 0)
    prompt2 = builder.build_system_prompt()

    assert prompt1 == prompt2


def test_runtime_context_is_separate_untrusted_user_message(tmp_path) -> None:
    """用处，参数

    功能:
        - 验证运行时元数据作为独立用户消息注入。
    """
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]

    assert messages[-2]["role"] == "user"
    runtime_content = messages[-2]["content"]
    assert isinstance(runtime_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in runtime_content
    assert "Current Time:" in runtime_content
    assert "Channel: cli" in runtime_content
    assert "Chat ID: direct" in runtime_content

    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Return exactly: OK"


#endregion
