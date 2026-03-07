# Omnibot

**Omnibot**（基于 [nanobot](https://github.com/HKUDS/nanobot)）是专为**飞书 (Feishu / Lark) 场景深度定制**的个人 AI 助理框架。

它保留了 nanobot 轻量级、无状态、多模型接入的优秀底层架构，并在此基础上进行了大量的飞书原生能力增强与交互重构，让你能够在飞书中获得流畅、深入的智能助理体验。

## 🌟 核心定制特性

- **深度集成的飞书流式卡片 (Card 2.0)**：独家实现单卡片平滑流式输出，支持结构化展现“思考过程”（可折叠）与最终回答，彻底告别消息刷屏。
- **飞书多维表格 (Bitable) 原生直连**：内置完整的多维表格 CRUD 工具，通过自然语言即可直接检索、新增、修改、删除你的 Bitable 业务数据，支持两阶段防呆确认。
- **智能化群聊与上下文管控**：
  - 完美支持飞书话题（Thread）和话题群模式，自动维持话题内的独立上下文流。
  - 支持飞书富文本帖子、图片、合并转发卡片的解析与理解。
  - 细粒度的群聊唤醒门控（全开、仅@、关闭），并支持特定管理员指令强行穿透。
- **SkillSpec 大模型前置路由系统**：
  - 摒弃了僵化的关键字匹配，重构为基于大语言模型原生 Tool Calling 的意图识别路由机制。
  - 将业务拆解为简单的声明式 YAML (`skillspec`)，即可低代码扩展专属于你的飞书业务数据查询与提醒技能。
- **无感知的飞书新用户引导**：内置友好的 `/setup` 引导流程，以交互式卡片的形式一键完成团队与角色配置。

---

## ⚙️ 飞书通道配置 (channels.feishu)

在 `~/.nanobot/config.json` 中配置飞书通道：

```yaml
channels:
  feishu:
    enabled: true
    react_enabled: false      # 是否自动回复表情反馈 (收到/处理中)
    reply_to_message: true    # 是否使用引用回复
    reply_in_thread: false    # 默认是否在 Thread 中回复（话题群会自动强制启用）

    # 审计保留与清理
    audit_cleanup_interval_seconds: 21600
    audit_event_retention_days: 365
    audit_message_index_retention_days: 365

    # 记忆写回策略（单聊/群聊分档）
    memory_flush_threshold_private: 3
    memory_flush_threshold_group: 5
    memory_force_flush_on_topic_end: true
    memory_topic_end_keywords: ["先这样", "结束", "结论", "收尾", "done"]

    # 🚀 流式互动卡片 (Card 2.0)
    stream_card_enabled: true
    stream_card_min_update_ms: 120        # 卡片更新防抖间隔
    stream_card_print_frequency_ms: 50    # 打字机效果步进间隔
    stream_card_print_step: 2             # 打字机效果每次步进字符数
    stream_card_show_thinking: true       # 是否在卡片中展示思考过程块

    # 🛡️ 群聊与权限门控
    activation_private_policy: always     # 私聊响应策略
    activation_group_policy: mention      # 群聊响应策略（推荐 mention 仅@回复）
    activation_topic_policy: always       # 话题响应策略
    activation_admin_open_ids: []         # 特权管理员的 Open ID 列表
    activation_admin_prefix_bypass: "/bot" # 管理员强行越权响应的前缀

integrations:
  feishu:
    auth:
      app_id: ""           # 建议用环境变量注入
      app_secret: ""       # 建议用环境变量注入
      encrypt_key: ""      # 建议用环境变量注入
      verification_token: "" # 建议用环境变量注入
    storage:
      state_db_path: ""    # 可为空；默认 ~/.nanobot/workspace/memory/feishu/state.sqlite3
      sqlite_journal_mode: "WAL"
      sqlite_synchronous: "NORMAL"
      sqlite_busy_timeout_ms: 5000
      sqlite_backup_dir: ""
      sqlite_backup_interval_hours: 24
      sqlite_backup_retention_days: 7
    oauth:
      enabled: true
      public_base_url: "https://bot.example.com"
      callback_path: "/oauth/feishu/callback"
      enforce_https_public_base_url: true
      allowed_redirect_domains: ["bot.example.com"]

tools:
  feishu_data:
    feature_flags:
      calendar_enabled: true
      task_enabled: true
      bitable_admin_enabled: true
      message_history_enabled: true
```

> **流式最佳实践**：推荐配置 `stream_answer_warmup_chars=24`、`stream_answer_warmup_ms=300`、`stream_card_min_update_ms=120` 和 `stream_card_print_frequency_ms=50` 以获得最丝滑的视觉体验。

推荐通过环境变量注入敏感配置（避免明文入库）：

```bash
export NANOBOT_INTEGRATIONS__FEISHU__AUTH__APP_ID="cli_xxx"
export NANOBOT_INTEGRATIONS__FEISHU__AUTH__APP_SECRET="xxx"
export NANOBOT_INTEGRATIONS__FEISHU__AUTH__ENCRYPT_KEY="xxx"
export NANOBOT_INTEGRATIONS__FEISHU__AUTH__VERIFICATION_TOKEN="xxx"
export NANOBOT_INTEGRATIONS__FEISHU__STORAGE__STATE_DB_PATH="/var/lib/nanobot/feishu/state.sqlite3"
```

## 🛠️ 内置指令 (Commands)

在聊天框发送以下指令控制 Omnibot：

- `/help` 或 `/commands`：显示全部指令与简介。
- `/new`：归档并清空当前会话上下文（重新开始）。
- `/stop`：紧急停止当前正在执行的耗时任务或长文本流式响应。
- `/session`：查看会话子命令。
  - `/session new [标题]`：从当前消息强制创建一个独立处理的飞书话题（Thread）。
  - `/session list`：列出当前聊天下的所有活跃会话（主会话 + 独立话题）。
  - `/session del [id|main]`：删除当前或指定会话。

## 🧩 声明式业务技能 (SkillSpec)

Omnibot 提供了强大且安全的声明式数据集成系统，支持快速将多维表格等业务查询组装为 AI 工具。

- **多层级配置加载**：
  1. `workspace/skillspec/*.yaml` (最高优先级，用户本地自定义)
  2. `workspace/skillspec/managed/*.yaml` (次级，集中管理分发)
  3. `nanobot/skills/skillspec/*.yaml` (内置保底默认规格)
- **防脱敏抽象层设计**：通过 `table_registry.yaml` 配置真实 `table_id` 和复杂的原始表头别名映射，避免将生产环境真实的表格 ID 和底层中文字段名直接暴露给大模型造成混淆和泄露风险。
- **混合智能路由**：引入了确定性规则优先（`explicit > regex > keyword`）与大模型自动编排相结合的调度逻辑，并可选开启 Embedding 向量化检索作为超大规模技能池场景下的路由兜底。

## 📅 协同与提醒系统 (Reminder MVP)

提供基于本地状态机确权流转的协同任务机制：
- 内置 `reminder_set`, `reminder_list`, `reminder_cancel`, `daily_summary` 等基础待办管理集。
- 提醒数据原生固化持久存储于 `workspace/reminders.json` 以保证重启后依然保持本地精确调度。
- 支持各种跨系统聚合桥接（Bridge）：
  - **Record Bridge**: 同步写入进度与内容更新操作至特定 Bitable 归档记录。
  - **Summary Cron Bridge**: 每日维护汇总统揽的摘要并推送调度更新。 

## 🛡️ 文档处理引擎加固

针对飞书文件解析场景，提供了严格的质量控制和错误溯源分类拦截机制：
- `[UNSUPPORTED_FORMAT]` 不支持的识别格式
- `[FILE_NOT_FOUND]` 解析请求资源丢失
- `[LOW_QUALITY_EXTRACTION]` 源文件质量极差或识别内容无有效信息退回
- `[API_TIMEOUT] / [API_ERROR]` 飞书平台开放接口拥堵和超限报警

## 🚀 部署与发版 (CI & Release)

- **持续集成**：默认采用 GitHub Actions 建立专门服务于飞书 SDK 交互兼容的 `.github/workflows/ci.yml` 验证环境体系，并启用 Ruff 强制代码风格审查（超过 300+ pytest 并发跑通认证）。
- **流程规范**：标准化打包分发流经由 `.github/workflows/release.yml` 处理释放流程并投递到 GitHub Release 或 PyPI 以供版本追踪与回退；需严格遵循 `RELEASE_CHECKLIST.md` 内容清单控制发行质量。

## 📘 生产运维手册

- 生产配置、联调脚本、灰度/回滚、监控告警详见 `OPERATIONS_FEISHU.md`。
- 生产环境变量模板详见 `ops/env/feishu-production.env.example`。
