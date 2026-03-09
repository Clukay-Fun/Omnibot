"""描述:
主要功能:
    - 维护工具注册、查询与执行入口。
"""

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool


@dataclass(slots=True)
class ToolExposureContext:
    channel: str = ""
    user_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    pending_write: bool = False
    mode: str = ""
    authorized_tools: tuple[str, ...] = ()
    authorized_resources: dict[str, tuple[str, ...] | str] = field(default_factory=dict)


_DEV_TOOL_NAMES = {"read_file", "write_file", "edit_file", "list_dir", "exec", "spawn"}
_WEB_TOOL_NAMES = {"web_search", "web_fetch"}
_BITABLE_READ_TOOL_NAMES = {
    "bitable_search",
    "bitable_list_tables",
    "bitable_match_table",
    "bitable_list_fields",
    "bitable_prepare_create",
    "bitable_get",
    "bitable_search_person",
    "bitable_directory_search",
}
_BITABLE_QUERY_TOOL_NAMES = _BITABLE_READ_TOOL_NAMES - {"bitable_prepare_create"}
_BITABLE_WRITE_TOOL_NAMES = {"bitable_create", "bitable_update", "bitable_delete"}
_BITABLE_ADMIN_TOOL_NAMES = {
    "bitable_app_create",
    "bitable_table_create",
    "bitable_view_create",
    "bitable_sync_schema",
}
_CALENDAR_TOOL_NAMES = {
    "calendar_list",
    "calendar_create",
    "calendar_update",
    "calendar_delete",
    "calendar_freebusy",
}
_CALENDAR_QUERY_TOOL_NAMES = {"calendar_list", "calendar_freebusy"}
_TASK_TOOL_NAMES = {
    "task_create",
    "task_get",
    "task_update",
    "task_delete",
    "task_list",
    "tasklist_list",
    "subtask_create",
    "task_comment_add",
}
_TASK_QUERY_TOOL_NAMES = {"task_get", "task_list", "tasklist_list"}
_MESSAGE_HISTORY_TOOL_NAMES = {"message_history_list"}
_REMINDER_TOOL_NAMES = {"cron"}
_FEISHU_RESEARCH_TOOL_NAMES = _BITABLE_QUERY_TOOL_NAMES | {"bitable_directory_search"} | _MESSAGE_HISTORY_TOOL_NAMES | _CALENDAR_QUERY_TOOL_NAMES | _TASK_QUERY_TOOL_NAMES


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _has_bitable_intent(text: str, *, include_write_terms: bool = False) -> bool:
    tokens = ("表格", "多维表格", "bitable", "字段", "schema", "周工作计划", "table_id", "视图", "view")
    return _contains_any(text, tokens) or (include_write_terms and _contains_any(text, ("记录到",)))


def _normalized_string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        text = value.strip()
        return {text} if text else set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()

#region 工具注册表

class ToolRegistry:
    """用处，参数

    功能:
        - 管理工具生命周期并统一执行校验。
    """

    def __init__(self):
        """用处，参数

        功能:
            - 初始化空的工具映射表。
        """
        self._tools: dict[str, Tool] = {}

    @staticmethod
    def _resource_scope_hint(exposure: ToolExposureContext | None) -> str:
        if exposure is None:
            return "none"
        allowed_tables = tuple(sorted(_normalized_string_set(exposure.authorized_resources.get("allowed_tables"))))
        if allowed_tables:
            return f"allowed_tables={','.join(allowed_tables)}"
        return "none"

    @staticmethod
    def _log_authz_denial(tool_name: str, mode: str, resource_scope: str) -> None:
        logger.warning(
            "Tool authorization denied: tool={} mode={} resources={}",
            tool_name,
            mode or "default",
            resource_scope,
        )

    def register(self, tool: Tool) -> None:
        """用处，参数

        功能:
            - 按工具名称注册工具实例。
        """
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """用处，参数

        功能:
            - 按名称移除已注册工具。
        """
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """用处，参数

        功能:
            - 返回指定名称的工具实例。
        """
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """用处，参数

        功能:
            - 判断工具名称是否存在。
        """
        return name in self._tools

    @staticmethod
    def _select_feishu_tools(exposure: ToolExposureContext, tool_names: set[str]) -> set[str]:
        text = exposure.user_text.strip().lower()
        if not text:
            return set()
        mode = exposure.mode or "main_chat_readonly"

        directory_intent = _contains_any(text, ("通讯录", "联系人", "同事", "open_id", "邮箱", "手机号", "电话"))
        write_intent = _contains_any(text, ("新增", "创建", "写入", "添加", "记到", "记录到", "更新", "修改", "删除", "移除"))
        bitable_intent = write_intent or _has_bitable_intent(text, include_write_terms=True)
        calendar_intent = _contains_any(text, ("日历", "日程", "会议", "空闲", "忙闲", "calendar"))
        task_intent = _contains_any(text, ("任务", "待办", "todo", "subtask", "评论", "备注任务"))
        message_history_intent = _contains_any(text, ("消息历史", "聊天记录", "历史消息", "上一条消息", "引用消息", "message history"))
        dev_intent = _contains_any(
            text,
            (
                "代码",
                "文件",
                "测试",
                "命令",
                "终端",
                "git",
                "python",
                "typescript",
                "javascript",
                "repo",
                "日志",
                "报错",
                "stack trace",
                "traceback",
            ),
        )
        web_intent = _contains_any(text, ("搜索", "查网页", "官网", "新闻", "联网", "web", "google", "百度"))
        admin_intent = _contains_any(text, ("建表", "创建表", "创建视图", "创建 app", "同步 schema", "schema 快照"))
        reminder_intent = _contains_any(text, ("提醒", "cron", "定时"))

        exposed: set[str] = set()
        if mode == "main_chat_readonly":
            if dev_intent:
                exposed |= _DEV_TOOL_NAMES | _WEB_TOOL_NAMES
            if web_intent:
                exposed |= _WEB_TOOL_NAMES
            if reminder_intent:
                exposed |= _REMINDER_TOOL_NAMES
            return exposed & tool_names

        if exposure.pending_write or mode == "main_write_commit":
            exposed |= _BITABLE_READ_TOOL_NAMES | _BITABLE_WRITE_TOOL_NAMES
        if directory_intent:
            exposed.add("bitable_directory_search")
        if bitable_intent:
            exposed |= _BITABLE_QUERY_TOOL_NAMES if mode == "main_feishu_query" else _BITABLE_READ_TOOL_NAMES
        if mode == "main_write_prepare" and write_intent:
            exposed |= _BITABLE_WRITE_TOOL_NAMES | {"bitable_directory_search"}
        if calendar_intent:
            exposed |= _CALENDAR_QUERY_TOOL_NAMES if mode == "main_feishu_query" else _CALENDAR_TOOL_NAMES
        if task_intent:
            exposed |= _TASK_QUERY_TOOL_NAMES if mode == "main_feishu_query" else _TASK_TOOL_NAMES
        if message_history_intent:
            exposed |= _MESSAGE_HISTORY_TOOL_NAMES
        if dev_intent and mode != "main_feishu_query":
            exposed |= _DEV_TOOL_NAMES | _WEB_TOOL_NAMES
        if web_intent and mode != "main_feishu_query":
            exposed |= _WEB_TOOL_NAMES
        if admin_intent and mode == "main_write_prepare":
            exposed |= _BITABLE_ADMIN_TOOL_NAMES | _BITABLE_READ_TOOL_NAMES
        if reminder_intent:
            exposed |= _REMINDER_TOOL_NAMES

        return exposed & tool_names

    @staticmethod
    def _select_subagent_tools(exposure: ToolExposureContext, tool_names: set[str]) -> set[str]:
        authorized = {str(item).strip() for item in exposure.authorized_tools if str(item).strip()}
        if exposure.mode in {"subagent_apply", "write_apply"}:
            return authorized & tool_names
        if exposure.mode == "feishu_research":
            return (_FEISHU_RESEARCH_TOOL_NAMES | authorized) & tool_names
        if exposure.mode == "code_research":
            return (_DEV_TOOL_NAMES | _WEB_TOOL_NAMES | authorized) & tool_names

        text = exposure.user_text.strip().lower()
        directory_intent = _contains_any(text, ("通讯录", "联系人", "同事", "open_id", "邮箱", "手机号", "电话"))
        bitable_intent = _has_bitable_intent(text, include_write_terms=True)
        calendar_intent = _contains_any(text, ("日历", "日程", "会议", "空闲", "忙闲", "calendar"))
        task_intent = _contains_any(text, ("任务", "待办", "todo", "subtask", "评论", "备注任务"))
        message_history_intent = _contains_any(text, ("消息历史", "聊天记录", "历史消息", "上一条消息", "引用消息", "message history"))

        if directory_intent or bitable_intent or calendar_intent or task_intent or message_history_intent:
            exposed: set[str] = set()
            if directory_intent:
                exposed.add("bitable_directory_search")
            if bitable_intent:
                exposed |= _BITABLE_READ_TOOL_NAMES
            if calendar_intent:
                exposed |= _CALENDAR_TOOL_NAMES
            if task_intent:
                exposed |= _TASK_TOOL_NAMES
            if message_history_intent:
                exposed |= _MESSAGE_HISTORY_TOOL_NAMES
            return (exposed | authorized) & tool_names

        return (_DEV_TOOL_NAMES | _WEB_TOOL_NAMES | authorized) & tool_names

    @classmethod
    def _allowed_tool_names(cls, exposure: ToolExposureContext | None, tool_names: set[str]) -> set[str]:
        if exposure is None:
            return set(tool_names)
        if exposure.mode.startswith("subagent_") or exposure.mode in {"feishu_research", "code_research", "write_apply"}:
            return cls._select_subagent_tools(exposure, tool_names)
        if exposure.channel != "feishu":
            return set(tool_names)
        return cls._select_feishu_tools(exposure, tool_names)

    @classmethod
    def _is_tool_authorized(
        cls,
        name: str,
        params: dict[str, Any],
        exposure: ToolExposureContext | None,
        tool_names: set[str],
    ) -> bool:
        allowed = cls._allowed_tool_names(exposure, tool_names)
        if name not in allowed:
            return False
        if exposure is None:
            return True
        if name in _BITABLE_ADMIN_TOOL_NAMES and name not in {
            str(item).strip() for item in exposure.authorized_tools if str(item).strip()
        }:
            return False
        if exposure.mode in {"main_chat_readonly", "main_feishu_query", "subagent_plan", "feishu_research", "code_research"} and name in _BITABLE_WRITE_TOOL_NAMES:
            return False
        if exposure.mode == "main_write_prepare" and name in _BITABLE_WRITE_TOOL_NAMES and params.get("confirm_token"):
            return False
        allowed_tables = _normalized_string_set(exposure.authorized_resources.get("allowed_tables"))
        if allowed_tables and name in (_BITABLE_WRITE_TOOL_NAMES | _BITABLE_ADMIN_TOOL_NAMES):
            table_id = str(params.get("table_id") or "").strip()
            if not table_id or table_id not in allowed_tables:
                return False
        return True

    def get_definitions(self, exposure: ToolExposureContext | None = None) -> list[dict[str, Any]]:
        """用处，参数

        功能:
            - 生成所有工具的 schema 定义列表。
        """
        selected_names = self._allowed_tool_names(exposure, set(self._tools))
        return [tool.to_schema() for name, tool in self._tools.items() if name in selected_names]

    async def execute(self, name: str, params: dict[str, Any], exposure: ToolExposureContext | None = None) -> str:
        """用处，参数

        功能:
            - 校验参数并执行目标工具，返回文本结果。
        """
        _hint = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}"
        if not self._is_tool_authorized(name, params, exposure, set(self._tools)):
            mode = exposure.mode if exposure is not None else "default"
            self._log_authz_denial(name, mode, self._resource_scope_hint(exposure))
            return f"Error: Tool '{name}' is not authorized in mode '{mode or 'default'}'." + _hint

        try:
            errors = tool.validate_params(params)
            if errors:
                return f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _hint
            result = await tool.execute(**params)
            if isinstance(result, str) and result.startswith("Error"):
                return result + _hint
            return result
        except Exception as e:
            return f"Error executing {name}: {str(e)}" + _hint

    @property
    def tool_names(self) -> list[str]:
        """用处，参数

        功能:
            - 返回当前所有已注册工具名。
        """
        return list(self._tools.keys())

    def __len__(self) -> int:
        """用处，参数

        功能:
            - 返回注册表中工具数量。
        """
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        """用处，参数

        功能:
            - 支持使用 in 判断工具是否存在。
        """
        return name in self._tools

#endregion
