# 飞书配置说明书

这份说明书专门解释 Nanobot 飞书接入相关的配置字段、可填参数、默认值、推荐值，以及首次接入时最容易踩坑的地方。

如果你只是想先把服务器跑起来，先看这个最小模板：

- [deploy/examples/config.feishu-websocket.min.json.example](../deploy/examples/config.feishu-websocket.min.json.example)

## 先说结论

如果你现在的目标是：

- 先把机器人跑起来
- 先完成第一次对话和 `BOOTSTRAP.md` 引导
- 群聊上下文默认共享

建议起步配置如下：

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.nanobot/workspace",
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
      "allowFrom": ["*"],
      "groupSessionMode": "shared",
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

## `allowFrom` 到底填什么

`allowFrom` 填的是飞书用户的 `open_id`，不是昵称，也不是 chat_id。

常见写法：

- `[]`：拒绝所有人
- `["*"]`：允许所有人
- `["ou_xxx", "ou_yyy"]`：只允许指定飞书用户

### 为什么你现在适合先写 `["*"]`

因为当前系统的首次引导流程是：

1. 消息先通过 ACL
2. 然后才进入 `BOOTSTRAP.md` 触发第一次对话引导

这意味着：

- 如果你一开始就把 `allowFrom` 写成精确白名单
- 但你还不知道自己的 `open_id`
- 那第一条消息根本进不来，`BOOTSTRAP.md` 也不会运行

所以你的当前选择 `allowFrom: ["*"]` 是合理的。

### 能不能第一次对话后自动把 `open_id` 回写到 config？

当前实现里不建议这样做，原因有三个：

1. 需要运行时改配置文件
2. 改完通常还需要重启网关才能生效
3. 会把“权限控制”和“聊天引导”耦合在一起，后续维护成本更高

更稳妥的做法是：

1. 先用 `allowFrom: ["*"]`
2. 完成首次 BOOTSTRAP 引导
3. 从日志或事件数据里拿到自己的 `open_id`
4. 再把 `allowFrom` 收紧成显式白名单

## `groupSessionMode` 该选什么

### 可选值

- `shared`
- `per_user`

### 现在的默认值

当前默认值已经改成：

- `shared`

### 两种模式的区别

#### `shared`

短期会话键：

- `feishu:chat:{chat_id}`

特点：

- 同一个群里所有人共享一份短期上下文
- 更像“群助手”
- 适合项目群、会议群、运维群、协作群
- 你前面一句、我后面一句，机器人能接着理解

风险：

- 上下文会互相污染
- 一个人的问题可能会影响另一个人的对话连续性

#### `per_user`

短期会话键：

- `feishu:chat:{chat_id}:user:{user_open_id}`

特点：

- 同一个群里每个人各自拥有独立短期上下文
- 更像“群里每个人各自的私人助理”

适合：

- 群里并发提问很多
- 彼此问题关系不大
- 不希望上下文互相影响

### 你现在该选什么

你说“群助手还是要共享的，但是目前群聊用处不大”，那就保持：

- `groupSessionMode: "shared"`

这是符合你当前目标的。

## 多人群聊时，记忆怎么存

这个要分“短期记忆”和“长期记忆”看。

### 短期记忆

由 `groupSessionMode` 决定：

- `shared`：整个群共用一份短期上下文
- `per_user`：每个人各有一份短期上下文

### 长期记忆

无论群聊是否共享，长期记忆仍然是“按人存”的：

- 存储键：`tenant_key + user_open_id`

结构固定为：

- `profile`
- `summary`

注入策略固定为：

- 单聊注入 `profile + summary`
- 群聊只注入 `profile`

所以即使你把群聊改成 `shared`，也不会把所有人的长期记忆混成一锅。共享的只有短期上下文，不是长期画像库。

## 字段说明总表

下面这份表按“最常需要改”的顺序来写。

### 一、全局相关字段

#### `agents.defaults.workspace`

- 类型：`string`
- 示例：`"~/.nanobot/workspace"`
- 作用：工作区目录，里面会有 `BOOTSTRAP.md`、`USER.md`、记忆文件等
- 推荐：第一次部署直接使用默认 `~/.nanobot/workspace`，先把单实例跑通；多实例时再拆分不同 workspace

#### `agents.defaults.memoryWindow`

- 类型：`int`
- 常见值：`50`、`100`、`150`
- 默认：`100`
- 作用：短期上下文窗口大小，也会影响窗口溢出归档触发点
- 推荐：先用 `100`

#### `channels.sendProgress`

- 类型：`bool`
- 可填：`true | false`
- 默认：`true`
- 作用：是否允许把 AgentLoop 的阶段进度发到通道层
- 推荐：飞书流式卡片要开，所以建议 `true`

#### `channels.sendToolHints`

- 类型：`bool`
- 可填：`true | false`
- 默认：`false`
- 作用：是否把工具调用提示一起发到通道层
- 推荐：如果你想让卡片里显示“正在执行 read_file / web_search”，就开 `true`

#### `gateway.host`

- 类型：`string`
- 常见值：`"0.0.0.0"`、`"127.0.0.1"`
- 默认：`"0.0.0.0"`
- 作用：网关监听地址
- 推荐：本机调试可用 `127.0.0.1`，部署常用 `0.0.0.0`

#### `gateway.port`

- 类型：`int`
- 默认：`18790`
- 作用：`nanobot gateway` 的监听端口
- 推荐：单实例保持默认，多实例时换端口

### 二、飞书专属字段

#### `channels.feishu.enabled`

- 类型：`bool`
- 可填：`true | false`
- 默认：`false`
- 作用：是否启用飞书通道

#### `channels.feishu.mode`

- 类型：`string`
- 可填：`"websocket" | "webhook" | "hybrid"`
- 默认：`"websocket"`
- 作用：选择飞书入口模式
- 推荐：优先 `websocket`

#### `channels.feishu.appId`

- 类型：`string`
- 作用：飞书应用 App ID
- 必填：是

#### `channels.feishu.appSecret`

- 类型：`string`
- 作用：飞书应用 App Secret
- 必填：是

#### `channels.feishu.verificationToken`

- 类型：`string`
- 作用：webhook / hybrid 模式下用于校验请求
- `websocket` 是否必须：否

#### `channels.feishu.encryptKey`

- 类型：`string`
- 作用：飞书事件加密相关字段
- 现状说明：当前 webhook 路径不建议开启事件加密
- 推荐：先留空或按长连接要求配置，但 webhook 模式下先不要依赖它

#### `channels.feishu.webhookPath`

- 类型：`string`
- 默认：`"/feishu/events"`
- 作用：webhook 回调路径
- 推荐：不改也行，除非你需要和现有网关统一路径规范

#### `channels.feishu.allowFrom`

- 类型：`string[]`
- 可填：`[]`、`["*"]`、`["ou_xxx", "ou_yyy"]`
- 默认：`[]`
- 作用：飞书用户 ACL
- 当前推荐：`["*"]`
- 后续推荐：改成显式 `open_id` 白名单

#### `channels.feishu.groupSessionMode`

- 类型：`string`
- 可填：`"shared" | "per_user"`
- 默认：`"shared"`
- 作用：群聊短期上下文是否共享
- 当前推荐：`"shared"`

#### `channels.feishu.dedupeMemorySize`

- 类型：`int`
- 默认：`1000`
- 作用：内存层 LRU 去重容量
- 推荐：保持默认即可

#### `channels.feishu.dedupeDbPath`

- 类型：`string`
- 作用：SQLite 去重数据库路径
- 为空时：走 workspace 下默认路径
- 推荐：多实例部署时显式写出来，方便排查

#### `channels.feishu.memoryDbPath`

- 类型：`string`
- 作用：飞书长期记忆数据库路径
- 为空时：走 workspace 下默认路径
- 推荐：生产环境显式配置

#### `channels.feishu.sessionTtlSeconds`

- 类型：`int`
- 常见值：`0`、`3600`、`86400`
- 默认：`0`
- 作用：短期会话 TTL
- `0` 含义：关闭 TTL
- 推荐：你如果要保守一点，可以先设 `86400`

#### `channels.feishu.streamingScope`

- 类型：`string`
- 可填：`"off" | "dm" | "all"`
- 默认：`"dm"`
- 作用：流式卡片在哪些聊天类型生效
- 推荐：先 `dm`

#### `channels.feishu.streamThrottleSeconds`

- 类型：`float`
- 默认：`0.5`
- 作用：流式卡片 patch 合并窗口
- 推荐：保持 `0.5`

## 当前飞书流式输出行为

现在飞书不是“先点表情，再单独发消息”了，而是：

1. 收到你的消息
2. 以“回复这条原消息”的方式创建第一张流式卡片
3. 后续继续 patch 这张卡片
4. 最终回复也收束到这张卡片上
5. 如果 patch 失败，就降级成一条“回复原消息”的普通文本

也就是说：

- `reactEmoji` 已经从飞书配置中移除了
- 当前默认体验是“回复原消息 + 流式卡片更新”

## 首次接入建议流程

推荐按这个顺序来：

1. 配置 `allowFrom: ["*"]`
2. 配置 `groupSessionMode: "shared"`
3. 启动 `nanobot gateway`
4. 先在飞书单聊里发第一条消息，完成 BOOTSTRAP 引导
5. 再测试 `/clear`、`/forget`、工具型问题、流式卡片
6. 如果后面要收紧权限，再把 `allowFrom` 改成明确 `open_id` 列表

## 推荐场景搭配

### 个人优先、先跑通

```json
{
  "channels": {
    "sendProgress": true,
    "sendToolHints": true,
    "feishu": {
      "enabled": true,
      "mode": "websocket",
      "allowFrom": ["*"],
      "groupSessionMode": "shared",
      "streamingScope": "dm"
    }
  }
}
```

### 上线后收紧 ACL

```json
{
  "channels": {
    "feishu": {
      "allowFrom": ["ou_abc123", "ou_def456"],
      "groupSessionMode": "shared"
    }
  }
}
```

### 群里每个人互不影响

```json
{
  "channels": {
    "feishu": {
      "allowFrom": ["*"],
      "groupSessionMode": "per_user"
    }
  }
}
```

## 最后建议

基于你现在的目标，建议就按下面这组值开始：

- `allowFrom: ["*"]`
- `groupSessionMode: "shared"`
- `mode: "websocket"`
- `streamingScope: "dm"`
- `sendProgress: true`
- `sendToolHints: true`

等你把第一次引导、单聊问答、群聊共享、`/clear`、`/forget` 都跑顺之后，再决定要不要把 ACL 收紧到 `open_id` 白名单。
