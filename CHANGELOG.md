# Changelog

本项目的所有重要变更都记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.2.0] - 2026-11-20

### Added

- `/retitle`：按当前对话的全部历史重新生成标题。
- 自动会话标题：第一轮对话结束后自动调用 LLM 生成标题，
  标题会清理引号/标点/换行并按字数截断。
- 配置项 `title_enabled`（默认关闭）、`title_max_chars`（默认 20）、
  `title_auto_permission` 与 `title_retitle_permission`（各自独立的群聊开关与黑白名单）。
- `/gettop`：查看当前对话的轮数，无需额外配置。
- `/aicomment "要求"`：取当前对话最近若干轮，按用户要求让模型复述或评论，
  只调用一次 LLM，不写入对话历史；配套配置 `comment_max_rounds`、`comment_round_chars`
  与 `aicomment_permission`。
- `/grep -ai "检索词"`：把候选轮次交给 LLM 挑选，最多展示 3 条。
- `/grep -rag "检索词" [页码]`：用 AstrBot 配置的嵌入模型做向量检索，按相似度排序、分页并显示相似度。
- 配置项 `grep_ai_max_rounds`、`grep_ai_max_chars`、`rag_round_chars`、`rag_embedding_provider`。
- `/rewrite get`：查看当前轮已重写几次、现在是第几版。
- `/rewrite set n`：切回第 n 次重写的版本（`0` 为原版）。
- `/rewrite view n`：预览第 n 次重写版本的回复原文。
- 配置项 `rewrite_mode`：`astrbot`（默认，官方链路，可调用工具）/ `plugin`
  （插件内置流水线，不调用工具，但不受 CmdMask 的 `call_llm` 标记影响）。
- 配额（时间流动控制）：所有权限组新增 `rate_enabled`、`rate_limit_minutes`、
  `rate_limit_count`，按「每 N 分钟最多 M 次」限制单个用户/群，UNIX 时间戳滑动窗口、
  记录懒加载于 `quotas.json`；指令成功后才记录，失败与用法提示不消耗配额。
- 会话黑白名单 `session_filter`：`on_waiting_llm_request` 钩子在 LLM 请求发出前按 UMO 判断，
  黑名单命中或白名单未命中时发送可配置的 `reason` 并 `stop_event()`，直接终止事件传播；
  支持 `*` / `?` 通配符与 `{umo}` 占位。
- 同上名单同时作用于指令：所有指令外层统一守卫，被拦截的会话只返回 `reason`，
  不执行指令逻辑、不消耗配额。

### Changed

- `/rewrite` 与 `/rewrite user|u` 默认改走 AstrBot 官方消息链路：插件截断历史到本轮之前并
  替换 `event.message_str`，由官方流程生成回复；生成完成后在 `on_llm_response` 钩子里
  把回复回填到对应版本。
- 版本记录只做内存暂存（`RewriteRecord` 数据类 + `asyncio.Lock`，按会话与对话 ID 分开），
  不落盘；参照「遗忘」插件：后台每 5 分钟清理超过 30 分钟未更新的记录，
  下一条 LLM 请求开始时清掉上一轮暂存，`terminate` 时取消清理任务并清空。
- 自动标题改在 `on_llm_response` 触发：AstrBot 采用洋葱式调度，`after_message_sent` 时
  本轮历史尚未写库，旧实现在第一轮读不到对话内容，标题不会生成。

### Fixed

- `/rewrite` 在 CmdMask 等插件改写指令后不生成回复的问题：这类插件会设置 `call_llm=True`，
  AstrBot 便不再进入 Agent 阶段，导致历史被截断却没有回复。现在发起重写前会复位
  `call_llm` 与唤醒标记，并跳过已有发送操作的事件。

## [1.0.0] - 2026-11-14

首个正式发布版本：由早期内部版本（会话检索与改写两个插件）合并为「对话增强」。

### Added

- `/grep [user|u|ai|a] "检索词" [页码]`：跨对话检索轮次，按时间线编号并分页。
  - 引号包裹检索词，支持半角/全角引号、嵌套引号；不带引号时末尾数字视为页码。
  - 匹配前压平空白，原文跨行也能命中。
  - 输出按官方 `/history` 风格省略（每页 3 轮、单条 150 字、单条消息 600 字）。
- `/show 轮次`：原样回看某一轮，先发 user，间隔可配置后再按条发 AI，不合并、不含工具调用。
- `/fork 轮次`：把第 1~N 轮复制成新对话，标题为 `原标题-分支`，原对话保持不变。
- `/rewrite`：重发上一条回复；`/rewrite user|u`、`/rewrite ai|a` 分别改写最后一条用户消息与 AI 回复。
  - 支持英文/中文/直角引号与半角圆括号，可嵌套；子串多处命中时提示消歧。
- `/showprompt`、`/addprompt`、`/coverprompt`、`/resetprompt`：按单个用户/群插入或覆盖系统提示词。
  - `/addprompt front|f|前|头` 追加在前，`tail|t|后|尾|尾巴|末尾` 追加在后；`/coverprompt` 覆盖默认提示词。
  - 注入通过 `on_llm_request` 改写 `req.system_prompt`，生效顺序为「头部追加 + 覆盖或默认 + 尾部追加」。
  - 配置存放于 `data/plugin_data/astrbot_plugin_conversation_enhance/prompts.json`。
- 按指令的权限配置：`grep_permission` / `show_permission` / `fork_permission` / `rewrite_permission` / `prompt_permission`。
  - 每条指令独立设置群聊开关、黑白名单模式（同一指令只能选一种）与名单 ID。
  - 名单可填发送者 ID、会话 UMO 或群号，命中任一即算在名单内。
- 提示词功能总开关 `prompt_permission.enabled`，默认关闭。
- `LICENSE`（Apache-2.0 官方原文）与 `NOTICE`。

### Changed

- 插件更名为 `astrbot_plugin_conversation_enhance`（显示名「对话增强」），合并检索、回看、分支、改写、提示词五个部分。
- `/rewrite` 的群聊限制从硬编码改为权限配置 `rewrite_permission.allow_group`，默认仍为仅私聊。
- 插件只依赖 AstrBot 公共 API（事件、对话管理、LLM、`StarTools`），不依赖任何平台适配器内部接口。

### Fixed

- 改写用户消息与重发时未继承当前作用域配置的提示词（`/rewrite` 直接调用 `llm_generate`，绕过了 `on_llm_request`）。
- 权限名单在平台拿不到 `sender_id` 时无法匹配，现补充会话 ID、UMO、群号兜底。
- 引号成对嵌套时尾部引号被误删。

[Unreleased]: https://github.com/yyswys-yjyj/astrbot_plugin_conversation_enhance/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/yyswys-yjyj/astrbot_plugin_conversation_enhance/commits/v1.1.0
[1.0.0]: https://github.com/yyswys-yjyj/astrbot_plugin_conversation_enhance/commits/v1.0.0
