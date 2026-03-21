# HEARTBEAT.md - 周期任务

你的 nanobot 守护进程会按照配置的时间间隔（如每 30 分钟）定期检查此文件并执行其中的巡查动作。
这里记录的是低打扰提醒规则与周期性巡查动作，不是完整任务正文。当前工作事项请记录在 `WORKLOG.md`。
heartbeat 是低打扰提醒执行器，不是任务管理者。

**注意：** 如果这个文件没有活动任务（即只有纯文本标题和注释），agent 将直接跳过心跳检查处理，避免浪费性能和 Token。

## 边界

- heartbeat 只做“读相关文件 + 判断是否提醒 + 发送提醒消息”。
- heartbeat 不替用户修改任务状态、偏好、人格或长期记忆。
- heartbeat 不修改 `WORKLOG.md`、`USER.md`、`SOUL.md`、`memory/MEMORY.md`。
- `HEARTBEAT.md` 中的 managed state block 只由框架代码维护。

## 活动检查任务 / Active Tasks

<!-- 使用 edit_file 向下追加你需要周期跟进检查的任务。请保持精简。 -->



## 已完成 / Completed

<!-- 任务不需要周期执行后，移动到这里，或者直接用写文件指令删除。 -->

<!-- HEARTBEAT_STATE:BEGIN -->
## Last Heartbeat Run
- At: (not run yet)
- Decision: skip
- Summary: No heartbeat run has been recorded yet.
<!-- HEARTBEAT_STATE:END -->
