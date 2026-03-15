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

tenant token 只能访问应用有权限的文档、知识库节点和云盘文件，不要假设能访问用户私人文件。

## 可用命令列表

- `check`
- `drive list|search|get|delete`
- `doc create|read_text|append_text|trash`
- `wiki space_list|space_get|node_list|node_get|node_create|node_delete`

支持标准 `docx`、`docs`、`wiki`、`file`、`drive/folder` URL 和对应原始 token。`doc read_text` 返回纯文本提取结果，并对非文本块使用占位符；`append_text` 只追加纯文本段落。

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
- 不支持权限管理、移动、所有权转移、空间级删除、Sheets。
- `doc trash`、`drive delete`、`wiki node_delete` 都是高风险操作，只在用户明确要求时执行。
- 当前状态类问题必须重新执行 `check`、`list`、`get`、`read_text` 或其他对应命令，不要直接复用历史结果。
