# Changelog

本项目的所有重要变更都记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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

[Unreleased]: https://github.com/yyswys-yjyj/astrbot_plugin_conversation_enhance/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/yyswys-yjyj/astrbot_plugin_conversation_enhance/commits/v1.0.0
