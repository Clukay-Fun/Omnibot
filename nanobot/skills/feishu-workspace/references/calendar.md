# Calendar Reference

## 最小权限

根据飞书开放平台当前文档文案，至少需要开通：

- `获取日历、日程及忙闲信息`
- `更新日历及日程信息`

tenant token 只能访问应用本身有权限的日历资源。不要假设能访问用户私人日历。

## 支持范围

- `calendar list|get`
- `event list|get|create|update|delete`

不支持：

- 创建或删除整个 calendar
- 共享权限、订阅、群组设置
- calendar URL 解析；v1 只接受原始 `calendar_id` / `event_id`

## 分页与时间

所有 list 默认 `page_size=20`。

`event list` 支持：

- `--page-token`
- `--start-time`
- `--end-time`
- `--anchor-time`
- `--sync-token`

时间建议使用 ISO 8601，例如 `2026-03-11T10:00:00+08:00`。

## 常用示例

检查：

```bash
bash "{baseDir}/scripts/calendar.sh" check
```

列 calendar：

```bash
bash "{baseDir}/scripts/calendar.sh" calendar list --page-size 20
```

列 event：

```bash
bash "{baseDir}/scripts/calendar.sh" event list \
  --calendar-id cal_id \
  --start-time 2026-03-11T00:00:00+08:00 \
  --end-time 2026-03-12T00:00:00+08:00
```

创建 event：

```bash
bash "{baseDir}/scripts/calendar.sh" event create \
  --calendar-id cal_id \
  --data-json '{"summary":"Demo","start_time":"2026-03-11T10:00:00+08:00","end_time":"2026-03-11T11:00:00+08:00"}'
```

更新 event：

```bash
bash "{baseDir}/scripts/calendar.sh" event update \
  --calendar-id cal_id \
  --event-id evt_id \
  --data-json '{"summary":"Updated title"}'
```

删除 event：

```bash
bash "{baseDir}/scripts/calendar.sh" event delete \
  --calendar-id cal_id \
  --event-id evt_id \
  --need-notification true
```
