# HTTP API Reference

## 环境前提

- 这个 skill 运行在 bot 当前所在机器上，网络、DNS、环境变量、代理配置都以当前运行环境为准。
- `${ENV_VAR}` 会在脚本里展开，所以凭证应优先放在服务器环境变量里。
- URL 会经过 SSRF 校验；私网、回环和内网地址会被直接拦截。

## 可用命令列表

- `request`

## 常见场景示例

调用 JSON API：

```bash
bash "{baseDir}/scripts/http.sh" request \
  --method GET \
  --url "https://api.github.com/repos/openai/openai-python"
```

带 query 和 header：

```bash
bash "{baseDir}/scripts/http.sh" request \
  --method GET \
  --url "https://open.feishu.cn/open-apis/docx/v1/documents/doccnxxxx" \
  --header "Authorization: Bearer ${FEISHU_TENANT_ACCESS_TOKEN}" \
  --query-json '{"lang":"zh_cn"}'
```

发送 JSON body：

```bash
bash "{baseDir}/scripts/http.sh" request \
  --method POST \
  --url "https://open.feishu.cn/open-apis/drive/v1/permissions/doccnxxxx/members?type=docx" \
  --header "Authorization: Bearer ${FEISHU_TENANT_ACCESS_TOKEN}" \
  --data-json '{"member_id":"ou_xxxx","member_type":"openid","perm":"edit","type":"user"}'
```

先查飞书官方文档，再决定怎么调：

- 先用 `web_search` 搜 `site:open.feishu.cn 飞书 接口名`
- 再用 `web_fetch` 打开具体官方页面
- 如果 `feishu-workspace` 已支持，优先走 wrapper
- 如果 wrapper 未支持，再用这个 skill 发请求

## 已知限制

- 仅支持 `http` 和 `https`。
- 不会自动替你发现最佳 API；文档不清楚时仍然需要先搜索官方资料。
- 默认会在非 `2xx` 时返回结构化错误；只有明确需要时才用 `--allow-non-2xx` 保留原始响应。
- 响应体会截断，避免把超大返回直接塞进上下文。
