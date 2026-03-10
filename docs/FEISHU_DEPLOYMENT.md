# 飞书部署指南

本指南涵盖了针对 Nanobot 飞书优先运行时的生产环境飞书设置。

## 推荐模式

除非你有特定的网络原因不能使用，否则请默认使用此推荐模式：

- `websocket` - 推荐。不需要公共端点（公网 IP 或域名）。最容易启动和调试。
- `webhook` - 当你希望飞书直接调用你的公共网关时使用。
- `hybrid` - 混合模式。在迁移时，或当你希望在发布期间同时提供两种入口路径时使用。

在所有情况下，`nanobot gateway` 的命令保持不变。飞书模式在配置文件中进行选择。

## 最小 WebSocket 配置

```json
{
  "channels": {
    "sendProgress": true,
    "sendToolHints": true,
    "feishu": {
      "enabled": true,
      "mode": "websocket",
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "allowFrom": ["*"],
      "groupSessionMode": "shared"
    }
  }
}
```

注意：

- 如果你希望消息卡片在工具使用/思考期间动态更新进度，`sendProgress: true` 是必须的。
- `sendToolHints: true` 是可选的，但如果你希望在卡片中显示 `tool_hint` (工具提示) 补丁，则推荐开启。
- `allowFrom` 填的是飞书用户 `open_id`。如果你暂时还不知道自己的 `open_id`，第一次接入建议先用 `["*"]` 完成引导，再收紧成显式白名单。

## 推荐的完整飞书配置

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.nanobot-feishu/workspace",
      "memoryWindow": 100
    }
  },
  "channels": {
    "sendProgress": true,
    "sendToolHints": true,
    "feishu": {
      "enabled": true,
      "mode": "websocket",
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "verificationToken": "",
      "encryptKey": "",
      "allowFrom": ["*"],
      "groupSessionMode": "shared",
      "memoryDbPath": "~/.nanobot-feishu/feishu-memory.sqlite3",
      "dedupeDbPath": "~/.nanobot-feishu/feishu-dedupe.sqlite3",
      "sessionTtlSeconds": 86400,
      "streamingScope": "dm",
      "streamThrottleSeconds": 0.5
    }
  },
  "gateway": {
    "host": "0.0.0.0",
    "port": 18790
  }
}
```

## 会话与记忆行为

短期会话键名 (Session keys):

- 单聊 (DM): `feishu:dm:{user_open_id}`
- 群聊默认 (共享): `feishu:chat:{chat_id}`
- 群聊按人隔离模式: `feishu:chat:{chat_id}:user:{user_open_id}`

长期记忆 (Long-term memory):

- 存储键名: `tenant_key + user_open_id`
- 数据结构: `profile + summary` (用户画像 + 摘要)
- 注入策略:
  - 单聊 (DM) 会注入 `profile + summary`
  - 群聊仅注入 `profile`

关于 `allowFrom` 与 BOOTSTRAP：

- `BOOTSTRAP.md` 的首次引导发生在消息已经通过 ACL 之后。
- 当前系统不会在第一次对话后自动把 `open_id` 回写到配置文件。
- 因此，如果你希望第一次对话就完成 BOOTSTRAP，引导期最省事的做法是 `allowFrom: ["*"]`。
- 完成首次引导后，再根据日志里看到的 `open_id` 收紧为显式白名单。

命令:

- `/help` - 飞书 Shell 帮助
- `/clear` - 立即清除短期会话，并在后台归档最近的上下文
- `/forget` - 删除当前租户用户的飞书长期记忆

归档行为:

- `/clear` 和窗口溢出时会进行异步归档
- 懒加载 TTL（生存时间）会在飞书适配器开始翻译时同步归档，并且只有在归档成功后才会清除短期会话

## 流式卡片机制 (Streaming Cards)

当前飞书的流式输出是基于阶段级的进度事件，而不是 token 级别的 LLM 输出。

- 第一个 `_progress` 事件会以回复用户原消息的方式创建一张卡片
- 后续的 `_progress` / `_tool_hint` 事件会修补 (patch) 同一张卡片
- 最终的回复会将卡片修补为完成状态
- 如果修补失败，飞书会降级发送一条纯文本的最终消息

相关设置：

- `channels.sendProgress` - 进度事件的全局开关
- `channels.sendToolHints` - 工具提示事件的全局开关
- `channels.feishu.streamingScope` - 可选 `off`, `dm`, 或是 `all`
- `channels.feishu.streamThrottleSeconds` - 轻量级的补丁合并窗口时间，默认 `0.5`

推荐的部署步骤：

- 从 `streamingScope: "dm"` 开始
- 首先验证单聊 (DM) 环境
- 只有当你对群聊卡片的行为和补丁更新频率感到满意时，才切换到 `all`

## Webhook 模式

当必须由飞书来调用你的公共网关时，请使用 `webhook` 或 `hybrid` 模式。

示例:

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "mode": "webhook",
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "verificationToken": "YOUR_TOKEN",
      "webhookPath": "/feishu/events",
      "allowFrom": ["ou_YOUR_OPEN_ID"]
    }
  },
  "gateway": {
    "host": "0.0.0.0",
    "port": 18790
  }
}
```

操作注意事项：

- 除非你更改了 `host`, `port`, 或 `webhookPath`，否则回调 URL 为 `https://YOUR_HOST:18790/feishu/events`。
- 当前的 webhook 实现会验证 `verificationToken` 并处理飞书的 URL 验证挑战 (challenge)。
- 目前请在 webhook 模式下禁用飞书事件加密。长连接注册使用了 `encryptKey`，但 webhook 端的加密事件解密目前尚未集成在网关路径中。
- 去重 (Dedupe) 机制采用双层架构：内存 LRU 为第一层，SQLite 为第二层。
- Webhook 请求会立即返回确认 (acknowledge)，并异步路由处理任务。

## 手动冒烟测试清单

启动网关：

```bash
nanobot gateway --config ~/.nanobot-feishu/config.json
```

然后在飞书测试租户中运行此清单：

1. 在单聊 (DM) 中问机器人一个简单的问题。
2. 问一个严重依赖工具的问题，并确认当机器人在处理时卡片能够动态更新。
3. 发送 `/clear`，然后紧接着问一个新问题。
4. 发送 `/forget`，然后问一个原来必须依赖用户画像/摘要才能回答的问题（以验证遗忘生效）。
5. 在群聊中 `@提及` 机器人并确认其群组行为是正确的。
6. 如果 `groupSessionMode` 配置为 `shared`，验证同一个群组中的两个用户可以共享上下文。
7. 如果测试配置中的 `sessionTtlSeconds` 设置得很短，请等待其过期，并确认在 TTL 归档之后，下一次的单聊依然有效。

推荐用于测试的配置调整：

- 将 `sessionTtlSeconds` 设置为 `60` 以进行短时 TTL 的冒烟测试
- 在初步部署期间保持 `streamingScope` 为 `dm`
- 保持 `allowFrom` 为严格配置，直到第一次成功通过测试

## Webhook Curl 请求测试

验证 URL 的挑战 (Challenge) 请求：

```bash
curl -sS -X POST "http://127.0.0.1:18790/feishu/events" \
  -H 'content-type: application/json' \
  -d '{
    "type": "url_verification",
    "token": "YOUR_TOKEN",
    "challenge": "test-challenge"
  }'
```

预期响应结果：

```json
{"challenge":"test-challenge"}
```

简单的 Webhook 事件接收测试：

```bash
curl -sS -X POST "http://127.0.0.1:18790/feishu/events" \
  -H 'content-type: application/json' \
  -d '{
    "header": {
      "event_id": "evt_local_smoke_1",
      "event_type": "im.message.receive_v1",
      "tenant_key": "tenant-local",
      "token": "YOUR_TOKEN"
    },
    "event": {
      "sender": {
        "sender_type": "user",
        "sender_id": {"open_id": "ou_test_user"}
      },
      "message": {
        "message_id": "om_local_smoke_1",
        "chat_id": "ou_test_user",
        "chat_type": "p2p",
        "message_type": "text",
        "content": "{\"text\":\"hello from curl\"}"
      }
    }
  }'
```

预期响应结果：

```json
{"code":0}
```

再次重复完全相同的一次请求，以快速检查去重 (dedupe) 机制的运行情况。

## 故障排除 (Troubleshooting)

- 单聊中收不到回复:
  - 检查验证 `allowFrom` 配置
  - 确认机器人应用已经发布，并且权限与事件订阅已经开启
  - 在网关日志中搜索你的 `open_id`
- 流式卡片没有更新:
  - 确认 `channels.sendProgress` 值为 `true`
  - 确认 `streamingScope` 包含了当前的聊天类型
- 卡片中没有显示工具提示的行:
  - 确认 `channels.sendToolHints` 值为 `true`
- Webhook 挑战请求失败:
  - 检查验证 `verificationToken`
  - 确认飞书后台填写的 Callback URL 与 `webhookPath` 完全一致
- Webhook 事件到达但内容看似报错/错误解读:
  - 确保暂时关闭了 webhook 的事件加密机制

## 部署推进建议

- 从 `websocket` 模式加上单聊 (DM) 开始测试。
- 在飞书测试租户中验证 `/clear`, `/forget`, 短期记忆 TTL 过期以及流式卡片更新。
- 然后扩展支持到群聊环境。
- 只有在你需要公网地址用来接收回调入口时，才考虑切换到 `webhook` 或 `hybrid` 模式。
