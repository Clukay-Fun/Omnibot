from pathlib import Path
from unittest.mock import patch

import pytest

from nanobot.agent.tools.filesystem import EditFileTool, WriteFileTool


@pytest.mark.asyncio
async def test_write_file_logs_worklog_update_and_noop(tmp_path: Path) -> None:
    with patch("nanobot.agent.tools.filesystem.logger.info") as mock_info:
        tool = WriteFileTool(workspace=tmp_path)

        first = await tool.execute(path="WORKLOG.md", content="hello")
        second = await tool.execute(path="WORKLOG.md", content="hello")

        assert "Successfully wrote" in first
        assert "No changes written" in second
        messages = [call.args[0] for call in mock_info.call_args_list]
        assert any("WORKLOG updated via write_file" in message for message in messages)
        assert any("WORKLOG unchanged via write_file" in message for message in messages)


@pytest.mark.asyncio
async def test_edit_file_logs_worklog_update(tmp_path: Path) -> None:
    with patch("nanobot.agent.tools.filesystem.logger.info") as mock_info:
        worklog = tmp_path / "WORKLOG.md"
        worklog.write_text("before", encoding="utf-8")
        tool = EditFileTool(workspace=tmp_path)

        result = await tool.execute(path="WORKLOG.md", old_text="before", new_text="after")

        assert "Successfully edited" in result
        assert worklog.read_text(encoding="utf-8") == "after"
        messages = [call.args[0] for call in mock_info.call_args_list]
        assert any("WORKLOG updated via edit_file" in message for message in messages)


@pytest.mark.asyncio
async def test_write_file_normalizes_legacy_worklog_schema(tmp_path: Path) -> None:
    tool = WriteFileTool(workspace=tmp_path)

    await tool.execute(
        path="WORKLOG.md",
        content=(
            "## 进行中\n\n"
            "1. 旧格式事项\n"
            "   - 优先级：高\n"
            "   - 状态/下一步：先改 prompt\n"
            "   - 阻塞：无\n\n"
            "## 待处理\n\n"
            "## 已完成\n"
        ),
    )

    content = (tmp_path / "WORKLOG.md").read_text(encoding="utf-8")
    assert "### 旧格式事项" in content
    assert "1. 旧格式事项" not in content
    assert "- 阻塞：" not in content
