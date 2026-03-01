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

# endregion

