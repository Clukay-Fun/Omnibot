# Docs / Wiki / Drive Reference

## 最小权限 Scope

根据飞书开放平台当前文案，v1 依赖以下权限组合：

- 新版文档：
  - `查看和评论新版文档`
  - `创建和编辑新版文档`
- 知识库：
  - `查看、编辑和管理知识库`
- 云空间文件：
  - `查看、评论、编辑和管理云空间中所有文件`
  - `上传、下载文件到云空间`
- 权限管理：
  - `添加云文档协作者`
  - `更新云文档协作者权限`
  - `修改云文档权限设置`

tenant token 只能访问应用有权限的文档、知识库节点和云盘文件，不要假设能访问用户私人文件。

如果当前请求来自飞书聊天消息，运行时上下文可能已经包含 `Feishu User Open ID`。当用户说“把我加进去”“给我可编辑权限”时，优先直接把这个值当作 `permission member create --member-id` 使用。注意这只适用于实时飞书消息上下文；在服务器 shell 手工执行 `docs.sh` 时，不会自动带入当前用户 open_id，需要显式传参。

如果当前用户要调飞书官方 API，但这里还没接入对应 endpoint，先搜索和打开 `open.feishu.cn` 官方文档，再切到 `http-api` skill 发请求；不要在 wrapper 已支持的能力上退回到通用 HTTP。

## 可用命令列表

- `check`
- `drive list|search|get|delete`
- `doc create|read_text|append_text|trash`
- `permission member list|auth|create|batch_create|update|delete|transfer_owner`
- `permission public get|patch`
- `permission public password create|update|delete`
- `raw request`
- `wiki space_list|space_get|node_list|node_get|node_create|node_delete`

支持标准 `docx`、`docs`、`wiki`、`file`、`drive/folder`、`base` URL 和对应原始 token。权限命令对原始 token 需要显式传 `--doc-type`；`public get|patch` 默认使用新版 `v2` 接口，也可通过 `--api-version v1` 切换到历史版本。`doc read_text` 返回纯文本提取结果，并对非文本块使用占位符；`append_text` 只追加纯文本段落。

## 常见场景示例

检查权限和连通性：

```bash
bash "{baseDir}/scripts/docs.sh" check
```

创建文档：

```bash
bash "{baseDir}/scripts/docs.sh" doc create --title "Project Notes"
```

读取文档文本：

```bash
bash "{baseDir}/scripts/docs.sh" doc read_text \
  --document-id doccnxxxx \
  --max-chars 8000
```

查看协作者列表：

```bash
bash "{baseDir}/scripts/docs.sh" permission member list \
  --token doccnxxxx \
  --doc-type docx \
  --fields name,type,avatar
```

增加协作者权限：

```bash
bash "{baseDir}/scripts/docs.sh" permission member create \
  --token doccnxxxx \
  --doc-type docx \
  --member-type openid \
  --member-id ou_xxxx \
  --perm edit \
  --collaborator-type user
```

如果是“把当前发消息的人加进去”，`--member-id` 优先使用运行时上下文里的 `Feishu User Open ID`，不要要求用户重复提供；只有在脱离飞书消息上下文的服务器 shell、定时任务或离线脚本里，才需要额外传真实 `open_id`。

直接调任意 Feishu Open API：

```bash
bash "{baseDir}/scripts/docs.sh" raw request \
  --method POST \
  --path "/open-apis/drive/v1/permissions/doccnxxxx/members" \
  --query-json '{"type":"docx"}' \
  --data-json '{"member_id":"ou_xxxx","member_type":"openid","perm":"edit","type":"user"}'
```

如果要显式使用其他 bearer token：

```bash
bash "{baseDir}/scripts/docs.sh" raw request \
  --method GET \
  --url "https://open.feishu.cn/open-apis/contact/v3/users/me" \
  --auth-mode bearer \
  --bearer-token-env FEISHU_USER_ACCESS_TOKEN
```

更新公开分享设置：

```bash
bash "{baseDir}/scripts/docs.sh" permission public patch \
  --token doccnxxxx \
  --doc-type docx \
  --api-version v2 \
  --external-access-entity open \
  --share-entity anyone \
  --link-share-entity tenant_readable
```

追加纯文本：

```bash
bash "{baseDir}/scripts/docs.sh" doc append_text \
  --document-id doccnxxxx \
  --text "Follow-up:\n\n- item 1\n- item 2"
```

目录范围内搜索标题：

```bash
bash "{baseDir}/scripts/docs.sh" drive search \
  --folder-token folder_token \
  --title-contains Roadmap
```

创建 wiki 节点：

```bash
bash "{baseDir}/scripts/docs.sh" wiki node_create \
  --space-id 123456 \
  --data-json '{"title":"Weekly Notes","obj_type":"docx","parent_node_token":"wikcnxxx"}'
```

## 已知限制

- 不支持 `replace_all_text`。
- 不支持富文本 block 精细编辑、图片/表格/嵌入块写入。
- 结构化 wrapper 仅覆盖常用 docs/wiki/drive/permission 能力；未覆盖的 Feishu 官方 endpoint 请改用 `raw request`。
- `doc trash`、`drive delete`、`wiki node_delete`、`permission member transfer_owner` 都是高风险操作，只在用户明确要求时执行。
- 当前状态类问题必须重新执行 `check`、`list`、`get`、`read_text` 或其他对应命令，不要直接复用历史结果。
