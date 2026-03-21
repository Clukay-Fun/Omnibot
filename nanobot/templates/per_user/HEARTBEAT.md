# HEARTBEAT.md - 私聊维护规则

这是当前私聊用户的低打扰维护规则。
heartbeat 是低打扰提醒执行器，不是任务管理者。

每次心跳检查时：

1. 结合当前用户目录下的 `USER.md`、`WORKLOG.md`、`memory/MEMORY.md` 与本文件里的当前规则
2. 只有在其中已经明确记录了待跟进事项、承诺回访项、持续任务或用户要求你后续再处理的事情时，才执行
3. 如果没有这些明确线索，就直接跳过，不主动打扰用户

如果需要长期维护某个周期性动作，请把具体规则追加到这个文件里；当前工作正文请写进 `WORKLOG.md`。如果某条信息会影响未来心跳行为，请把它写进 `HEARTBEAT.md` 或 `memory/MEMORY.md`，不要只留在聊天记录里。

## 边界

- heartbeat 只允许读取相关文件并在必要时提醒用户。
- heartbeat 不替用户修改任务状态、偏好、人格或长期记忆。
- heartbeat 不修改 `WORKLOG.md`、`USER.md`、`SOUL.md`、`memory/MEMORY.md`。
- `HEARTBEAT.md` 里的 managed state block 只由框架代码维护。

<!-- HEARTBEAT_STATE:BEGIN -->
## Last Heartbeat Run
- At: (not run yet)
- Decision: skip
- Summary: No heartbeat run has been recorded yet.
<!-- HEARTBEAT_STATE:END -->
