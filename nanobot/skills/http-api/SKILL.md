---
name: http-api
description: Call external HTTP APIs through a controlled wrapper when no domain-specific skill already covers the endpoint.
metadata: {"nanobot":{"emoji":"🌐","requires":{"bins":["bash","python3"]}}}
---

# HTTP API

## 一句话定位

在用户明确要调用外部 HTTP API，且现有专用 skill 或 wrapper 没有覆盖对应 endpoint 时使用这个 skill。

## 触发条件

- 用户明确要求“调用 API”“发 HTTP 请求”“试某个 REST endpoint”。
- 用户需要调试请求参数、headers、body、返回状态码或响应体。
- 用户要查官方 API 文档后，再补调当前 wrapper 未覆盖的 endpoint。

## 开始前必做检查

- 先判断有没有更专用的 skill 或 wrapper。
- Feishu 场景优先使用 `feishu-workspace`；只有在 wrapper 未覆盖某个官方 endpoint 时，才切到这个 skill。
- 先确认当前运行环境是不是实际要发请求的那台机器，不要把本地和服务器环境混为一谈。
- 认证信息优先走当前环境变量，不要把密钥硬编码进命令或文档。
- 如果 API 文档还不明确，先用 `web_search` 搜索官方文档，再用 `web_fetch` 打开具体页面。

## 可执行操作清单

- 可以通过 `{baseDir}/scripts/http.sh request` 发起受控的 `GET`、`POST`、`PUT`、`PATCH`、`DELETE` 请求。
- 可以传 query、headers、JSON body、纯文本 body，并返回结构化 JSON 结果。
- 可以读取响应状态码、最终 URL、响应头、JSON 响应体或截断后的文本响应体。
- 可以把 `${ENV_VAR}` 占位符交给脚本展开，用当前运行环境里的密钥发请求。
- 可以把它当作专用 wrapper 的补充，不要和专用 wrapper 重复造轮子。

## 不要尝试的操作清单

- 不要把它当成“随意调用任何 API”的无限权限通道。
- 不要访问内网、回环、本机私网地址或任何被 SSRF 保护拦截的目标。
- 不要优先用它替代已经存在的专用 skill。
- 不要把 token、app_secret、cookie 直接写死到仓库文件里。
- 不要在用户只需要“查文档”时直接盲调写操作 endpoint。

## 失败处理规则

- 遇到 `401`、`403`、`429`、`5xx` 时，直接报告状态码和响应摘要，不要编造成功结果。
- 如果 URL 校验失败或命中了私网拦截，立即停止，不要换变体绕过。
- API 文档不明确时，先补搜索官方文档，不要猜参数。
- 同一错误不要在没有新信息的情况下重复重试。
