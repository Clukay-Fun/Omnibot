"""飞书 API 端点：统一定义调用飞书开放平台相关接口所使用的路由路径。"""

# region [API 端点]

class FeishuEndpoints:
    """集成涉及到的各类飞书 OpenAPI 请求路径的中央管理类。"""

    @staticmethod
    def tenant_token() -> str:
        """获取企业自建应用访问令牌的内部端点。"""
        return "/auth/v3/tenant_access_token/internal"

    @staticmethod
    def bitable_tables(app_token: str) -> str:
        """列出某个多维表格 App 下所有数据表的端点。"""
        return f"/bitable/v1/apps/{app_token}/tables"

    @staticmethod
    def bitable_fields(app_token: str, table_id: str) -> str:
        """列出某个数据表中所有字段（列）配置的端点。"""
        return f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields"

    @staticmethod
    def bitable_records_search(app_token: str, table_id: str) -> str:
        """在特定数据表中根据复合过滤条件进行数据流式搜索的端点。"""
        return f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/search"

    @staticmethod
    def bitable_record(app_token: str, table_id: str, record_id: str) -> str:
        """检索、更新或删除一条特定数据表记录的端点。"""
        return f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"

    @staticmethod
    def doc_search() -> str:
        """搜索具有读权限的飞书云文档列表的端点。"""
        return "/drive/v1/files"

    @staticmethod
    def contact_users_batch_get_id() -> str:
        """通过邮箱或手机号换取飞书用户 ID 的端点。"""
        return "/contact/v3/users/batch_get_id"

    @staticmethod
    def contact_users_find_by_department() -> str:
        """按部门列出通讯录用户的端点。"""
        return "/contact/v3/users/find_by_department"

    @staticmethod
    def bitable_records(app_token: str, table_id: str) -> str:
        """创建记录或批量列出记录的端点。"""
        return f"/bitable/v1/apps/{app_token}/tables/{table_id}/records"

    @staticmethod
    def bitable_apps() -> str:
        """创建或列出多维表格 App 的端点。"""
        return "/bitable/v1/apps"

    @staticmethod
    def bitable_views(app_token: str, table_id: str) -> str:
        """创建或列出数据表视图（View）的端点。"""
        return f"/bitable/v1/apps/{app_token}/tables/{table_id}/views"

    @staticmethod
    def calendar_list() -> str:
        """列出用户日历的端点。"""
        return "/calendar/v4/calendars"

    @staticmethod
    def calendar_detail(calendar_id: str) -> str:
        """读取、更新、删除单个日历的端点。"""
        return f"/calendar/v4/calendars/{calendar_id}"

    @staticmethod
    def calendar_freebusy() -> str:
        """查询忙闲信息的端点。"""
        return "/calendar/v4/freebusy"

    @staticmethod
    def calendar_event_attendees(calendar_id: str, event_id: str) -> str:
        """管理日程参会人集合的端点。"""
        return f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees"

    @staticmethod
    def calendar_event_attendee(calendar_id: str, event_id: str, attendee_id: str) -> str:
        """更新或删除单个参会人的端点。"""
        return f"/calendar/v4/calendars/{calendar_id}/events/{event_id}/attendees/{attendee_id}"

    @staticmethod
    def task_v2_tasks() -> str:
        """创建任务或按条件查询任务列表的端点。"""
        return "/task/v2/tasks"

    @staticmethod
    def task_v2_task(task_id: str) -> str:
        """读取、更新、删除单个任务的端点。"""
        return f"/task/v2/tasks/{task_id}"

    @staticmethod
    def task_v2_tasklists() -> str:
        """查询任务清单（TaskList）的端点。"""
        return "/task/v2/tasklists"

    @staticmethod
    def task_v2_subtasks(task_id: str) -> str:
        """创建子任务的端点。"""
        return f"/task/v2/tasks/{task_id}/subtasks"

    @staticmethod
    def task_v2_comments(task_id: str) -> str:
        """新增任务评论的端点。"""
        return f"/task/v2/tasks/{task_id}/comments"

    @staticmethod
    def im_message_list() -> str:
        """拉取会话消息历史列表的端点。"""
        return "/im/v1/messages"

# endregion
