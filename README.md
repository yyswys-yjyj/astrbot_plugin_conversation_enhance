# 对话增强插件 (Conversation Enhance)

围绕 **AI 与用户的对话历史** 提供增强能力：检索、回看、分支、改写、提示词插入/覆盖。

> Power by serveryyswys · v1.1.0

变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 指令一览

| 指令 | 作用 |
|------|------|
| `/grep [user\|u\|ai\|a] "检索词" [页码]` | 跨对话检索轮次，按时间线编号，分页返回 |
| `/show 轮次` | 原样回看某一轮：先发 user，隔 10 秒再按条发 AI |
| `/fork 轮次` | 把第 1~N 轮复制成新对话，标题 `原标题-分支` |
| `/rewrite` | 重发：让模型重新生成上一条回复，不改提示词 |
| `/rewrite user\|u "旧文本" "新文本"` | 改写自己最后一条消息，并让 AI 重新回答 |
| `/rewrite ai\|a "旧文本" "新文本"` | 改写 AI 最后一条回复（记忆修正） |
| `/rewrite_help` | 查看改写帮助 |
| `/showprompt` | 查看本用户/群已配置的提示词 |
| `/addprompt front\|f\|前 "内容"` | 在默认提示词**前**追加 |
| `/addprompt tail\|t\|后 "内容"` | 在默认提示词**后**追加 |
| `/coverprompt "内容"` | 覆盖默认提示词 |
| `/resetprompt` | 恢复默认提示词 |
| `/retitle` | 基于当前对话全部历史重新生成标题 |

## /grep

- **引号**用于让检索词包含空格、冒号、逗号等：`/grep u “第一句 第二句”`。
- **引号可嵌套**：只剥最外层一层，内层原样保留，例如 `“句子“引号内句子”，“不完整引号句”` → `句子“引号内句子”，“不完整引号句`。
- 支持中英文引号与直角引号；引号不成对时只去掉落单的一侧。
- 不带引号时按空白切分，末尾纯数字视为页码。
- 匹配前会压平空白，原文跨行也能命中。
- 输出按官方 `/history` 风格省略：每页 3 轮、单条 150 字、单条消息总量 600 字，防止触发平台风控。
- `/grep a` 只匹配 AI 侧，`/grep u` 只匹配用户侧。

## /show 与 /fork

- 轮次编号从 1 开始：一条 user 消息开始一轮，其后所有 assistant（含其间 tool 记录）都属于该轮。
- `/show` 不合并 AI 多条回复，也不发送工具调用内容；`/show` 会完整发送，不受 grep 的截断配置影响。
- `/fork` 是**复制**而非移动：原对话保持不变，创建后当前对话切换到新分支。

## /rewrite

- 仅私聊可用，群聊会被拦截。
- 子串替换，重复出现多处时会要求提供更多特征文本。
- 文本含空格时用引号或括号包裹，两个参数需使用相同符号：
  `"文本"`、`'文本'`、`(文本)`、`“文本”`、`‘文本’`、`「文本」`、`『文本』`。
- 引号可嵌套，例如 `/rewrite u “他说“你好”然后走了” “新的句子”`。
- 改写用户消息后会调用 LLM 重新生成回复，并处理调用期间并发到达的新消息。

## 提示词插入/覆盖

面向不满足平台默认提示词的人，作用域是**单个用户或群**（按 `platform:消息类型:会话ID` 区分）。

- `/addprompt f|front|前|头|头部 "内容"`：插在默认提示词之前；\
  `/addprompt t|tail|后|尾|尾巴|末尾 "内容"`：插在默认提示词之后。\
  多次追加会按顺序拼接，方向不变。
- `/coverprompt "内容"`：用该内容替换默认提示词；与前后追加同时存在时，\
  最终为「头部追加 + 覆盖内容 + 尾部追加」。
- `/resetprompt`：删除本作用域的配置，恢复默认。
- `/showprompt`：展示本作用域已配置的覆盖/头部/尾部内容（预览长度受 `prompt_preview_chars` 限制）。
- 内容引号支持半角/全角，且可嵌套，与 `/grep`、`/rewrite` 一致。
- 注入时机是 AstrBot 的 `on_llm_request` 钩子，直接改写 `req.system_prompt`；\
  `/rewrite` 内部直接调用 `llm_generate`，已额外复用同一套拼接逻辑，\
  所以改写用户消息或重发时也会继承本作用域的提示词。\
  配置存放于 `data/plugin_data/astrbot_plugin_conversation_enhance/prompts.json`。
- 该功能**默认关闭**，需打开 `prompt_permission.enabled`；关闭时指令提示未开启，\
  也不改动任何 LLM 请求。

## 自动会话标题

- 开关：`title_enabled`，默认关闭（会额外调用 LLM，产生 token 开支）。
- 自动触发：消息发送完成后检查当前对话，如果历史里只有**一组**（一条 user 及其后的\
  assistant 与工具调用记录），且对话还没有标题，就调用一次 LLM 生成标题写回。
- `/retitle`：按当前对话的**全部**历史重新生成标题，覆盖已有标题。
- 标题会清理引号、结尾标点、换行并按 `title_max_chars`（默认 20 字）截断。
- 生成时只取最近 10 轮、每段 300 字作为上下文，避免标题请求本身消耗过多 token。

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `page_size` | int | 3 | `/grep` 每页轮次数 |
| `max_chars` | int | 150 | `/grep` 每条内容截断字数 |
| `max_total_chars` | int | 600 | `/grep` 单条消息总字数上限，0 = 不限制 |
| `show_delay_seconds` | int | 10 | `/show` 中 user 与 AI 之间的间隔 |
| `grep_scope` | string | all | `all` = 当前会话下所有对话；`current` = 仅当前对话 |
| `strip_system_reminder` | bool | true | 展示时去掉 `<system_reminder>` 块 |
| `allow_modify_assistant` | bool | true | 是否允许 `/rewrite ai` |
| `prompt_preview_chars` | int | 200 | `/showprompt` 每条内容的预览字数 |
| `title_enabled` | bool | false | 自动标题与 `/retitle` 总开关 |
| `title_max_chars` | int | 20 | 标题字数上限 |

## 权限配置

每个指令/功能独立配置（`grep_permission` / `show_permission` / `fork_permission` / `rewrite_permission` / `prompt_permission` / `title_auto_permission` / `title_retitle_permission`）：

| 字段 | 说明 |
|------|------|
| `enabled` | 仅 `prompt_permission` 有：提示词功能总开关，默认 `false` |
| `allow_group` | 是否允许在群聊中使用；`/rewrite`、提示词功能、标题功能默认 `false`，其余默认 `true` |
| `mode` | `off` 不限制 / `whitelist` 仅名单内可用 / `blacklist` 名单内禁用，同一指令只能选一种 |
| `ids` | 名单内容，可填发送者 ID、会话 UMO 或群号，命中任一即算在名单内 |

判定顺序：先看群聊开关（群聊被禁用时静默忽略，避免刷屏），再按名单放行或拒绝（拒绝时回复「没有使用 xxx 的权限」）。

## 兼容性

- AstrBot >= 4.16。插件只使用 AstrBot 公共 API（事件、对话管理、LLM 调用），不依赖任何平台适配器内部接口，理论上可在 aiocqhttp / QQ 官方 / Telegram 等平台通用；`[MSG_ID:...]` 清理只对 QQ 适配器注入的标记生效，其他平台是无副作用的空操作。
- 修改后需重载插件或重启 AstrBot 才生效。
- 本项目基于 Apache License 2.0 开源，详见 `LICENSE`。
