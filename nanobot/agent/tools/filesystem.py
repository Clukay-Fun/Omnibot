"""
描述: 本地文件系统操作工具集。
主要功能:
    - 提供向安全沙箱（Workspace 等机制）内读取、写入、内容替换和列出目录内容的能力。
"""

import difflib
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool

# region [路径限制与解析]


def _resolve_candidate(path: str, workspace: Path | None = None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    return p.resolve()


def _ensure_in_allowed_dir(path: Path, *, raw_path: str, allowed_dir: Path | None = None) -> None:
    if not allowed_dir:
        return
    try:
        path.relative_to(allowed_dir.resolve())
    except ValueError:
        raise PermissionError(f"Path {raw_path} is outside allowed directory {allowed_dir}")

def _resolve_path(path: str, workspace: Path | None = None, allowed_dir: Path | None = None) -> Path:
    """
    用处: 安全组装和校验绝对路径。

    功能:
        - 强制相对于工作区解析路径，并实施目录边界限制检查以阻止跨目录攻击。
    """
    resolved = _resolve_candidate(path, workspace)
    _ensure_in_allowed_dir(resolved, raw_path=path, allowed_dir=allowed_dir)
    return resolved


def _is_skill_markdown(path: Path) -> bool:
    return path.name.lower() == "skill.md"


def _resolve_skill_write_path(
    path: str,
    *,
    workspace: Path | None,
    allowed_dir: Path | None,
) -> tuple[Path, Path]:
    requested = _resolve_candidate(path, workspace)
    if workspace is None or not _is_skill_markdown(requested):
        _ensure_in_allowed_dir(requested, raw_path=path, allowed_dir=allowed_dir)
        return requested, requested

    workspace_skills = (workspace / "skills").resolve()
    try:
        requested.relative_to(workspace_skills)
        _ensure_in_allowed_dir(requested, raw_path=path, allowed_dir=allowed_dir)
        return requested, requested
    except ValueError:
        pass

    skill_name = requested.parent.name.strip() or "skill"
    redirected = (workspace_skills / skill_name / "SKILL.md").resolve()
    _ensure_in_allowed_dir(redirected, raw_path=str(redirected), allowed_dir=allowed_dir)
    return redirected, requested


# endregion

# region [文件读取工具]

class ReadFileTool(Tool):
    """
    用处: 读取单个文件全文。

    功能:
        - 向模型呈现指定限制目录内的纯文本或代码文件内容。
    """

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file at the given path."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            content = file_path.read_text(encoding="utf-8")
            return content
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


# endregion

# region [文件写入工具]

class WriteFileTool(Tool):
    """
    用处: 截断并写入内容至指定文件。

    功能:
        - 提供创建新文件并按需生成上游缺失父级目录的能力。
    """

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to write to"
                },
                "content": {
                    "type": "string",
                    "description": "The content to write"
                }
            },
            "required": ["path", "content"]
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path, _ = _resolve_skill_write_path(
                path,
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
            )
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} bytes to {file_path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


# endregion

# region [文件编辑工具]

class EditFileTool(Tool):
    """
    用处: 对已有文件行踪局部定点修改。

    功能:
        - 基于精确的字符串匹配进行替换。适用于只想改一行而不重写全文的场景。
    """

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a file by replacing old_text with new_text. The old_text must exist exactly in the file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to edit"
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace"
                },
                "new_text": {
                    "type": "string",
                    "description": "The text to replace with"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        try:
            file_path, source_path = _resolve_skill_write_path(
                path,
                workspace=self._workspace,
                allowed_dir=self._allowed_dir,
            )
            if file_path != source_path and source_path.exists() and source_path.is_file() and not file_path.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
            if not file_path.exists():
                return f"Error: File not found: {path}"

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return self._not_found_message(old_text, content, path)

            # 计算出现次数
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            file_path.write_text(new_content, encoding="utf-8")

            return f"Successfully edited {file_path}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"

    @staticmethod
    def _not_found_message(old_text: str, content: str, path: str) -> str:
        """
        用处: 大模型常常记错当前代码段，此方法用以辅助容错。

        功能:
            - 当精确匹配 `old_text` 失败时，执行向下的模糊相似度对比。
            - 以 unified diff 的格式将“最接近的匹配处”展示给模型，帮助其理解真正的上下文并修正其修改指令。
        """
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)

        best_ratio, best_start = 0.0, 0
        for i in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, i

        if best_ratio > 0.5:
            diff = "\n".join(difflib.unified_diff(
                old_lines, lines[best_start : best_start + window],
                fromfile="old_text (provided)", tofile=f"{path} (actual, line {best_start + 1})",
                lineterm="",
            ))
            return f"Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
        return f"Error: old_text not found in {path}. No similar text found. Verify the file content."


# endregion

# region [目录列出工具]

class ListDirTool(Tool):
    """
    用处: 文件目录探测工具。

    功能:
        - 以 `📁` 与 `📄` 的标识树出安全范围内指定目标目录内的文件列表。
    """

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            dir_path = _resolve_path(path, self._workspace, self._allowed_dir)
            if not dir_path.exists():
                return f"Error: Directory not found: {path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {path}"

            items = []
            for item in sorted(dir_path.iterdir()):
                prefix = "📁 " if item.is_dir() else "📄 "
                items.append(f"{prefix}{item.name}")

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"

# endregion
