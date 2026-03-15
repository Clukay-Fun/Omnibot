# 服务器部署检查清单

这份清单面向第一次把 Ominibot 部署到服务器上的场景，默认前提是：

- Ubuntu 22.04 / 24.04
- 源码部署
- `systemd` 托管
- Feishu `websocket`
- 单实例

本文默认采用这一组固定路径：

- `SERVICE_USER=nanobot`
- `APP_DIR=/opt/ominibot`
- `HOME_DIR=/home/nanobot`
- `CONFIG_PATH=/home/nanobot/.nanobot/config.json`

## 部署前

- [ ] 已有一台可登录的 Linux 服务器
- [ ] 已创建非 root 运行用户
- [ ] 已拉取仓库代码到固定目录，例如 `/opt/ominibot`
- [ ] 已创建 Python 虚拟环境并完成 `pip install -e .`
- [ ] 已执行 `nanobot onboard`
- [ ] 已填写 `~/.nanobot/config.json`
- [ ] 已执行 `chmod 600 ~/.nanobot/config.json`

如果你要从零开始准备服务器，推荐直接执行：

```bash
sudo useradd -m -s /bin/bash nanobot || true
sudo mkdir -p /opt
sudo chown -R nanobot:nanobot /opt
```

然后切到运行用户：

```bash
sudo -iu nanobot
```

在服务器上拉代码并安装：

```bash
git clone <your-ominibot-repo-url> /opt/ominibot
cd /opt/ominibot
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
nanobot onboard
chmod 600 /home/nanobot/.nanobot/config.json
```

## 配置检查

- [ ] `channels.feishu.enabled = true`
- [ ] `channels.feishu.mode = websocket`
- [ ] `channels.feishu.appId` 已填写
- [ ] `channels.feishu.appSecret` 已填写
- [ ] `agents.defaults.model` 已填写
- [ ] `channels.sendProgress = true`
- [ ] `channels.sendToolHints = true`
- [ ] `channels.feishu.streamingScope = dm`
- [ ] `channels.feishu.allowFrom` 已确认

## 启动前预检

推荐先运行预检脚本：

```bash
bash /opt/ominibot/deploy/scripts/preflight-feishu.sh /opt/ominibot /home/nanobot/.nanobot/config.json
```

确认：

- [ ] JSON 配置能解析
- [ ] model 不为空
- [ ] Feishu 配置完整
- [ ] `nanobot status` 能正常输出

## 第一次前台冒烟

不要一开始就直接后台托管，先前台确认：

```bash
/opt/ominibot/.venv/bin/nanobot gateway --config /home/nanobot/.nanobot/config.json -v
```

然后在飞书里做最小验证：

- [ ] 发一条简单问候
- [ ] 发一条需要联网或工具的问题
- [ ] 发一条需要 skill 的问题

## 切到 systemd

先安装 service：

```bash
sudo SERVICE_USER=nanobot APP_DIR=/opt/ominibot HOME_DIR=/home/nanobot \
  bash /opt/ominibot/deploy/scripts/install-systemd-service.sh
```

再启动：

```bash
sudo systemctl start nanobot-gateway
sudo systemctl status nanobot-gateway
journalctl -u nanobot-gateway -f
```

确认：

- [ ] 服务能启动
- [ ] 服务重启后仍能正常连上飞书
- [ ] 日志没有持续报错

## 上线后

- [ ] 将 `allowFrom` 从 `["*"]` 收紧到你的实际 `open_id`
- [ ] 备份整个 `~/.nanobot/`
- [ ] 记录当前部署目录、配置路径和服务名
- [ ] 确认以后更新使用同一套路径和用户
