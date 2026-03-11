# Docs / Wiki / Drive Reference

## 最小权限

根据飞书开放平台当前文档文案，v1 依赖以下权限组合：

- 新版文档：
  - `查看和评论新版文档`
  - `创建和编辑新版文档`
- 知识库：
  - `查看、编辑和管理知识库`
- 云空间文件：
  - `查看、评论、编辑和管理云空间中所有文件`
  - `上传、下载文件到云空间`

tenant token 只能访问应用有权限的文档、知识库节点和云盘文件。不要假设能访问用户私人文件。

## 支持范围

- Drive:
  - `drive list`
  - `drive search`（目录扫描 + 标题过滤，不是全局搜索）
  - `drive get`
  - `drive delete`
- Doc:
  - `doc create`
  - `doc read_text`
  - `doc append_text`
  - `doc trash`
- Wiki:
  - `wiki space_list|space_get`
  - `wiki node_list|node_get|node_create|node_delete`

不支持：

- `replace_all_text`
- 富文本 block 精细编辑
- 图片 / 表格 / 嵌入块写入
- 权限管理、移动、所有权转移、空间级删除
- Sheets

## URL / ID

支持：

- 文档：`https://xxx.feishu.cn/docx/<document_id>`、`https://xxx.feishu.cn/docs/<document_id>`
- 知识库：`https://xxx.feishu.cn/wiki/<node_token>`
- 文件：`https://xxx.feishu.cn/file/<file_token>`
- 文件夹：`https://xxx.feishu.cn/drive/folder/<folder_token>`
- 以及对应原始 token

不支持：

- calendar URL
- 无法识别格式的自定义分享链接

## 文档文本策略

`doc read_text` 返回纯文本提取结果，并对非文本块使用占位符：

- `[图片]`
- `[表格]`
- `[代码块]`
- `[嵌入内容]`
- `[多维表格]`

这意味着：

- 结果适合让模型理解文档大意
- 但不保留完整 block JSON
- `append_text` 只会追加纯文本段落

## 删除边界

- `doc trash` / `drive delete` / `wiki node_delete` 都是高风险操作
- 仅在用户明确要求删除时才执行
- `wiki node_delete` 当前实现走“先取 node，再删除其底层 file/doc 对象”的 fallback

## 分页

`drive list`、`drive search`、`wiki space_list`、`wiki node_list` 默认 `page_size=20`。

翻页示例：

```bash
bash "{baseDir}/scripts/docs.sh" drive list \
  --folder-token folder_token \
  --page-size 20 \
  --page-token next_token
```

## 常用示例

检查：

```bash
bash "{baseDir}/scripts/docs.sh" check
```

创建文档：

```bash
bash "{baseDir}/scripts/docs.sh" doc create \
  --title "Project Notes"
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

查询文件元数据：

```bash
bash "{baseDir}/scripts/docs.sh" drive get \
  --identifier https://example.feishu.cn/file/file_token
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
