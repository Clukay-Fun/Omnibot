# 飞书服务器部署实战手册

这份文档记录这次从零部署到阿里云轻量服务器的真实过程，重点放在实际可用的命令、踩过的坑、以及后续更新时该怎么做。

适用前提：

- Ubuntu 24.04
- 阿里云轻量应用服务器
- `systemd` 托管
- 飞书 `websocket` 模式
- OpenAI Codex OAuth
- 本机 `mihomo` 代理

本文统一使用这些路径：

- 项目目录：`/opt/omnibot`
- 运行用户：`nanobot`
- 配置文件：`/home/nanobot/.nanobot/config.json`
- 代理配置：`/etc/mihomo/config.yaml`

## 一、这次最终跑通的架构

- `mihomo` 作为本机代理，监听：
  - `127.0.0.1:7890` (`HTTP_PROXY` / `HTTPS_PROXY`)
  - `127.0.0.1:7891` (`ALL_PROXY`, socks5)
- `nanobot-gateway` 通过 `systemd` 启动，并继承代理环境变量
- 飞书通道使用 `websocket`，不依赖公网 webhook 回调
- 模型使用 `gpt-5.2-codex`，服务器直接复用本地已有的 Codex OAuth token

## 二、服务器初始化

### 1. 更换 apt 国内源

```bash
sudo cp /etc/apt/sources.list /etc/apt/sources.list.bak
sudo tee /etc/apt/sources.list >/dev/null <<'EOF'
deb https://mirrors.aliyun.com/ubuntu/ noble main restricted universe multiverse
deb https://mirrors.aliyun.com/ubuntu/ noble-updates main restricted universe multiverse
deb https://mirrors.aliyun.com/ubuntu/ noble-backports main restricted universe multiverse
deb https://mirrors.aliyun.com/ubuntu/ noble-security main restricted universe multiverse
EOF
sudo apt update
```

### 2. 安装基础依赖

```bash
sudo apt install -y git curl ca-certificates python3 python3-venv python3-pip
```

### 3. 创建运行用户和目录

```bash
sudo useradd -m -s /bin/bash nanobot
sudo mkdir -p /opt/omnibot
sudo chown -R nanobot:nanobot /opt/omnibot
```

## 三、代码拉取与 Python 安装

### 1. 拉 GitHub 代码

大陆环境下建议直接强制 HTTP/1.1：

```bash
sudo -u nanobot -H git -c http.version=HTTP/1.1 clone --depth 1 --single-branch https://github.com/Clukay-Fun/Omnibot.git /opt/omnibot
```

### 2. 创建虚拟环境

```bash
sudo -u nanobot -H python3 -m venv /opt/omnibot/.venv
```

### 3. 用国内 pip 源升级 pip

```bash
sudo -u nanobot -H /opt/omnibot/.venv/bin/pip install -U pip -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 4. 安装项目

```bash
sudo -u nanobot -H bash -lc 'cd /opt/omnibot && /opt/omnibot/.venv/bin/pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple'
```

## 四、第一次踩到的坑

### 1. `bridge` 打包配置导致安装失败

报错特征：

```text
FileNotFoundError: Forced include not found: /opt/omnibot/bridge
```

根因：

- 当前分支已经没有 `bridge/` 目录
- 但 `pyproject.toml` 里还保留了旧的打包配置

最终修复：

- 从 `pyproject.toml` 删除 `bridge/` 的 `sdist` 和 `force-include`
- 这个修复已提交到仓库，服务器更新后会自动带上

### 2. 服务器不能直接访问 GitHub / chatgpt.com

现象：

- `git clone` 或 submodule 拉取超时
- `curl https://chatgpt.com` 超时
- 飞书消息收到了，但模型请求卡在 `connect_tcp.started host='chatgpt.com'`

根因：

- 大陆服务器不能直连上游

最终方案：

- 在服务器本机部署 `mihomo`
- 给 `nanobot-gateway` 注入代理环境变量

### 3. OpenAI Codex OAuth 不能在服务器网页直接完成

现象：

- `nanobot provider login openai-codex` 跳浏览器后报 `missing_required_parameter`
- 或跳转 `localhost` 后无法回调

最终方案：

- 直接复用本地已登录成功的 token 文件
- 本地 token 来源：`~/Library/Application Support/oauth-cli-kit/auth/codex.json`
- 服务器放到：`/home/nanobot/.local/share/oauth-cli-kit/auth/codex.json`

验证命令：

```bash
sudo -u nanobot -H /opt/omnibot/.venv/bin/python -c 'from datetime import datetime; from oauth_cli_kit import get_token; token = get_token(); print("account_id:", token.account_id); print("expires_at:", datetime.fromtimestamp(token.expires / 1000).isoformat())'
```

## 五、运行配置

### 1. 初始化配置目录

```bash
sudo -u nanobot -H bash -lc 'cd /opt/omnibot && /opt/omnibot/.venv/bin/nanobot onboard'
sudo chmod 700 /home/nanobot/.nanobot /home/nanobot/.nanobot/workspace
sudo chmod 600 /home/nanobot/.nanobot/config.json
```

### 2. 当前有效配置要点

- `channels.feishu.enabled = true`
- `channels.feishu.mode = "websocket"`
- `channels.feishu.allowFrom = ["*"]`
- `agents.defaults.model = "gpt-5.2-codex"`
- `agents.defaults.provider = "openai_codex"`
- `sendProgress = true`
- `sendToolHints = true`

### 3. 预检

```bash
sudo -u nanobot -H bash /opt/omnibot/deploy/scripts/preflight-feishu.sh /opt/omnibot /home/nanobot/.nanobot/config.json
```

## 六、代理部署（mihomo）

### 1. 安装 mihomo

```bash
curl -L "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.21/mihomo-linux-amd64-compatible-v1.19.21.gz" -o /tmp/mihomo.gz && gzip -dc /tmp/mihomo.gz | sudo tee /usr/local/bin/mihomo >/dev/null && sudo chmod +x /usr/local/bin/mihomo
```

### 2. 放置订阅配置

```bash
sudo mkdir -p /etc/mihomo
curl -fsSL '<你的 clash 订阅链接>' -o /tmp/mihomo-config.yaml
sudo mv /tmp/mihomo-config.yaml /etc/mihomo/config.yaml
sudo chmod 600 /etc/mihomo/config.yaml
```

### 3. 下载 `country.mmdb`

如果 `mihomo` 启动时报 `can't download MMDB`，手动执行：

```bash
sudo curl -L --http1.1 'https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/country.mmdb' -o /etc/mihomo/country.mmdb
```

### 4. `systemd` 服务文件

`/etc/systemd/system/mihomo.service`：

```ini
[Unit]
Description=Mihomo Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/mihomo -d /etc/mihomo -f /etc/mihomo/config.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mihomo
sudo systemctl status mihomo --no-pager
```

### 5. 代理连通性验证

```bash
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 ALL_PROXY=socks5://127.0.0.1:7891 curl -I --max-time 20 https://chatgpt.com
```

`HTTP/2 403` + `cf-mitigated: challenge` 是正常的，这说明代理已经通到 `chatgpt.com`。

## 七、`nanobot-gateway` 托管

### 1. 当前生效的 service 关键点

`/etc/systemd/system/nanobot-gateway.service` 需要包含：

```ini
[Unit]
Description=Nanobot Gateway
After=network-online.target mihomo.service
Wants=network-online.target
Requires=mihomo.service

[Service]
Type=simple
User=nanobot
Group=nanobot
WorkingDirectory=/opt/omnibot
Environment="HOME=/home/nanobot"
Environment="HTTP_PROXY=http://127.0.0.1:7890"
Environment="HTTPS_PROXY=http://127.0.0.1:7890"
Environment="ALL_PROXY=socks5://127.0.0.1:7891"
Environment="NO_PROXY=127.0.0.1,localhost"
ExecStart=/opt/omnibot/.venv/bin/nanobot gateway --config /home/nanobot/.nanobot/config.json
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

### 2. 启动与验证

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nanobot-gateway
sudo systemctl status nanobot-gateway --no-pager
```

## 八、实时日志怎么查

机器人日志：

```bash
sudo journalctl -u nanobot-gateway -f
```

代理日志：

```bash
sudo journalctl -u mihomo -f
```

看最近日志：

```bash
sudo journalctl -u nanobot-gateway -n 50 --no-pager
```

## 九、这次成功的关键验证信号

### 飞书连接成功

日志里看到：

```text
Feishu bot started in websocket mode
connected to wss://msg-frontier.feishu.cn/
```

### 消息收到并回复成功

日志里看到：

```text
Processing message from feishu:ou_xxx: 你好
Response to feishu:ou_xxx: 你好！我是小敬 🙂
```

### 服务启动正常

```bash
sudo systemctl status nanobot-gateway --no-pager
sudo systemctl status mihomo --no-pager
```

都应显示：

```text
Active: active (running)
```

## 十、后续更新流程

### 1. 一键更新（推荐）

新增了一键更新脚本：`scripts/ops/update-server.sh`

默认行为：

- 默认项目目录：`/opt/omnibot`
- 默认分支：`dev/upstream-clean-main`
- 默认服务用户：`nanobot`
- 默认服务名：`nanobot-gateway`
- 检测到本地脏工作区时直接停止，不自动 stash
- 更新前打印旧 commit 和回滚命令
- 自动继承当前 shell 的 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 给 `fetch`、`pull` 和 `submodule`

无代理环境：

```bash
sudo bash /opt/omnibot/scripts/ops/update-server.sh
```

有代理环境：

```bash
sudo HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 ALL_PROXY=socks5://127.0.0.1:7891 \
  bash /opt/omnibot/scripts/ops/update-server.sh
```

如果要更新到别的分支：

```bash
sudo bash /opt/omnibot/scripts/ops/update-server.sh feature/some-branch
```

### 2. 标准更新命令（手动）

```bash
sudo -u nanobot -H git -C /opt/omnibot fetch --all --tags
sudo -u nanobot -H git -C /opt/omnibot checkout dev/upstream-clean-main
sudo -u nanobot -H git -C /opt/omnibot pull --ff-only
sudo -u nanobot -H env HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 git -c http.version=HTTP/1.1 -C /opt/omnibot submodule update --init --recursive --depth 1
sudo -u nanobot -H bash -lc 'cd /opt/omnibot && /opt/omnibot/.venv/bin/pip install -e .'
sudo systemctl restart nanobot-gateway
sudo systemctl status nanobot-gateway --no-pager
```

### 3. 如果 `pyproject.toml` 之类有本地改动挡住 `pull`

先确认是不是历史手工修补残留：

```bash
sudo -u nanobot -H git -C /opt/omnibot status --short
sudo -u nanobot -H git -C /opt/omnibot diff -- pyproject.toml
```

如果只是旧修补、远端已经带上修复，可以直接 stash：

```bash
sudo -u nanobot -H git -C /opt/omnibot stash push -m "temp-before-update" -- pyproject.toml
sudo -u nanobot -H git -C /opt/omnibot pull --ff-only
```

### 4. 更新后确认版本

```bash
sudo -u nanobot -H git -C /opt/omnibot rev-parse --short HEAD
sudo -u nanobot -H git -C /opt/omnibot submodule status
```

## 十一、临时 SSH Key 协作流程

如果需要临时让本地机器免密协助服务器维护：

### 1. 本地生成临时 key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/opencode_temp -N ""
cat ~/.ssh/opencode_temp.pub
```

### 2. 服务器追加公钥

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
printf '%s\n' '<你的公钥整行>' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
```

### 3. 本地验证

```bash
ssh -i ~/.ssh/opencode_temp -o IdentitiesOnly=yes admin@ominiagent.online "echo temp-ssh-ok"
```

### 4. 用完即删

服务器移除公钥后，本地也删除：

```bash
rm ~/.ssh/opencode_temp ~/.ssh/opencode_temp.pub
```

## 十二、建议长期保留的习惯

- 所有运行路径固定，不再混用 `/opt/ominibot` 和 `/opt/omnibot`
- `nanobot-gateway` 始终依赖 `mihomo`
- 每次更新后先看 `systemctl status`，再看 `journalctl -f`
- 需要访问 GitHub 子模块时，默认让 `git` 走代理
- 首次验证优先 DM，不要一上来就做群聊或复杂广播
