# 飞书部署指南

这份文档按“第一次上服务器也能照着走”的标准写，只覆盖当前仓库已经支持、并且适合生产最小上线的路径。

## 先定一个最小部署目标

第一次部署建议固定成这一组：

- 只启用 Feishu
- 只启用一个模型 provider
- 飞书模式使用 `websocket`
- 先验证私聊（DM）
- 群聊、webhook、hybrid、更多渠道都放到第二阶段

这样问题会少很多，也更容易确认到底是配置问题、网络问题，还是模型/工具问题。

## 1. 服务器准备

推荐环境：

- Ubuntu 22.04 / 24.04
- 非 root 用户运行，例如 `nanobot`
- Python 3.11 或 3.12

安装基础工具：

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

如果你打算用 `systemd` 托管，后面不需要额外安装别的东西。

## 2. 拉代码并安装

```bash
git clone <your-ominibot-repo-url> /opt/ominibot
cd /opt/ominibot
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## 3. 初始化 `~/.nanobot`

```bash
nanobot onboard
```

这个命令会创建：

- `~/.nanobot/config.json`
- `~/.nanobot/workspace`

并自动补齐缺失的模板文件。

## 4. 填最小配置

推荐的最小配置：

```json
{
  "agents": {
    "defaults": {
      "workspace": "~/.nanobot/workspace",
      "model": "gpt-5.2-codex",
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
      "streamingScope": "dm",
      "streamThrottleSeconds": 0.5
    }
  }
}
```

如果你想直接从现成模板开始填，可以复制：

- [deploy/examples/config.feishu-websocket.min.json.example](../deploy/examples/config.feishu-websocket.min.json.example)

关键说明：

- `mode = websocket`
  这是第一次部署最稳的模式，不需要公网回调地址。
- `allowFrom = ["*"]`
  仅建议用在第一次接入和引导阶段。确认自己的 `open_id` 后再收紧。
- `sendProgress = true`
  开启过程消息输出。
- `sendToolHints = true`
  如果你想在飞书里看到工具/skill 过程卡片，建议开启。
- `streamingScope = "dm"`
  先只在私聊验证。群聊上线放后面。

## 5. 配置文件权限

`config.json` 里会放 API key 和飞书密钥，至少要收成：

```bash
chmod 600 ~/.nanobot/config.json
```

并且尽量用非 root 用户运行进程。

## 6. 第一次启动：先前台跑

第一次不要直接上后台服务，先前台看日志：

```bash
nanobot gateway -v
```

然后在飞书里做 3 个最小冒烟测试：

1. 私聊发一句简单问候，例如 `你好`
2. 发一句需要联网/工具的问题
3. 发一句需要 skill 或文件操作的问题

只要这三类都正常，再切换到托管运行。

## 7. 当前飞书回复机制

当前 Feishu 回复是“两条消息”模型：

- 第一条：只有在真的出现工具/搜索进度时，才创建 `interactive` thinking card
- 第二条：最终正文单独发送，使用正式回复

这意味着：

- 普通快回复通常不会看到思考卡片
- 真有工具、联网或 skill 过程时，才会看到卡片中的步骤

如果你看到“只有正文，没有过程卡片”，先判断这轮是否真的发生了工具调用，不要把“没有过程”误认为“过程显示坏了”。

## 8. `systemd` 托管

推荐第一次上线用 `systemd`，比 Docker 更容易看日志和排错。

模板文件在：

- [deploy/systemd/nanobot-gateway.service.example](../deploy/systemd/nanobot-gateway.service.example)

使用方式：

1. 复制到 `/etc/systemd/system/nanobot-gateway.service`
2. 按你的实际路径修改 `User`、`WorkingDirectory`、`ExecStart`
3. 启用并启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable nanobot-gateway
sudo systemctl start nanobot-gateway
sudo systemctl status nanobot-gateway
```

查看日志：

```bash
journalctl -u nanobot-gateway -f
```

## 9. Docker 什么时候用

仓库里有现成的 [docker-compose.yml](../docker-compose.yml)，但第一次部署我不优先推荐它，原因很简单：

- 你更难直接看 Python 运行时日志
- 当前镜像会额外安装 Node 并构建 bridge
- 如果你现在只上 Feishu，这一步不是最小复杂度

如果你已经熟悉 Docker，可以继续用；如果你是第一次上服务器，先把源码版跑通更稳。

另外，若你只使用 Feishu `websocket` 模式，通常不需要把 `18790` 端口暴露到公网。

如果你确实想直接用 Docker，建议优先使用这套 Feishu-only 示例，而不是当前全量镜像：

- [Dockerfile.feishu](../Dockerfile.feishu)
- [docker-compose.feishu.yml](../docker-compose.feishu.yml)

这套示例的设计目标是：

- 不构建 WhatsApp bridge
- 不安装 Node
- 不默认暴露 `18790`
- 只保留 Feishu `websocket` 最小运行路径

启动方式：

```bash
docker compose -f docker-compose.feishu.yml up -d --build
```

查看日志：

```bash
docker compose -f docker-compose.feishu.yml logs -f
```

如果后面你要改成 `webhook` 或 `hybrid`，再单独补端口映射和公网入口，不要在第一次部署时一并处理。

## 10. 持久化和备份

一定要把整个 `~/.nanobot/` 当成持久化目录处理，至少要备份：

- `~/.nanobot/config.json`
- `~/.nanobot/workspace/`
- `~/.nanobot/sessions/`

如果这些丢了，你不仅会丢配置，还会丢记忆、会话历史和用户工作区数据。

## 11. 常见问题

### 飞书收不到回复

- 检查 `appId` / `appSecret`
- 检查应用是否已发布
- 检查事件权限和机器人可见范围
- 检查 `allowFrom`

### 能回复，但工具过程卡片不出现

- 检查 `sendProgress = true`
- 检查 `sendToolHints = true`
- 确认这轮真的发生了工具调用或搜索

### 第一次就想上群聊

不建议。先把私聊跑通。群聊会把：

- 提及规则
- 会话隔离
- 卡片频率
- 白名单

这些问题一起放大。

## 12. 上线前最后核对

上线前至少确认这 8 项：

- 只启用了 Feishu
- 只启用了一个 provider / 一个模型
- `mode = websocket`
- `config.json` 权限是 `600`
- 进程不是 root 跑的
- 已经前台跑通过一次
- 已经做过 3 条飞书冒烟测试
- 已经决定如何备份 `~/.nanobot/`

如果你照这个顺序来，第一次上线会稳很多。
