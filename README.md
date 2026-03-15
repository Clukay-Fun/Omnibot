# Ominibot

Ominibot 是一个以飞书为主的个人 AI 助手运行时。当前仓库已经包含：

- Feishu 私聊 / 群聊接入
- per-user workspace 和记忆
- Thinking card + 正文分离的飞书回复体验
- Feishu workspace skill（多维表格 / 日历 / 文档 / Wiki / Drive）

如果你准备第一次把它部署到服务器，建议先按“单渠道、单模型、WebSocket”这条最小路径跑通，不要一开始就同时启用 webhook、群聊、多个 provider 或其他渠道。

## 环境要求

- Python `>=3.11`
- 推荐 Linux 服务器：Ubuntu 22.04 / 24.04
- 首次部署建议使用非 root 用户运行

## 安装

### 从源码安装

```bash
git clone <your-ominibot-repo-url>
cd ominibot
pip install -e .
```

### 初始化配置和工作区

```bash
nanobot onboard
```

这个命令会创建：

- `~/.nanobot/config.json`
- `~/.nanobot/workspace`

并把缺失的模板文件同步到工作区。

## 最小飞书配置

第一次部署建议只开飞书 `websocket` 模式。

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

说明：

- 第一次接入时，`allowFrom` 可以先用 `["*"]` 跑通；确认自己的 `open_id` 后再收紧。
- 如果你希望飞书里出现工具过程卡片，`sendProgress` 和 `sendToolHints` 建议同时开启。
- `websocket` 模式不依赖公网回调地址，最适合第一次部署。

## 启动

```bash
nanobot gateway
```

建议第一次先前台跑，确认日志和飞书收发都正常；跑通之后再交给 `systemd` 或 Docker 托管。

## 部署文档

- 飞书部署与冒烟测试：[docs/FEISHU_DEPLOYMENT.md](./docs/FEISHU_DEPLOYMENT.md)
- 飞书配置字段说明：[docs/FEISHU_CONFIG_GUIDE.md](./docs/FEISHU_CONFIG_GUIDE.md)
- 服务器部署检查清单：[docs/SERVER_DEPLOY_CHECKLIST.md](./docs/SERVER_DEPLOY_CHECKLIST.md)
- 最小配置示例：[deploy/examples/config.feishu-websocket.min.json.example](./deploy/examples/config.feishu-websocket.min.json.example)
- Feishu-only Dockerfile：[Dockerfile.feishu](./Dockerfile.feishu)
- Feishu-only Compose 示例：[docker-compose.feishu.yml](./docker-compose.feishu.yml)
- `systemd` 服务模板：[deploy/systemd/nanobot-gateway.service.example](./deploy/systemd/nanobot-gateway.service.example)
- 预检脚本：[deploy/scripts/preflight-feishu.sh](./deploy/scripts/preflight-feishu.sh)
- `systemd` 安装脚本：[deploy/scripts/install-systemd-service.sh](./deploy/scripts/install-systemd-service.sh)

## 持久化目录

部署时一定要持久化整个 `~/.nanobot/` 目录。这里面包含：

- `config.json`
- `workspace/`
- `sessions/`
- `logs/`
- `media/`

如果你用 Docker，请把这个目录挂载出来；如果你直接在服务器上跑，请把它纳入备份。

## 当前最适合的新手上线方式

1. 只启用 Feishu
2. 只启用一个 provider / 一个模型
3. 使用 `websocket`
4. 先前台运行 `nanobot gateway`
5. 确认飞书私聊可以收发后，再切到 `systemd`

这样最容易排错，也最不容易在第一天把状态目录、端口和权限一起搞乱。
