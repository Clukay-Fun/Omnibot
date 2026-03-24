---
name: feishu-workspace
description: Operate Feishu bitable, calendar, docs, wiki, and drive resources via bundled scripts. Use for deterministic Feishu workspace operations that require app-level API access, structured JSON output, and explicit action boundaries.
metadata: {"nanobot":{"emoji":"🪶","requires":{"bins":["bash"]}}}
---

# Feishu Workspace

## 一句话定位

在需要通过飞书应用级 API 读取或修改 bitable、calendar、docs、wiki、drive 当前状态时使用这个 skill。

## 触发条件

- 用户明确要查看或修改飞书多维表格、日历、文档、知识库、云空间文件或云文档权限。
- 用户问的是当前状态，例如“现在有哪些表”“当前有哪些事件”“这份文档现在写了什么”。
- 用户要求执行当前 wrapper 已支持的实体级创建、更新、删除。

## 开始前必做检查

- 先判断目标属于 `bitable`、`calendar` 还是 `docs/wiki/drive`。
- 先运行对应模块的 `check`。
- 非简单操作先读对应 reference：
  - Bitable: `{baseDir}/references/bitable.md`
  - Calendar: `{baseDir}/references/calendar.md`
  - Docs / Wiki / Drive: `{baseDir}/references/docs.md`
- 如果用户没有给出必要的资源标识，先索要链接、ID、token 或足够明确的对象名称。
- 如果当前会话来自飞书，优先查看运行时上下文是否已经提供 `Feishu User Open ID`、`Feishu Tenant Key` 等标识。用户说“把我加进去”“给我权限”时，优先直接复用当前 `Feishu User Open ID` 作为协作者 `member_id`，不要重复索要。
- 如果用户要的是飞书官方 API，但当前 wrapper 没有对应 endpoint，先用 `web_search` / `web_fetch` 查 `open.feishu.cn` 官方文档，再切到 `http-api` skill 发请求。
- 如果用户明确要“直接调任意飞书 API”，可以使用当前 skill 的原始 `open-apis` 请求入口，不必等 wrapper 逐个补 endpoint。

## 可执行操作清单

- 可以读取 bitable 的 app、table、view、field、record 当前状态。
- 可以执行受支持的 bitable `record` / `field` 实体级操作。
- 可以读取 calendar、event 当前状态，并执行 event 的受支持实体级操作。
- 可以读取 doc 文本、wiki 节点、drive 文件当前状态，并执行 doc、wiki node、drive file 的受支持实体级操作。
- 可以读取和修改受支持的云文档协作者、公开分享设置、公开密码等权限能力。
- 可以直接请求任意 Feishu `open-apis` 路径，并复用当前应用鉴权或显式 bearer token。
- 对任何当前状态类问题，都重新运行 `check`、`list`、`get`、`read` 或其他对应 wrapper 命令，不要直接复用历史回答。

## 不要尝试的操作清单

- 不要假设这里支持 `tenant_access_token` 之外的认证模型。
- 不要承诺访问用户私有日历、私有云盘文件或任何未共享给应用的资源。
- 不要做容器级删除，例如删除整个 bitable app/table、calendar、wiki space 或文件夹。
- 不要把这里当作通用 doc 富文本编辑器。仅允许受控的根级基础块追加（例如固定模板写入），不要做整文替换、任意嵌套 block 编辑或图片/表格/嵌入块写入。
- 不要手写 API 请求；直接使用 `{baseDir}/scripts/*.sh` wrapper。
- 不要把当前 wrapper 未接入的 URL、资源类型或权限能力说成已经可用。
- 不要把飞书聊天上下文里的 `open_id` 获取方式和服务器 shell 手工执行混为一谈。离开实时消息上下文后，`docs.sh` 不会自动带入当前用户身份。
- 不要在 wrapper 已支持的 Feishu 能力上退回到通用 HTTP；只有缺失 endpoint 时才切换。
- 不要把“任意飞书 API”理解成“任意外部域名”；原始请求入口也只面向 Feishu 官方 `open-apis`。

## 失败处理规则

- 遇到 `403` 或 `99991672`，解释权限边界并停止。
- 同一个不可访问目标不要重复重试。
- 脚本返回错误 JSON 时，按错误内容解释问题，不要编造结果。
- 资源标识不足时先补充信息，不要猜测对象。
