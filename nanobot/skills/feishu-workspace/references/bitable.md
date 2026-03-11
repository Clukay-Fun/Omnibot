# Bitable Reference

## 最小权限

根据飞书开放平台当前文档文案，至少需要开通以下多维表格权限：

- `查看、评论和导出多维表格`
- `查看、评论、编辑和管理多维表格`

如果接口返回 `99991672`，优先检查这两项是否已在应用后台开通。

## 支持范围

- `app get`
- `table list|get`
- `view list|get`
- `field list|get|create|update|delete`
- `record list|get|create|update|delete|batch_create|batch_update|batch_delete`

不支持：

- 创建或删除整个 bitable app
- 创建或删除整个 table
- wiki 内嵌 bitable URL

## URL / ID

支持：

- 原始 `app_token`
- 原始 `table_id`
- 原始 `view_id`
- `https://xxx.feishu.cn/base/<app_token>`
- 带查询参数的 bitable URL，例如 `?table=tblxxx&view=vewxxx`

不支持：

- `wiki/...` 里的内嵌 bitable URL

## 分页

所有 list 默认 `page_size=20`，需要更多结果时显式传 `--page-token`。

示例：

```bash
bash "{baseDir}/scripts/bitable.sh" record list \
  --app-token app_token \
  --table-id tbl_id \
  --page-size 20 \
  --page-token next_token
```

## 常用示例

先做检查：

```bash
bash "{baseDir}/scripts/bitable.sh" check --app-token app_token
```

获取 app：

```bash
bash "{baseDir}/scripts/bitable.sh" app get --app-token app_token
```

列记录：

```bash
bash "{baseDir}/scripts/bitable.sh" record list \
  --app-token app_token \
  --table-id tbl_id \
  --view-id view_id
```

创建记录：

```bash
bash "{baseDir}/scripts/bitable.sh" record create \
  --app-token app_token \
  --table-id tbl_id \
  --data-json '{"fields":{"Name":"Alice","Status":"Open"}}'
```

批量更新记录：

```bash
bash "{baseDir}/scripts/bitable.sh" record batch_update \
  --app-token app_token \
  --table-id tbl_id \
  --data-json '{"records":[{"record_id":"rec1","fields":{"Status":"Done"}}]}'
```

更新字段：

```bash
bash "{baseDir}/scripts/bitable.sh" field update \
  --app-token app_token \
  --table-id tbl_id \
  --field-id fld_id \
  --data-json '{"field_name":"Priority","type":1}'
```
