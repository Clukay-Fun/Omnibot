# Runtime 文案与路由配置说明

本目录用于承载「可配置的用户文案、触发词、卡片模板」。

- `prompts/`: 用户可见文案（语气、提示、引导）
- `routing/`: 路由触发词（smalltalk、分页、领域提示）
- `templates/`: 卡片/结构化输出模板

代码会优先读取 workspace 下同名文件，未覆盖时回退到内置默认值。

## 1) prompts/*.yaml

### `prompts/smalltalk.yaml`
- `bot_intro_lines`: 闲聊问「你是谁/能干嘛」时的介绍文案（多行）

### `prompts/onboarding.yaml`
- `guide_lines`: onboarding 完成后的引导说明（多行）
- `intro_reentry`: 已完成用户重新 `/setup` 时提示
- `intro_completed_reentry`: 重复点击已完成卡片时提示
- `intro_start`: 开始设置提示
- `intro_submit_done`: 提交设置完成提示
- `intro_skip_done`: 跳过设置完成提示
- `intro_first`: 首次触发 onboarding 提示

### `prompts/preference.yaml`
- `preferred_name_set`: 已设置称呼后的反馈
  - 占位符: `{preferred_name}`
- `preferred_name_fallback`: 未设置称呼时的兜底反馈
  - 占位符: `{fallback_name}`
- `preferred_name_missing`: 无可用称呼时的提示

### `prompts/help.yaml`
- `commands_help_text`: `/help` 主帮助文案
- `session_help_text`: `/session` 子命令帮助文案

### `prompts/pagination.yaml`
- `no_more_content`: 没有可继续内容提示
- `continuation_hint`: 截断后提示继续命令
  - 占位符: `{continue_command}`
- `not_found_data`: 查询无数据兜底文案

### `prompts/progress.yaml`
- `answer_placeholder`: 模型首包慢时的占位回复
- `prepare_tool`: 准备调用工具
  - 占位符: `{tool}`
- `call_tool_with_args`: 调用工具（带参数）
  - 占位符: `{tool}`, `{args}`
- `call_tool_no_args`: 调用工具（无参数）
  - 占位符: `{tool}`
- `tool_result`: 工具结果摘要
  - 占位符: `{tool}`, `{result}`
- `tool_done`: 工具完成提示
  - 占位符: `{tool}`
- `data_ready`: 数据已获取，开始整理答案
- `thinking_done`: 思考完成提示
- `thinking_active`: 思考区默认标题
- `thinking_collapsed_summary`: 思考折叠标题
- `thinking_placeholder_markdown`: 思考区空内容占位（markdown）
- `thinking_generic_lines`: 判定为“通用占位思考语”的行列表

## 2) routing/*.yaml

### `routing/smalltalk_triggers.yaml`
- `direct_queries`: 直接判定为 smalltalk 的问句
- `smalltalk_hints`: smalltalk 兜底提示词
- `ability_subject_tokens`: 主体词（如“你/您”）
- `ability_aux_tokens`: 能力助词（如“能/会/可以”）
- `ability_action_tokens`: 行为词（如“干嘛/做什么/怎么用”）

### `routing/preference_triggers.yaml`
- `direct_queries`: 直接判定为称呼查询的问句
- `contains_rules`: 包含匹配规则（`all` 内词全命中即触发）

### `routing/pagination_triggers.yaml`
- `continuation_commands`: 续页命令（如“继续/展开”）

### `routing/domain_hints.yaml`
- `reminder_keywords`: 提醒域识别词
- `cancel_intent_tokens`: 取消意图识别词
- `business_keywords`: 业务域词（smalltalk 过滤）
- `case_query_keywords`: case 查询域词
- `case_query_prefixes`: case 查询前缀词（用于清洗 query）
- `case_query_suffixes`: case 查询后缀词（用于清洗 query）
- `template_case_keywords`: response template 案件路由词
- `template_contract_keywords`: response template 合同路由词
- `template_cross_keywords`: response template 跨表路由词

## 3) templates/*.json

### `templates/onboarding_form.json`
- onboarding 卡片结构与文案（标题、字段名、placeholder、按钮、选项）
- 建议只改文案值，不改字段 key（避免前端 action/form 兼容性问题）

### `templates/card_confirm.json`
- `text`: 写入确认提示文案
  - 占位符: `{preview}`, `{token}`

### `templates/card_case.json`
- `header`: 案件卡片标题
- `lines`: 行模板数组
  - 占位符: `{case_no}`, `{title}`, `{client}`, `{owner}`, `{status}`, `{total}`, `{url}`

### `templates/card_contract.json`
- `header`: 合同卡片标题
- `lines`: 行模板数组
  - 占位符: `{contract_no}`, `{name}`, `{counterparty}`, `{owner}`, `{amount}`, `{status}`, `{sign_date}`, `{total}`, `{url}`

### `templates/card_overview.json`
- `header`: 总览卡片标题
- `source_title`: 数据来源段标题
- `preview_title`: 结果预览段标题
- `empty_source`: 无来源时文案
- `empty_preview`: 无预览时文案
- `overflow_hint`: 预览超长提示
  - 占位符: `{omitted}`, `{continue_command}`

### `templates/card_summary.json`
- `header`: 摘要卡片标题
- `source_line`: 数据来源行
  - 占位符: `{sources}`
- `total_line`: 命中总数行
  - 占位符: `{total}`
- `next_step`: 下一步建议

## 修改建议

- 只改 value，不改 key 名。
- 带占位符的文案请保留占位符本身。
- 先在测试环境改 workspace 配置验证，再合并默认模板。
