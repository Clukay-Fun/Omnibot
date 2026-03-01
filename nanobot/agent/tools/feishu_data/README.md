# Feishu Data Tools 模块文档

## 概述

本模块通过飞书开放平台 OpenAPI 为 nanobot 智能体提供 **多维表格 (Bitable)** 和 **云文档 (Drive)** 的原生数据操作能力。所有工具通过 `tools.feishu_data` 配置段启用，并在 `AgentLoop` / `SubagentManager` 初始化时自动注册。

## 架构总览

```
feishu_data/
├── __init__.py          # 公共 API 导出
├── client.py            # HTTP 客户端（令牌注入、重试、错误处理）
├── token_manager.py     # tenant_access_token 生命周期管理
├── endpoints.py         # 飞书 API 路由路径集中管理
├── errors.py            # FeishuDataAPIError 统一异常
├── date_utils.py        # ISO 日期 → 毫秒时间戳 → filter 结构
├── field_utils.py       # 字段名映射与归一化
├── confirm_store.py     # 写入操作的一次性确认令牌存储
├── bitable.py           # 只读工具：search / list_tables / get / search_person
├── bitable_write.py     # 写入工具：create / update / delete（两阶段安全）
├── doc_search.py        # 云文档搜索工具
└── registry.py          # 工具注册工厂
```

## 工具清单

| 工具名 | 类型 | 功能 |
|--------|------|------|
| `bitable_search` | 只读 | 按关键词/日期/过滤器检索多维表格记录 |
| `bitable_list_tables` | 只读 | 列出多维表格 App 下的所有数据表 |
| `bitable_get` | 只读 | 按 record_id 获取单条记录详情 |
| `bitable_search_person` | 只读 | 按人员姓名搜索多维表格记录 |
| `doc_search` | 只读 | 搜索飞书云文档 |
| `bitable_create` | 写入 | 创建新记录（两阶段：dry_run → confirm） |
| `bitable_update` | 写入 | 更新已有记录（两阶段：dry_run → confirm） |
| `bitable_delete` | 写入 | 删除记录（两阶段：dry_run → confirm） |

## 两阶段写入安全机制

所有写入工具均采用 **dry_run + confirm_token** 安全模式：

1. **阶段 1**：不传 `confirm_token` → 返回操作预览 + 一次性令牌
2. **阶段 2**：回传 `confirm_token` → 验证 payload 哈希一致后执行实际写入

令牌特性：TTL 可配置（默认 300 秒）、一次性消费、payload 绑定。

## 配置说明

在 `~/.nanobot/config.json` 的 `tools` 下添加 `feishuData` 段：

```json
{
  "tools": {
    "feishuData": {
      "enabled": true,
      "appId": "<飞书数据应用 App ID>",
      "appSecret": "<飞书数据应用 App Secret>",
      "apiBase": "https://open.feishu.cn/open-apis",
      "confirmTokenTtlSeconds": 300,
      "token": {
        "refreshAheadSeconds": 300
      },
      "request": {
        "timeout": 30,
        "maxRetries": 3,
        "retryDelay": 1.0
      },
      "bitable": {
        "domain": "https://your-tenant.feishu.cn",
        "defaultAppToken": "<多维表格 app_token>",
        "defaultTableId": "<默认数据表 table_id>",
        "defaultViewId": null,
        "fieldMapping": {
          "姓名": "Name",
          "状态": "Status",
          "创建日期": "Created"
        },
        "search": {
          "searchableFields": ["姓名", "描述"],
          "dateField": "创建日期",
          "maxRecords": 100,
          "defaultLimit": 20
        }
      },
      "doc": {
        "search": {
          "defaultFolderToken": null,
          "previewLength": 200,
          "defaultLimit": 10
        }
      }
    }
  }
}
```

### 关键配置项

| 配置路径 | 说明 |
|----------|------|
| `enabled` | 是否启用飞书数据工具（`false` 时不注册任何工具） |
| `appId` / `appSecret` | **数据账号 (Account A)** 的凭据，独立于渠道的飞书账号 |
| `bitable.defaultAppToken` | 默认多维表格 App Token，工具调用时可覆盖 |
| `bitable.fieldMapping` | 字段名映射表，将原始字段名重命名为 LLM 友好名称 |
| `bitable.search.dateField` | 日期过滤的目标字段名 |
| `confirmTokenTtlSeconds` | 写入确认令牌的有效时长（秒） |
