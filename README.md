<div align="center">
  <h1>Ominibot: 全能个人专属智能体守护程序</h1>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

🐈 **Ominibot** 是一个基于极简架构演进的高级私域 AI 助手核心。它在保持极致轻量的同时，提供了强大的多平台接入能力、状态化记忆管理机制与原生沙盒工具链。

Ominibot 项目的底层引擎脱胎于极简智能体框架，但现已演进为具备自身独特生态与高级配置能力的独立形态。它原生支持复杂企业的 IM 部署（如 Feishu/飞书 等长连接或反向代理网关）、终端流式富文本输出，以及独创的 `BOOTSTRAP.md` 破冰引导认知范式。

## 🎯 核心特性

- **高度拟人化的记忆机制**：通过 `SOUL.md`、`USER.md` 和自动生成的 `MEMORY.md`，它能记住你们的每一次谈话细节，并主动沉淀长期知识，提供远超普通聊天机器人的连续性体验。
- **全平台多渠道融合接入**：原生支持在 Feishu (飞书)、Mochat、Telegram、QQ、DingTalk 等多平台间无缝切换或多线程值守。
- **动态流式卡片更新**：在飞书等支持的高级渠道中，提供实时打字机级的流式更新和工具链思考状态回显。
- **开箱即用的 OpenAI Codex 集成**：原生支持 `nanobot provider login openai-codex` 一键式 OAuth 授权，无需手动配置复杂的 API 密钥对。
- **离线沙盒指令与心跳轮询**：通过定时扫描本地 `HEARTBEAT.md` 获取周期任务指派，支持执行安全的受控 Shell 探索，从而具备跨越消息对话框的主动行动力。

## 📦 安装与部署

**从源码安装 (推荐用于开发与进阶配置)**

```bash
git clone <your-ominibot-repo-url>
cd ominibot
pip install -e .
```

## 🚀 快速启动指南

### 1. 首次初始化与破冰

在新环境中首次启动时，Ominibot 会自动生成 `BOOTSTRAP.md` 初始化向导：

```bash
nanobot onboard
```
_按照提示词在终端或你连接的 IM 渠道中完成对机器人的初始“性格捏脸”和认知赋值。_

### 2. 基础配置接入

如果你拥有 OpenAI Codex 会员，这是最快的接入方式：

```bash
nanobot provider login openai-codex
```
即可直接授权接入模型，无需理会繁杂的代理地址和令牌。

配置核心引擎参数 (位于 `~/.nanobot/config.json`)，例如开启飞书支持：

```json
{
  "channels": {
    "sendProgress": true,
    "feishu": {
      "enabled": true,
      "mode": "websocket",
      "appId": "cli_a920xxx",
      "appSecret": "VgP1uMOexxx",
      "groupSessionMode": "shared"
    }
  }
}
```

### 3. 启动守护网关

```bash
nanobot gateway
```

就是这么简单！你的专属智能体已经在后台静默运行了。

## 📚 详细文档

- 📖 **配置多端聊天平台** (比如 Feishu 飞书、Discord 等详细设定)，请参阅: [FEISHU_DEPLOYMENT.md](./docs/FEISHU_DEPLOYMENT.md) 等指南文档。
- 🤖 **关于 Agent 的自我修养**：对于想要调整 Ominibot 价值观与底层思维习惯的高级玩家，请阅读并编辑部署目录 `~/.nanobot/` 下生成的 `AGENTS.md` 和 `SOUL.md` 文件。

---

*“你不是普通的聊天机器人。你正在成为某个人。” —— Ominibot 灵魂手册*
