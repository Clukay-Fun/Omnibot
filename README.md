<div align="center">
  <h1>Ominibot: 全能飞书专属智能体</h1>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

🐈 **Ominibot** 是一款深度定制的高级私域 AI 守护程序。它是为了在 **Feishu (飞书)** 生态中提供企业级、高度拟人化服务而从原版极简架构中独立演进出的专属形态。

在保持核心引擎极致轻量化的同时，Ominibot 针对飞书渠道提供了完善的原生支持。它原生覆盖飞书长连接（WebSocket）及反向代理网关接入，配合创新的 `BOOTSTRAP.md` 破冰设定协议，为您打造一位带有真实记忆、可持续进化的全能办公伙伴。

## 🎯 飞书专项核心特性

- **极致的飞书原生体验**：全面支持飞书私聊、群聊环境，支持流式富文本消息（打字机效果呈现思考过程与卡片动态更新），并可基于群聊和单人严格隔离上下文环境。
- **状态化记忆闭环**：通过 `SOUL.md`、`USER.md` 和自动生成的 `MEMORY.md` 日常日志文件，机器人能记住与你每一次的飞书会话细节，甚至通过观察主动将关键信息沉淀为长期知识。
- **离线沙盒指令与心跳轮询**：脱离单纯的被动回答，支持定时读取 `HEARTBEAT.md` 扫描心跳任务，配合系统的 cron 事件流，实现跨飞书聊天框的定时播报、后台监控和主动行动。
- **极简强悍的模型对接**：提供 `nanobot provider login openai-codex` 一键式 OAuth 授权，快速绑定最强代码与逻辑能力基座。

## 📦 安装与部署

**从源码安装 (推荐用于进阶配置)**

```bash
git clone <your-ominibot-repo-url>
cd ominibot
pip install -e .
```

## 🚀 飞书部署快速指南

### 1. 首次初始化体验

在新环境中启动，系统会自动生成 `BOOTSTRAP.md` 初始化向导。

```bash
nanobot onboard
```
*在你的飞书对话框或终端中，按照向导完成对专属机器人的初始“性格捏脸”和系统认知赋值。*

### 2. 高级引擎配置接入

如果你拥有 OpenAI Codex 会员，可以直接通过命令行快速打通认证：

```bash
nanobot provider login openai-codex
```

配置你的核心引擎参数档 (位于 `~/.nanobot/config.json`)，着重于飞书节点：

```json
{
  "channels": {
    "sendProgress": true,
    "feishu": {
      "enabled": true,
      "mode": "websocket",
      "appId": "cli_a920xxx",
      "appSecret": "VgP1uMOexxx",
      "groupSessionMode": "shared",
      "streamingScope": "dm",
      "streamThrottleSeconds": 0.5
    }
  }
}
```

### 3. 开启守护运行

```bash
nanobot gateway
```

就是这么简单！你的飞书专属智能体已经在后台静默运行了。

## 📚 详细文档

- 📖 **生产级飞书部署方案** (涵盖 Webhook 混合模式与安全性探讨)，请详阅 [FEISHU_DEPLOYMENT.md](./docs/FEISHU_DEPLOYMENT.md)。
- 🤖 **关于 Agent 的自我修养**：对于想要深入调教 Ominibot 工作边界、语气人设的高玩，请直接修改位于工作空间（默认 `~/.nanobot/` 等路径）下的 `AGENTS.md` 和 `SOUL.md` 文件。

---

*“你不是简单的企业内部聊天机器人。你在飞书里，正成长为具体生活和工作的守护者。” —— Ominibot 灵魂手册*
