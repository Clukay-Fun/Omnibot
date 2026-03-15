# Bitable Reference

## 最小权限 Scope

根据飞书开放平台当前文案，至少需要开通：

- `查看、评论和导出多维表格`
- `查看、评论、编辑和管理多维表格`

如果接口返回 `99991672`，先检查这些权限是否已在应用后台开通。

## 可用命令列表

- `check`
- `app get`
- `table list|get`
- `view list|get`
- `field list|get|create|update|delete`
- `record list|get|create|update|delete|batch_create|batch_update|batch_delete`

命令支持原始 `app_token` / `table_id` / `view_id`，也支持标准 `base` URL。所有 list 默认 `page_size=20`，需要更多结果时显式传 `--page-token`。

## 常见场景示例

检查权限和连通性：

```bash
bash "{baseDir}/scripts/bitable.sh" check --app-token app_token
```

列出 table：

```bash
bash "{baseDir}/scripts/bitable.sh" table list --app-token app_token
```

列出 record：

```bash
bash "{baseDir}/scripts/bitable.sh" record list \
  --app-token app_token \
  --table-id tbl_id \
  --view-id view_id
```

翻页读取 record：

```bash
bash "{baseDir}/scripts/bitable.sh" record list \
  --app-token app_token \
  --table-id tbl_id \
  --page-size 20 \
  --page-token next_token
```

创建 record：

```bash
bash "{baseDir}/scripts/bitable.sh" record create \
  --app-token app_token \
  --table-id tbl_id \
  --data-json '{"fields":{"Name":"Alice","Status":"Open"}}'
```

更新 field：

```bash
bash "{baseDir}/scripts/bitable.sh" field update \
  --app-token app_token \
  --table-id tbl_id \
  --field-id fld_id \
  --data-json '{"field_name":"Priority","type":1}'
```

## 已知限制

- 不支持创建或删除整个 bitable app。
- 不支持创建或删除整个 table。
- 不支持 `wiki/...` 内嵌 bitable URL。
- 当前状态类问题必须重新执行 `check`、`list`、`get` 或其他对应命令，不要直接复用历史结果。
