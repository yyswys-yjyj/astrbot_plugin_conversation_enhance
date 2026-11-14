"""对话增强插件：/grep 检索、/show 回看、/fork 分支、/rewrite 改写、提示词插入/覆盖。

前四个指令都围绕"轮"工作：一条 user 消息开始一轮，其后所有 assistant 消息
（含它们之间的 tool 记录）属于同一轮，轮次按时间线从 1 开始编号。
/rewrite 用于改写最后一条用户消息或 AI 回复。
提示词指令按"单个用户/群"作用域改写系统提示词。
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import traceback
from pathlib import Path
from typing import Any, List, Optional, Tuple

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import (
    AssistantMessageSegment,
    SystemMessageSegment,
    TextPart,
    UserMessageSegment,
)

from .rounds import (
    Round,
    build_rounds,
    display_text,
    load_history,
    round_matches,
)

USAGE_GREP = '用法：/grep [user|u|ai|a] "检索词" [页码]，例如 /grep u "某件事" 1'
USAGE_SHOW = "用法：/show 轮次，例如 /show 3"
USAGE_FORK = "用法：/fork 轮次，例如 /fork 3（把第 1~3 轮复制成新分支）"

SUBJECT_ALIASES = {
    "user": "user",
    "u": "user",
    "用户": "user",
    "ai": "assistant",
    "a": "assistant",
    "assistant": "assistant",
    "bot": "assistant",
    "助手": "assistant",
}

PLUGIN_NAME = "astrbot_plugin_conversation_enhance"

#: /addprompt 的位置别名：front 插在默认提示词之前，tail 插在其后。
PROMPT_POSITIONS = {
    "front": "front",
    "f": "front",
    "head": "front",
    "start": "front",
    "前": "front",
    "前面": "front",
    "开头": "front",
    "头": "front",
    "头部": "front",
    "tail": "tail",
    "t": "tail",
    "end": "tail",
    "back": "tail",
    "后": "tail",
    "后面": "tail",
    "末尾": "tail",
    "结尾": "tail",
    "尾": "tail",
    "尾巴": "tail",
}

USAGE_PROMPT = (
    "提示词指令：\n"
    "/showprompt 查看本作用域已配置的提示词\n"
    '/addprompt front|f|前 "内容" 在默认提示词前追加\n'
    '/addprompt tail|t|后 "内容" 在默认提示词后追加\n'
    '/coverprompt "内容" 覆盖默认提示词\n'
    "/resetprompt 恢复平台默认提示词"
)

#: 平台会往消息文本里追加的内部标记，解析参数前必须去掉。
_MSG_ID_RE = re.compile(r"\[MSG_ID:[^\]]*\]")

#: 中英文引号，检索词两侧的引号需要剥掉。
_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "“": "”",
    "‘": "’",
    "「": "」",
    "『": "』",
}


def _strip_quotes(token: str) -> str:
    """去掉 token 两端成对的引号。"""
    token = token.strip()
    if len(token) >= 2:
        opening, closing = token[0], token[-1]
        if _QUOTE_PAIRS.get(opening) == closing:
            return token[1:-1].strip()
    return token


def _command_args(message: str) -> list[str]:
    """取指令名之后的所有参数，支持引号包裹带空格的检索词。"""
    text = _MSG_ID_RE.sub(" ", message).strip()
    parts = text.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    try:
        tokens = shlex.split(rest)
    except ValueError:  # 引号不配对时退回按空白切分
        tokens = rest.split()
    return [_strip_quotes(token) for token in tokens if token.strip()]


def _quotes_balanced(text: str) -> bool:
    """text 里各类引号是否都成对，成对说明内层引号是完整的。"""
    for opening, closing in set(_QUOTE_PAIRS.items()):
        if opening == closing:
            if text.count(opening) % 2:
                return False
        elif text.count(opening) != text.count(closing):
            return False
    return True


def _trim_stray_quote(text: str) -> str:
    """去掉两端落单的引号（只有一半、配不成对的情况）。"""
    if not text or _quotes_balanced(text):
        return text
    if text[0] in _QUOTE_PAIRS:
        return text[1:].strip()
    if text[-1] in _QUOTE_PAIRS.values():
        return text[:-1].strip()
    return text


def _parse_keyword_and_page(text: str) -> tuple[str, int]:
    """解析检索词与可选页码。

    引号只剥最外层一层，内部引号原样保留，例如
    ``“句子“引号内句子”，“不完整引号句”`` → ``句子“引号内句子”，“不完整引号句``。
    以右引号结尾时整段都算检索词，不会把末尾的数字误判成页码。
    """
    text = text.strip()
    if not text:
        return "", 1

    page = 1
    if text[-1] in _QUOTE_PAIRS.values():
        return _trim_stray_quote(_strip_quotes(text)), 1

    match = re.match(r"^(.*\S)\s+(\d+)$", text, re.DOTALL)
    if match:
        candidate = match.group(1).strip()
        # 候选词以左引号结尾，说明页码还没走出引号，不能当页码
        if candidate and candidate[-1] not in _QUOTE_PAIRS:
            page = max(1, int(match.group(2)))
            text = candidate

    return _trim_stray_quote(_strip_quotes(text)), page


def _parse_grep_args(message: str) -> tuple[str, str, int] | None:
    """解析 /grep 的主体、检索词与页码，失败返回 None。"""
    text = _MSG_ID_RE.sub(" ", message).strip()
    parts = text.split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        return None

    match = re.match(r"^(\S+)\s*(.*)$", rest, re.DOTALL)
    if match is None:
        return None
    subject = SUBJECT_ALIASES.get(_strip_quotes(match.group(1)).lower())
    if subject is None:
        return None

    keyword, page = _parse_keyword_and_page(match.group(2))
    if not keyword:
        return None
    return subject, keyword, page


def _parse_rewrite_args(
    raw: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """解析 /rewrite 参数，返回 (主体, 旧文本, 新文本, 错误)。"""
    raw = _MSG_ID_RE.sub("", raw).strip()
    # 裸 rewrite（不带参数）= 重发：不替换提示词，直接让模型重新生成上一条回复
    if re.match(r"^[^\w]?rewrite\s*$", raw, re.IGNORECASE):
        return "resend", None, None, None
    match = re.match(r"^[^\w]?rewrite\s+(user|ai|u|a)\s+(.*)", raw, re.IGNORECASE | re.DOTALL)
    if not match:
        return None, None, None, "指令格式错误，应为 rewrite user|u / ai|a <旧文本> <新文本>"
    target = {"u": "user", "a": "ai"}.get(match.group(1).lower(), match.group(1).lower())
    args_str = match.group(2).strip()

    delim_pairs = [
        ('"', '"'),
        ("'", "'"),
        ("(", ")"),
        ("\u201c", "\u201d"),
        ("\u2018", "\u2019"),
        ("「", "」"),
        ("『", "』"),
    ]
    for start, end in delim_pairs:
        if not args_str.startswith(start):
            continue
        # 逐个尝试结束符位置：只有当它后面紧跟第二个参数的起始符号时才算数，
        # 这样 “他说“你好”然后走了” 里的内层引号不会被误当结尾。
        pos = len(start)
        while True:
            end_idx = args_str.find(end, pos)
            if end_idx == -1:
                break
            rest = args_str[end_idx + len(end):].lstrip()
            if (
                rest.startswith(start)
                and rest.endswith(end)
                and len(rest) >= len(start) + len(end)
            ):
                return (
                    target,
                    args_str[len(start):end_idx],
                    rest[len(start):-len(end)],
                    None,
                )
            pos = end_idx + len(end)

        close_idx = args_str.find(end, len(start))
        if close_idx == -1:
            return None, None, None, f"未找到匹配的结束符号: {repr(end)}"
        if not args_str[close_idx + len(end):].strip():
            return None, None, None, "参数数量不足，需要两个参数（旧文本 和 新文本）"
        return None, None, None, "两个参数必须使用相同的包裹符号（检查引号是否成对）"

    tokens = args_str.split()
    if len(tokens) != 2:
        return None, None, None, "需要两个参数（若文本含空格，请使用引号或括号包裹）"
    return target, tokens[0], tokens[1], None


def _parse_prompt_text(text: str) -> str:
    """剥掉提示词内容最外层成对的引号，内层引号原样保留。"""
    text = _MSG_ID_RE.sub("", text).strip()
    if len(text) >= 2 and _QUOTE_PAIRS.get(text[0]) == text[-1]:
        return text[1:-1].strip()
    return _trim_stray_quote(text)


def _parse_addprompt_args(message: str) -> tuple[str, str] | None:
    """解析 /addprompt 的位置与内容，失败返回 None。"""
    text = _MSG_ID_RE.sub(" ", message).strip()
    parts = text.split(maxsplit=2)
    if len(parts) < 3:
        return None
    position = PROMPT_POSITIONS.get(_strip_quotes(parts[1]).lower())
    if position is None:
        return None
    content = _parse_prompt_text(parts[2])
    if not content:
        return None
    return position, content


def _parse_coverprompt_args(message: str) -> str:
    """解析 /coverprompt 的内容，空内容返回空字符串。"""
    text = _MSG_ID_RE.sub(" ", message).strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    return _parse_prompt_text(parts[1])


class ConversationEnhance(Star):
    """提供 /grep、/show、/fork、/rewrite 与提示词插入/覆盖指令。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}
        #: 提示词配置缓存，None 表示尚未从磁盘读取
        self._prompts: dict[str, dict] | None = None

    # ------------------------------------------------------------------ 指令

    @filter.command("grep")
    async def cmd_grep(self, event: AstrMessageEvent) -> None:
        """按关键词检索对话轮次"""
        if not self._permission_ok(event, "grep"):
            return
        parsed = _parse_grep_args(event.message_str)
        if parsed is None:
            self._reply(event, USAGE_GREP)
            return
        subject, keyword, page = parsed

        conversations = await self._collect_conversations(event)
        if conversations is None:
            return

        entries: list[tuple[str, str, Round]] = []
        for title, cid, history_raw in conversations:
            rounds = build_rounds(
                load_history(history_raw),
                strip_reminder=self._bool("strip_system_reminder", True),
            )
            for round_ in rounds:
                if round_matches(round_, subject, keyword):
                    entries.append((title, cid, round_))

        if not entries:
            self._reply(
                event,
                f'没有找到 {"用户" if subject == "user" else "AI"} 侧包含「{keyword}」的轮次。',
            )
            return

        page_size = max(1, self._int("page_size", 3))
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        page = min(page, total_pages)
        chunk = entries[(page - 1) * page_size : page * page_size]

        limit = self._int("max_chars", 150)
        budget = self._int("max_total_chars", 600)
        show_conv = len(conversations) > 1
        subject_arg = "user" if subject == "user" else "ai"
        header = (
            f"以下是检索结果（共 {len(entries)} 条 "
            f"| 第 {page} 页 / 共 {total_pages} 页）："
        )
        lines = [header, ""]
        used = len(header) + 2
        shown = 0
        omitted = 0
        last_cid = None

        for title, cid, round_ in chunk:
            block: list[str] = []
            if show_conv and cid != last_cid:
                block.append(f"【对话：{title}（{cid[:4]}）】")
            block.append(f"第 {round_.number} 轮")
            block.append(f"user：{display_text(round_.user_text or '（无）', limit)}")
            block.append(
                f"assistant：{display_text(round_.assistant_text or '（无）', limit)}"
            )
            block.append("")

            block_len = sum(len(item) + 1 for item in block)
            if budget > 0 and shown > 0 and used + block_len > budget:
                omitted = len(chunk) - shown
                break

            lines.extend(block)
            used += block_len
            shown += 1
            last_cid = cid

        if omitted:
            lines.append(f"…（本页其余 {omitted} 条已省略，可用更精确的检索词或翻页）")

        if total_pages > 1:
            lines.append(f'*输入 /grep {subject_arg} "{keyword}" {page + 1} 翻页')

        self._reply(event, "\n".join(lines).rstrip())

    @filter.command("show")
    async def cmd_show(self, event: AstrMessageEvent) -> None:
        """原样回看指定轮次的完整对话"""
        if not self._permission_ok(event, "show"):
            return
        args = _command_args(event.message_str)
        number = int(args[0]) if args and args[0].isdigit() else 0

        conv = await self._current_conversation(event)
        if conv is None:
            return

        rounds = build_rounds(
            load_history(conv.history),
            strip_reminder=self._bool("strip_system_reminder", True),
        )
        if not rounds:
            self._reply(event, "当前对话还没有历史记录。")
            return
        if number < 1 or number > len(rounds):
            self._reply(
                event,
                f"{USAGE_SHOW}\n当前对话共 {len(rounds)} 轮，可用 1 ~ {len(rounds)}。",
            )
            return

        round_ = rounds[number - 1]

        # 先发用户侧（同一轮的多条 user 消息合并成一条）
        if round_.user_texts:
            await event.send(event.plain_result(round_.user_text))

        await asyncio.sleep(self._float("show_delay_seconds", 10.0))

        # AI 侧按条发送，不合并，也不发送工具调用内容
        if not round_.assistant_texts:
            await event.send(event.plain_result("（该轮没有 AI 回复）"))
            return
        for text in round_.assistant_texts:
            await event.send(event.plain_result(text))

    @filter.command("fork")
    async def cmd_fork(self, event: AstrMessageEvent) -> None:
        """把前 N 轮复制成新的对话分支"""
        if not self._permission_ok(event, "fork"):
            return
        args = _command_args(event.message_str)
        number = int(args[0]) if args and args[0].isdigit() else 0

        conv = await self._current_conversation(event)
        if conv is None:
            return

        history = load_history(conv.history)
        rounds = build_rounds(
            history,
            strip_reminder=self._bool("strip_system_reminder", True),
        )
        if not rounds:
            self._reply(event, "当前对话还没有历史记录，无法创建分支。")
            return
        if number < 1 or number > len(rounds):
            self._reply(
                event,
                f"{USAGE_FORK}\n当前对话共 {len(rounds)} 轮，可用 1 ~ {len(rounds)}。",
            )
            return

        end = rounds[number - 1].end
        branch_history = history[: end + 1]
        title = f"{conv.title or '新对话'}-分支"

        try:
            new_cid = await self.context.conversation_manager.new_conversation(
                event.unified_msg_origin,
                event.get_platform_id(),
                content=branch_history,
                title=title,
                persona_id=conv.persona_id,
            )
        except Exception as exc:  # 建分支失败不应让指令崩溃
            logger.error(f"[conversation_enhance] 创建分支失败：{exc}", exc_info=True)
            self._reply(event, f"创建分支失败：{type(exc).__name__}: {exc}")
            return

        self._reply(
            event,
            "已创建分支："
            f"{title}（{new_cid[:4]}）\n"
            f"包含第 1 ~ {number} 轮，共 {len(branch_history)} 条记录。\n"
            "原对话内容保留不变，当前已切换到新分支。",
        )

    @filter.command("rewrite")
    async def cmd_rewrite(self, event: AstrMessageEvent) -> None:
        """改写最后一条用户消息或 AI 回复"""
        if not self._permission_ok(event, "rewrite"):
            return

        target, old, new, err = _parse_rewrite_args(event.message_str)
        if err:
            logger.info(f"[rewrite] 解析失败: {err}")
            self._reply(event, f"[FAIL] {err}")
            return
        logger.info(f"[rewrite] target={target}, old={repr(old)}, new={repr(new)}")

        if target == "ai" and not self._bool("allow_modify_assistant", True):
            self._reply(event, "[FAIL] 修改 AI 记忆功能未开启")
            return

        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager

        try:
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        except Exception:
            logger.error(f"获取对话ID失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 获取对话 ID 失败")
            return
        if not curr_cid:
            self._reply(event, "[FAIL] 无法获取当前对话 ID")
            return

        try:
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
        except Exception:
            logger.error(f"获取对话对象失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 获取对话对象失败")
            return
        if not conversation or not conversation.history:
            self._reply(event, "[FAIL] 当前对话没有可修改的历史")
            return

        history = self._history_list(conversation.history)
        if not history:
            self._reply(event, "[FAIL] 对话历史格式异常")
            return

        if target == "resend":
            await self._handle_resend(event, history, umo, curr_cid)
        elif target == "user":
            await self._handle_user_rewrite(event, history, old, new, umo, curr_cid)
        else:
            await self._handle_ai_rewrite(event, history, old, new, umo, curr_cid)

    @filter.command("rewrite_help")
    async def cmd_rewrite_help(self, event: AstrMessageEvent) -> None:
        """查看 /rewrite 用法"""
        self._reply(
            event,
            "【对话增强】改写帮助\n"
            "重发（重新生成上一条回复，不改提示词）：/rewrite\n"
            '修改自己最后一条消息：/rewrite user|u "旧文本" "新文本"\n'
            '修改 AI 最后一条回复：/rewrite ai|a "旧文本" "新文本"\n'
            "支持子串替换，若重复多处会提示；文本含空格请用引号或括号包裹。\n"
            "引号可嵌套，例如 /rewrite u “他说“你好”然后走了” “新的句子”。\n"
            "支持的包裹符号：英文双引号、英文单引号、半角圆括号、中文引号、直角引号",
        )

    # ---------------------------------------------------------- 提示词插入/覆盖

    @filter.command("showprompt")
    async def cmd_showprompt(self, event: AstrMessageEvent) -> None:
        """查看本作用域已配置的提示词"""
        if not self._prompt_ready(event):
            return
        umo = event.unified_msg_origin
        entry = self._load_prompts().get(umo) or {}
        if not entry:
            self._reply(event, f"当前作用域（{umo}）没有配置提示词，AI 使用默认提示词。")
            return

        limit = self._int("prompt_preview_chars", 200)
        lines = [f"当前作用域：{umo}"]
        if entry.get("cover"):
            lines.append(f"覆盖：{display_text(entry['cover'], limit)}")
        else:
            lines.append("覆盖：未设置（使用默认提示词）")
        lines.append(
            f"头部追加：{display_text(entry['front'], limit) if entry.get('front') else '无'}"
        )
        lines.append(
            f"尾部追加：{display_text(entry['tail'], limit) if entry.get('tail') else '无'}"
        )
        self._reply(event, "\n".join(lines))

    @filter.command("addprompt")
    async def cmd_addprompt(self, event: AstrMessageEvent) -> None:
        """在默认提示词前后追加内容"""
        if not self._prompt_ready(event):
            return
        parsed = _parse_addprompt_args(event.message_str)
        if parsed is None:
            self._reply(event, f"用法错误。\n{USAGE_PROMPT}")
            return
        position, content = parsed

        prompts = self._load_prompts()
        umo = event.unified_msg_origin
        entry = dict(prompts.get(umo) or {})
        existing = str(entry.get(position) or "").strip()
        entry[position] = f"{existing}\n{content}".strip() if existing else content
        prompts[umo] = entry
        if not self._save_prompts():
            self._reply(event, "[FAIL] 保存提示词失败，请查看日志")
            return

        label = "头部" if position == "front" else "尾部"
        self._reply(
            event,
            f"已在本作用域（{umo}）的{label}追加提示词，当前{label}共 {len(entry[position])} 字。",
        )

    @filter.command("coverprompt")
    async def cmd_coverprompt(self, event: AstrMessageEvent) -> None:
        """用新内容覆盖默认提示词"""
        if not self._prompt_ready(event):
            return
        content = _parse_coverprompt_args(event.message_str)
        if not content:
            self._reply(event, f"用法错误。\n{USAGE_PROMPT}")
            return

        prompts = self._load_prompts()
        umo = event.unified_msg_origin
        entry = dict(prompts.get(umo) or {})
        entry["cover"] = content
        prompts[umo] = entry
        if not self._save_prompts():
            self._reply(event, "[FAIL] 保存提示词失败，请查看日志")
            return

        self._reply(
            event,
            f"已覆盖本作用域（{umo}）的默认提示词，当前覆盖内容共 {len(content)} 字。",
        )

    @filter.command("resetprompt")
    async def cmd_resetprompt(self, event: AstrMessageEvent) -> None:
        """恢复默认提示词"""
        if not self._prompt_ready(event):
            return
        prompts = self._load_prompts()
        umo = event.unified_msg_origin
        if umo not in prompts:
            self._reply(event, "当前作用域没有配置提示词，无需恢复。")
            return
        prompts.pop(umo, None)
        if not self._save_prompts():
            self._reply(event, "[FAIL] 保存提示词失败，请查看日志")
            return
        self._reply(event, "已恢复默认提示词。")

    @filter.on_llm_request()
    async def inject_prompt(self, event: AstrMessageEvent, req: Any) -> None:
        """请求 LLM 前按作用域改写系统提示词。"""
        req.system_prompt = self._apply_prompt_custom(event, req.system_prompt or "")

    # -------------------------------------------------------------- 改写实现

    async def _handle_user_rewrite(
        self,
        event: AstrMessageEvent,
        history: List[dict],
        old: str,
        new: str,
        umo: str,
        curr_cid: str,
    ) -> None:
        # 1. 定位最后一条用户消息
        last_user_idx, last_user = self._find_last_by_role(history, "user")
        if last_user_idx == -1:
            self._reply(event, "[FAIL] 没有找到用户消息")
            return

        user_text, system_items = self._split_content(last_user.get("content"))
        if old not in user_text:
            self._reply(event, "[FAIL] 未找到匹配的原文，请检查后重试")
            return
        if user_text.count(old) > 1:
            self._reply(event, "[FAIL] 匹配到多处相同的文本，请提供更多特征文本以避免歧义")
            return

        # 2. 构造新用户消息
        new_user_text = user_text.replace(old, new, 1)
        new_content = system_items + [{"type": "text", "text": new_user_text}]
        new_user_msg = {"role": "user", "content": new_content}

        # 3. 确定删除区间（确保 end >= start）
        last_ai_idx, _ = self._find_last_by_role(history, "assistant")
        delete_start = last_user_idx
        delete_end = max(last_ai_idx, last_user_idx)

        temp_history = [msg for i, msg in enumerate(history) if i < delete_start or i > delete_end]
        temp_history.append(new_user_msg)

        # 4. 加载人格 system_prompt，并叠加本作用域配置的提示词
        system_prompt = await self._load_system_prompt(umo, curr_cid)
        system_prompt = self._apply_prompt_custom(event, system_prompt)

        # 5. 构建 LLM 上下文
        contexts = []
        if system_prompt:
            contexts.append(SystemMessageSegment(content=[TextPart(text=system_prompt)]))
        contexts.extend(self._history_to_message_segments(temp_history))

        # 6. 调用 LLM
        try:
            prov_id = await self.context.get_current_chat_provider_id(umo)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                contexts=contexts,
            )
            assistant_text = llm_resp.completion_text
        except asyncio.TimeoutError:
            logger.error("LLM 调用超时")
            self._reply(event, "[FAIL] 调用 LLM 超时，历史未修改")
            return
        except Exception:
            logger.error(f"LLM 调用失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 调用 LLM 失败，历史未修改")
            return

        # 7. 处理并发新消息
        try:
            fresh_conv = await self.context.conversation_manager.get_conversation(umo, curr_cid)
            if fresh_conv and fresh_conv.history:
                fresh_history = self._history_list(fresh_conv.history)
                extra_msgs = fresh_history[len(history):] if len(fresh_history) > len(history) else []
            else:
                extra_msgs = []
        except Exception:
            logger.error(f"并发检查失败: {traceback.format_exc()}")
            extra_msgs = []

        # 8. 安全修改历史（先删除旧区间，再追加本次重写的新对话，最后追加并发消息）
        self._remove_range(history, delete_start, delete_end)
        # 先添加本次重写产生的新用户消息和 AI 回复
        history.append(new_user_msg)
        history.append({"role": "assistant", "content": [{"type": "text", "text": assistant_text}]})
        # 再追加 LLM 期间并发的新消息（它们应该发生在本次重写之后）
        if extra_msgs:
            history.extend(extra_msgs)
            logger.info(f"[rewrite] 追加了 {len(extra_msgs)} 条并发消息（已保证时间顺序）")

        # 9. 持久化
        try:
            await self.context.conversation_manager.update_conversation(umo, curr_cid, history=history)
            logger.info("[rewrite] 新对话对已保存")
        except Exception:
            logger.error(f"保存对话失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 修改成功但保存失败")
            return

        self._reply(event, assistant_text)

    async def _handle_resend(
        self,
        event: AstrMessageEvent,
        history: List[dict],
        umo: str,
        curr_cid: str,
    ) -> None:
        """裸 /rewrite（重发）：不替换任何提示词，直接用历史让模型重新生成上一条回复。"""
        last_user_idx, _ = self._find_last_by_role(history, "user")
        if last_user_idx == -1:
            self._reply(event, "[FAIL] 没有找到可重发的用户消息")
            return

        # 只丢掉末尾那条 AI 回复，用户那条原样保留（不替换、不重建）
        drop_idx = None
        last_msg = history[-1]
        if isinstance(last_msg, dict) and last_msg.get("role") == "assistant":
            drop_idx = len(history) - 1
        temp_history = [msg for i, msg in enumerate(history) if i != drop_idx]
        logger.info(
            f"[rewrite] 重发: 上下文 {len(temp_history)} 条, 丢弃末尾 AI 回复: {drop_idx is not None}"
        )

        # 加载人格 system_prompt，并叠加本作用域配置的提示词
        system_prompt = await self._load_system_prompt(umo, curr_cid)
        system_prompt = self._apply_prompt_custom(event, system_prompt)

        # 构建 LLM 上下文（只有历史，不注入任何新提示词）
        contexts = []
        if system_prompt:
            contexts.append(SystemMessageSegment(content=[TextPart(text=system_prompt)]))
        contexts.extend(self._history_to_message_segments(temp_history))

        # 调用 LLM
        try:
            prov_id = await self.context.get_current_chat_provider_id(umo)
            llm_resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                contexts=contexts,
            )
            assistant_text = llm_resp.completion_text
        except asyncio.TimeoutError:
            logger.error("LLM 调用超时")
            self._reply(event, "[FAIL] 调用 LLM 超时，历史未修改")
            return
        except Exception:
            logger.error(f"LLM 调用失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 调用 LLM 失败，历史未修改")
            return

        if not assistant_text or not assistant_text.strip():
            logger.error("[rewrite] LLM 返回空回复，历史未修改")
            self._reply(event, "[FAIL] 模型返回空回复，历史未修改")
            return

        # 处理并发新消息
        try:
            fresh_conv = await self.context.conversation_manager.get_conversation(umo, curr_cid)
            if fresh_conv and fresh_conv.history:
                fresh_history = self._history_list(fresh_conv.history)
                extra_msgs = fresh_history[len(history):] if len(fresh_history) > len(history) else []
            else:
                extra_msgs = []
        except Exception:
            logger.error(f"并发检查失败: {traceback.format_exc()}")
            extra_msgs = []

        # 用新回复顶掉旧的那条 AI 回复（用户消息保持原样）
        if drop_idx is not None:
            del history[drop_idx]
        history.append({"role": "assistant", "content": [{"type": "text", "text": assistant_text}]})
        if extra_msgs:
            history.extend(extra_msgs)
            logger.info(f"[rewrite] 追加了 {len(extra_msgs)} 条并发消息（已保证时间顺序）")

        # 持久化
        try:
            await self.context.conversation_manager.update_conversation(umo, curr_cid, history=history)
            logger.info("[rewrite] 重发结果已保存")
        except Exception:
            logger.error(f"保存对话失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 修改成功但保存失败")
            return

        self._reply(event, assistant_text)

    async def _handle_ai_rewrite(
        self,
        event: AstrMessageEvent,
        history: List[dict],
        old: str,
        new: str,
        umo: str,
        curr_cid: str,
    ) -> None:
        last_ai_idx, last_ai = self._find_last_by_role(history, "assistant")
        if last_ai_idx == -1:
            self._reply(event, "[FAIL] 没有找到 AI 回复")
            return
        ai_text, _ = self._split_content(last_ai.get("content"))
        if old not in ai_text:
            self._reply(event, "[FAIL] 未找到匹配的原文，请检查后重试")
            return
        if ai_text.count(old) > 1:
            self._reply(event, "[FAIL] 匹配到多处相同的文本，请提供更多特征文本以避免歧义")
            return
        new_ai_text = ai_text.replace(old, new, 1)
        last_ai["content"] = [{"type": "text", "text": new_ai_text}]

        try:
            await self.context.conversation_manager.update_conversation(umo, curr_cid, history=history)
            logger.info("[rewrite] AI 记忆已更新")
        except Exception:
            logger.error(f"保存 AI 记忆失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 保存失败")
            return
        self._reply(event, "[OK] AI 记忆已修正，下次对话将基于新记忆")

    # ------------------------------------------------------------------ 辅助

    def _reply(self, event: AstrMessageEvent, text: str) -> None:
        """设置指令的返回文本（关闭文转图，避免长文本被渲染）。"""
        event.set_result(MessageEventResult().message(text).use_t2i(False))

    # ---- 提示词存储 ----

    def _prompt_path(self) -> Path:
        """提示词数据文件：data/plugin_data/<插件名>/prompts.json。"""
        return StarTools.get_data_dir(PLUGIN_NAME) / "prompts.json"

    def _load_prompts(self) -> dict:
        if self._prompts is None:
            self._prompts = self._read_prompts()
        return self._prompts

    def _read_prompts(self) -> dict:
        try:
            with self._prompt_path().open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return {str(key): value for key, value in data.items() if isinstance(value, dict)}
            logger.warning("[prompt] prompts.json 不是对象，已忽略")
        except FileNotFoundError:
            pass
        except Exception:
            logger.error(f"[prompt] 读取提示词失败: {traceback.format_exc()}")
        return {}

    def _save_prompts(self) -> bool:
        try:
            path = self._prompt_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._prompts or {}, fh, ensure_ascii=False, indent=2)
            tmp.replace(path)
            return True
        except Exception:
            logger.error(f"[prompt] 保存提示词失败: {traceback.format_exc()}")
            return False

    async def _current_conversation(self, event: AstrMessageEvent) -> Any:
        """取当前对话，没有则回复提示并返回 None。"""
        umo = event.unified_msg_origin
        conv_mgr = self.context.conversation_manager
        cid = await conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            self._reply(event, "当前未处于对话状态，请先发送消息或使用 /new 创建对话。")
            return None
        conv = await conv_mgr.get_conversation(umo, cid)
        if conv is None:
            self._reply(event, "找不到当前对话，可能已被删除。")
            return None
        return conv

    async def _collect_conversations(
        self, event: AstrMessageEvent
    ) -> list[tuple[str, str, str | None]] | None:
        """按配置范围收集待检索的对话，返回 [(标题, cid, history)]。"""
        scope = str(self._raw("grep_scope", "all") or "all").strip().lower()
        conv_mgr = self.context.conversation_manager
        umo = event.unified_msg_origin

        if scope == "current":
            conv = await self._current_conversation(event)
            if conv is None:
                return None
            return [(conv.title or "新对话", conv.cid, conv.history)]

        conversations = await conv_mgr.get_conversations(umo)
        if not conversations:
            self._reply(event, "当前会话下还没有任何对话记录。")
            return None

        conversations = sorted(conversations, key=lambda item: item.created_at)
        return [(conv.title or "新对话", conv.cid, conv.history) for conv in conversations]

    async def _load_system_prompt(self, umo: str, curr_cid: str) -> str:
        """加载当前对话使用的人格 system_prompt（取不到就返回空字符串）。"""
        try:
            conv_mgr = self.context.conversation_manager
            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            persona_id = conversation.persona_id if conversation else None
            if persona_id:
                persona = await self.context.persona_manager.get_persona(persona_id)
                if persona:
                    system_prompt = persona.system_prompt or ""
                    logger.info(f"[rewrite] 已加载人格: {persona_id}, 长度: {len(system_prompt)}")
                    return system_prompt
                logger.warning(f"[rewrite] 人格未找到: {persona_id}")
            else:
                default_persona = await self.context.persona_manager.get_default_persona_v3(umo)
                if default_persona and "prompt" in default_persona:
                    logger.info("[rewrite] 使用默认人格")
                    return default_persona["prompt"]
        except Exception:
            logger.error(f"加载人格失败: {traceback.format_exc()}")
        return ""

    @staticmethod
    def _split_content(content: Any) -> Tuple[str, List[dict]]:
        """拆出纯文本与需要保留的非文本/系统部分。"""
        if isinstance(content, str):
            return _MSG_ID_RE.sub("", content).strip(), []
        if isinstance(content, list):
            user_parts = []
            system_items = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    raw = item.get("text", "")
                    if raw.startswith("<system_reminder"):
                        system_items.append(item)
                    else:
                        user_parts.append(_MSG_ID_RE.sub("", raw))
                else:
                    system_items.append(item)
            return "".join(user_parts).strip(), system_items
        return str(content).strip(), []

    @staticmethod
    def _history_list(history_raw: Any) -> List[dict]:
        """把对话历史（JSON 字符串或列表）统一成消息列表。"""
        if isinstance(history_raw, list):
            return list(history_raw)
        if isinstance(history_raw, str):
            try:
                data = json.loads(history_raw)
            except json.JSONDecodeError:
                logger.error("历史记录 JSON 解析失败")
                return []
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    logger.error("二次 JSON 解析失败")
                    return []
            if isinstance(data, list):
                return list(data)
        return []

    @staticmethod
    def _find_last_by_role(history: List[dict], role: str) -> Tuple[int, Optional[dict]]:
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            if isinstance(msg, dict) and msg.get("role") == role:
                return i, msg
        return -1, None

    @staticmethod
    def _remove_range(history: List[dict], start: int, end: int) -> None:
        if 0 <= start <= end < len(history):
            del history[start:end + 1]
        else:
            logger.warning(f"_remove_range 索引无效: start={start}, end={end}, len={len(history)}")

    @staticmethod
    def _history_to_message_segments(history: List[dict]) -> List[Any]:
        segments: list[Any] = []
        for msg in history:
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                non_text_count = 0
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    else:
                        non_text_count += 1
                if non_text_count > 0:
                    logger.warning(f"消息包含 {non_text_count} 个非文本组件，将被丢弃")
                text = "".join(text_parts)
            else:
                text = str(content)

            role = msg.get("role")
            if role == "user":
                segments.append(UserMessageSegment(content=[TextPart(text=text)]))
            elif role == "assistant":
                segments.append(AssistantMessageSegment(content=[TextPart(text=text)]))
            elif role == "system":
                segments.append(SystemMessageSegment(content=[TextPart(text=text)]))
        return segments

    # ---- 权限控制 ----

    def _permission_config(self, command: str) -> dict:
        """取某个指令的权限配置，格式异常时按空配置处理。"""
        raw = self.config.get(f"{command}_permission", {}) if self.config else {}
        return raw if isinstance(raw, dict) else {}

    def _prompt_feature_enabled(self) -> bool:
        """提示词功能总开关，默认关闭。"""
        return self._as_bool(self._permission_config("prompt").get("enabled"), False)

    def _prompt_ready(self, event: AstrMessageEvent) -> bool:
        """提示词指令的前置检查：功能开关 + 群聊/名单权限。"""
        if not self._prompt_feature_enabled():
            self._reply(
                event,
                "提示词功能未开启，请先在插件配置中打开 prompt_permission 的 enabled。",
            )
            return False
        return self._permission_ok(event, "prompt")

    def _apply_prompt_custom(self, event: AstrMessageEvent, base: str) -> str:
        """把本作用域配置的提示词套到 base 上。

        普通对话走 on_llm_request 钩子，/rewrite 内部直接调用 llm_generate
        绕过了钩子，所以两处都调用本方法，保证改写时同样继承私有提示词。
        """
        if not self._prompt_feature_enabled():
            return base
        if not self._permission_ok(event, "prompt", notify=False):
            return base
        entry = self._load_prompts().get(event.unified_msg_origin) or {}
        if not entry:
            return base

        text = str(entry.get("cover") or base or "").strip()
        front = str(entry.get("front") or "").strip()
        tail = str(entry.get("tail") or "").strip()
        return "\n".join(part for part in (front, text, tail) if part)

    def _permission_ok(
        self, event: AstrMessageEvent, command: str, notify: bool = True
    ) -> bool:
        """按指令独立校验：群聊开关 + ID 黑/白名单。"""
        conf = self._permission_config(command)
        group_id = event.get_group_id()
        # rewrite / prompt 默认不允许群聊，其余指令默认允许
        default_group = command not in {"rewrite", "prompt"}
        if group_id and not self._as_bool(conf.get("allow_group"), default_group):
            logger.info(f"[permission] {command} 在群聊 {group_id} 被禁用")
            return False

        mode = str(conf.get("mode") or "off").strip().lower()
        if mode not in {"whitelist", "blacklist"}:
            return True
        ids = conf.get("ids") or []
        if isinstance(ids, str):
            ids = [ids]
        hit = self._identity_hit(event, ids)
        if (mode == "whitelist" and not hit) or (mode == "blacklist" and hit):
            logger.info(f"[permission] {command} 被拒绝：mode={mode}, 命中={hit}")
            if notify:
                if command == "prompt":
                    self._reply(event, "[FAIL] 没有使用提示词指令的权限")
                else:
                    self._reply(event, f"[FAIL] 没有使用 /{command} 的权限")
            return False
        return True

    @staticmethod
    def _identity_hit(event: AstrMessageEvent, ids: Any) -> bool:
        """ids 里是否包含发送者 ID、会话 ID、UMO 或群号。

        只依赖 AstrBot 公共事件接口，不区分平台：私聊时 session_id 就是对方 ID，
        群聊时是群号；部分平台拿不到 sender_id 时仍能靠 session_id 命中。
        """
        candidates = {str(event.unified_msg_origin)}
        try:
            sender_id = event.get_sender_id()
            if sender_id:
                candidates.add(str(sender_id))
        except Exception:  # 个别平台事件缺少发送者信息
            pass
        try:
            session_id = event.get_session_id()
            if session_id:
                candidates.add(str(session_id))
        except Exception:
            pass
        group_id = event.get_group_id()
        if group_id:
            candidates.add(str(group_id))
        for raw in ids:
            token = str(raw).strip()
            if token and token in candidates:
                return True
        return False

    # ---- 配置读取 ----

    def _raw(self, key: str, default: Any) -> Any:
        value = self.config.get(key, default)
        return default if value is None else value

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _bool(self, key: str, default: bool) -> bool:
        return self._as_bool(self._raw(key, default), default)

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._raw(key, default))
        except (TypeError, ValueError):
            return default

    def _float(self, key: str, default: float) -> float:
        try:
            value = float(self._raw(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default
