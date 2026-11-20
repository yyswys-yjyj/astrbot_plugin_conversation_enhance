"""对话增强插件：/grep 检索、/show 回看、/fork 分支、/rewrite 改写、提示词插入/覆盖。

前四个指令都围绕"轮"工作：一条 user 消息开始一轮，其后所有 assistant 消息
（含它们之间的 tool 记录）属于同一轮，轮次按时间线从 1 开始编号。
/rewrite 用于改写最后一条用户消息或 AI 回复。
提示词指令按"单个用户/群"作用域改写系统提示词。
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import shlex
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
    inline,
    load_history,
    round_matches,
    truncate,
)

USAGE_GREP = (
    '用法：/grep [user|u|ai|a] ["-ai"|"-rag"] "检索词" [页码]\n'
    '  子串检索：/grep u "某件事" 1\n'
    '  AI 检索：/grep u -ai "某件事"（最多 3 条，无页码）\n'
    '  RAG 检索：/grep u -rag "某件事" 1（需要已配置嵌入模型）'
)
USAGE_SHOW = "用法：/show 轮次，例如 /show 3"
USAGE_FORK = "用法：/fork 轮次，例如 /fork 3（把第 1~3 轮复制成新分支）"
USAGE_AICOMMENT = (
    '用法：/aicomment "要求"，例如 /aicomment "用三句话复述这段对话"'
)
AICOMMENT_SYSTEM_PROMPT = (
    "你在阅读一段用户与 AI 的对话记录。请严格按照用户的要求复述或评论这段对话，"
    "只输出结果本身，不要复述要求，也不要提及这些指令。"
)

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

#: 重写版本暂存过期时间（分钟）与后台清理间隔（秒），与「遗忘」插件一致。
REWRITE_EXPIRE_MINUTES = 30
CLEANUP_INTERVAL_SECONDS = 300

#: 配额记录的默认窗口与次数（配置未填时使用）
QUOTA_DEFAULT_MINUTES = 1
QUOTA_DEFAULT_COUNT = 5
#: 指令失败时打的标记，避免失败请求消耗配额
QUOTA_FAILED_FLAG = "_quota_failed"


def _quota_guard(command: str):
    """给指令加配额检查：先查窗口，执行成功后才记录时间戳。"""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, event: AstrMessageEvent, *args, **kwargs):
            if not await self._quota_check(event, command):
                return
            try:
                event.set_extra(QUOTA_FAILED_FLAG, False)
            except Exception:
                pass
            await func(self, event, *args, **kwargs)
            failed = False
            try:
                failed = bool(event.get_extra(QUOTA_FAILED_FLAG))
            except Exception:
                pass
            if not failed:
                await self._quota_commit(event, command)

        return wrapper

    return decorator


@dataclass
class RewriteRecord:
    """一轮对话的重写版本暂存（对应遗忘插件的 DeletedRecord）。

    只放在内存里，作为这一轮的可回退快照；下一条 LLM 请求开始或超时后清掉。
    """

    cid: str
    base_len: int
    original_content: Any
    original_assistant: str = ""
    versions: List[dict] = field(default_factory=list)
    current: int = 0
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def round_count(self) -> int:
        """累计重写次数。"""
        return len(self.versions)

    def touch(self) -> None:
        """刷新时间戳，避免活跃记录被清理。"""
        self.timestamp = datetime.now()

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

#: 生成会话标题时给模型的系统提示词。
TITLE_SYSTEM_PROMPT = (
    "你是对话标题生成器。根据用户给出的对话内容生成一个简短标题，"
    "只输出标题本身，不要解释、不要引号、不要结尾标点。"
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


def _parse_grep_args(message: str) -> tuple[str, str, int, str] | None:
    """解析 /grep 的主体、检索词、页码与检索模式，失败返回 None。

    模式由主体后的 ``-ai`` / ``-rag`` 开关决定，缺省为子串检索。
    """
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

    mode = "substring"
    remainder = match.group(2).strip()
    flag = re.match(r"^--?(ai|rag)\s+(.*)$", remainder, re.IGNORECASE | re.DOTALL)
    if flag:
        mode = flag.group(1).lower()
        remainder = flag.group(2)

    keyword, page = _parse_keyword_and_page(remainder)
    if not keyword:
        return None
    return subject, keyword, page, mode


def _parse_index_list(text: str, limit: int) -> list[int]:
    """从模型输出里解析轮次下标数组，非法项丢弃并按上限过滤。"""
    match = re.search(r"\[[^\]]*\]", text or "", re.DOTALL)
    if not match:
        return []

    payload = match.group(0)
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        data = re.findall(r"\d+", payload)

    result: list[int] = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            index = item
        elif isinstance(item, float):
            index = int(item)
        elif isinstance(item, str) and item.strip().isdigit():
            index = int(item.strip())
        else:
            continue
        if 1 <= index <= limit and index not in result:
            result.append(index)
    return result


def _cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度，长度不一致或空向量返回 0。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = sum(a * a for a in left) ** 0.5
    norm_right = sum(b * b for b in right) ** 0.5
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def _parse_rewrite_args(
    raw: str,
) -> Tuple[Optional[str], Any, Any, Optional[str]]:
    """解析 /rewrite 参数，返回 (动作, 参数1, 参数2, 错误)。

    动作：resend / user / ai / get / set / view。
    """
    raw = _MSG_ID_RE.sub("", raw).strip()
    # 裸 rewrite（不带参数）= 重发：让模型重新生成当前版本的回复
    if re.match(r"^[^\w]?rewrite\s*$", raw, re.IGNORECASE):
        return "resend", None, None, None

    match = re.match(
        r"^[^\w]?rewrite\s+(get|set|view|user|ai|u|a)\b\s*(.*)$",
        raw,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return (
            None,
            None,
            None,
            "指令格式错误，应为 rewrite u|ai <旧文本> <新文本> / get / set n / view n",
        )

    sub = match.group(1).lower()
    rest = match.group(2).strip()

    if sub == "get":
        if rest:
            return None, None, None, "/rewrite get 不需要参数"
        return "get", None, None, None

    if sub in {"set", "view"}:
        if not rest.isdigit():
            return None, None, None, f"/rewrite {sub} 需要一个版本号（0 表示原版）"
        return sub, int(rest), None, None

    target = {"u": "user", "a": "ai"}.get(sub, sub)
    args_str = rest

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


def _clean_title(text: str, max_chars: int) -> str:
    """把模型输出整理成一行标题，并限制字数。"""
    text = (text or "").strip()
    for line in text.splitlines():
        if line.strip():
            text = line.strip()
            break
    text = re.sub(r"^[#>*\-–—\s]+", "", text)
    text = _strip_quotes(text)
    text = text.strip("。.!！?？:：;；,，、~～ \t\"'“”‘’「」『』【】[]()（）")
    text = re.sub(r"\s+", " ", text)
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


class ConversationEnhance(Star):
    """提供 /grep、/show、/fork、/rewrite 与提示词插入/覆盖指令。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}
        #: 提示词配置缓存，None 表示尚未从磁盘读取
        self._prompts: dict[str, dict] | None = None
        #: RAG 轮次向量缓存：cid -> (历史签名, 向量列表)
        self._rag_cache: dict[str, tuple[tuple[int, int], list[list[float]]]] = {}
        #: 重写版本暂存：umo -> cid -> RewriteRecord（仅内存，随插件生命周期）
        self._rewrite_store: dict[str, dict[str, RewriteRecord]] = {}
        #: 指令配额记录（懒加载：首次访问才读磁盘）
        self._quotas: dict[str, list[float]] | None = None
        #: 配额读写锁
        self._quota_lock = asyncio.Lock()
        #: 等待官方链路回填回复的重写任务：umo -> 任务
        self._pending_rewrite: dict[str, dict] = {}
        #: 暂存并发锁（同遗忘插件）
        self._rewrite_lock = asyncio.Lock()
        #: 后台清理任务（同遗忘插件，随插件加载启动、terminate 时取消）
        self._cleanup_task: asyncio.Task | None = None
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # 没有运行中的事件循环（如单测直接构造）
            logger.warning("[rewrite] 未检测到事件循环，暂存清理任务未启动")
        else:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    # ------------------------------------------------------------- 暂存生命周期

    async def _cleanup_loop(self) -> None:
        """定期清理过期的版本暂存。"""
        while True:
            try:
                await self._cleanup_expired()
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                logger.info("[rewrite] 暂存清理任务已取消")
                break
            except Exception as exc:
                logger.error(f"[rewrite] 清理任务出错: {exc}")
                await asyncio.sleep(60)

    async def _cleanup_expired(self) -> None:
        """删除超过 REWRITE_EXPIRE_MINUTES 未更新的记录。"""
        limit = timedelta(minutes=REWRITE_EXPIRE_MINUTES)
        now = datetime.now()
        async with self._rewrite_lock:
            for umo, cid_map in list(self._rewrite_store.items()):
                for cid, rec in list(cid_map.items()):
                    if now - rec.timestamp > limit:
                        del cid_map[cid]
                        logger.info(f"[rewrite] 清理过期暂存：{umo} / {cid}")
                if not cid_map:
                    del self._rewrite_store[umo]

    @filter.on_llm_request()
    async def clear_staged_on_new_request(
        self, event: AstrMessageEvent, req: Any
    ) -> None:
        """新的一轮 LLM 请求开始时清掉上一轮的版本暂存。

        与遗忘插件的反悔窗口一致：暂存只服务于上一轮，对话继续往下走就失效。
        重写自身触发的请求带 `_rewrite_launched` 标记，不能清。
        """
        if event.get_extra("_rewrite_launched"):
            return
        umo = event.unified_msg_origin
        async with self._rewrite_lock:
            if self._rewrite_store.pop(umo, None):
                logger.info(f"[rewrite] 新请求开始，清除上一轮暂存：{umo}")

    # ------------------------------------------------------------------ 指令

    @filter.command("grep")
    @_quota_guard("grep")
    async def cmd_grep(self, event: AstrMessageEvent) -> None:
        """按关键词检索对话轮次"""
        if not self._permission_ok(event, "grep"):
            return
        parsed = _parse_grep_args(event.message_str)
        if parsed is None:
            self._reply(event, USAGE_GREP)
            return
        subject, keyword, page, mode = parsed

        conversations = await self._collect_conversations(event)
        if conversations is None:
            return

        if mode == "ai":
            await self._grep_by_ai(event, keyword, conversations)
            return
        if mode == "rag":
            await self._grep_by_rag(event, keyword, page, subject, conversations)
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

        subject_arg = "user" if subject == "user" else "ai"
        header = (
            f"以下是检索结果（共 {len(entries)} 条 "
            f"| 第 {page} 页 / 共 {total_pages} 页）："
        )
        next_hint = (
            f'*输入 /grep {subject_arg} "{keyword}" {page + 1} 翻页'
            if total_pages > 1
            else None
        )
        self._reply(
            event,
            self._render_entries(
                chunk,
                page,
                total_pages,
                len(conversations) > 1,
                header,
                next_hint,
            ),
        )

    def _render_entries(
        self,
        chunk: list,
        page: int,
        total_pages: int,
        show_conv: bool,
        header: str,
        next_hint: str | None,
    ) -> str:
        """渲染检索结果；条目可以是 (标题, cid, 轮次) 或带相似度的四元组。"""
        limit = self._int("max_chars", 150)
        budget = self._int("max_total_chars", 600)
        lines = [header, ""]
        used = len(header) + 2
        shown = 0
        omitted = 0
        last_cid = None

        for item in chunk:
            title, cid, round_ = item[0], item[1], item[2]
            score = item[3] if len(item) > 3 else None
            block: list[str] = []
            if show_conv and cid != last_cid:
                block.append(f"【对话：{title}（{cid[:4]}）】")
            block.append(f"第 {round_.number} 轮")
            if score is not None:
                block.append(f"相似度：{score:.3f}")
            block.append(f"user：{display_text(round_.user_text or '（无）', limit)}")
            block.append(
                f"assistant：{display_text(round_.assistant_text or '（无）', limit)}"
            )
            block.append("")

            block_len = sum(len(text) + 1 for text in block)
            if budget > 0 and shown > 0 and used + block_len > budget:
                omitted = len(chunk) - shown
                break

            lines.extend(block)
            used += block_len
            shown += 1
            last_cid = cid

        if omitted:
            lines.append(f"…（本页其余 {omitted} 条已省略，可用更精确的检索词或翻页）")
        if next_hint:
            lines.append(next_hint)
        return "\n".join(lines).rstrip()

    async def _grep_by_ai(
        self,
        event: AstrMessageEvent,
        keyword: str,
        conversations: list[tuple[str, str, str | None]],
    ) -> None:
        """让 LLM 从候选轮次里挑出最相关的至多 3 轮。"""
        max_rounds = max(1, self._int("grep_ai_max_rounds", 100))
        max_chars = max(0, self._int("grep_ai_max_chars", 12000))

        entries: list[tuple[str, str, Round]] = []
        lines: list[str] = []
        used = 0
        for title, cid, history_raw in conversations:
            rounds = build_rounds(
                load_history(history_raw),
                strip_reminder=self._bool("strip_system_reminder", True),
            )
            for round_ in rounds:
                if len(entries) >= max_rounds:
                    break
                text = (
                    f"[{len(entries) + 1}] 用户：{inline(round_.user_text)[:200]}\n"
                    f"AI：{inline(round_.assistant_text)[:200]}"
                )
                if max_chars and used + len(text) > max_chars:
                    break
                entries.append((title, cid, round_))
                lines.append(text)
                used += len(text) + 1
            if len(entries) >= max_rounds or (max_chars and used >= max_chars):
                break

        if not entries:
            self._reply(event, "当前会话下还没有可检索的对话历史。")
            return

        prompt = (
            f"检索需求：{keyword}\n\n"
            "下面是候选对话轮次，编号在方括号内：\n\n"
            + "\n".join(lines)
            + "\n\n请选出与检索需求最相关的最多 3 个轮次，"
            "只输出编号组成的 JSON 数组（如 [2, 5]），不要解释。"
        )
        try:
            prov_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                contexts=[
                    SystemMessageSegment(
                        content=[TextPart(text="你是对话检索助手，只输出 JSON 数组。")]
                    ),
                    UserMessageSegment(content=[TextPart(text=prompt)]),
                ],
            )
            indices = _parse_index_list(
                getattr(resp, "completion_text", "") or "", len(entries)
            )
        except Exception:
            logger.error(f"[grep] AI 检索失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] AI 检索失败，请稍后重试")
            return

        picks = [entries[index - 1] for index in indices[:3]]
        if not picks:
            self._reply(event, "AI 没有选出相关轮次，可以换个说法或改用子串检索。")
            return

        header = f"语义检索（AI 判断，共 {len(picks)} 条）："
        self._reply(
            event,
            self._render_entries(picks, 1, 1, len(conversations) > 1, header, None),
        )

    async def _grep_by_rag(
        self,
        event: AstrMessageEvent,
        keyword: str,
        page: int,
        subject: str,
        conversations: list[tuple[str, str, str | None]],
    ) -> None:
        """用嵌入模型做向量检索，按相似度排序并分页。"""
        providers = self.context.get_all_embedding_providers()
        if not providers:
            self._reply(
                event,
                "当前 AstrBot 没有配置嵌入模型（Embedding Provider），无法进行 RAG 检索。",
            )
            return

        provider = self._pick_embedding_provider(providers)
        round_chars = max(50, self._int("rag_round_chars", 500))

        targets: list[tuple[str, str, Round]] = []
        vectors: list[list[float]] = []
        for title, cid, history_raw in conversations:
            rounds = build_rounds(
                load_history(history_raw),
                strip_reminder=self._bool("strip_system_reminder", True),
            )
            if not rounds:
                continue
            texts = [
                f"用户：{inline(round_.user_text)}\nAI：{inline(round_.assistant_text)}"[
                    :round_chars
                ]
                for round_ in rounds
            ]
            cached = await self._rag_vectors(cid, history_raw, texts, provider)
            if cached is None:
                self._reply(event, "[FAIL] 向量计算失败，请查看日志")
                return
            for round_, vector in zip(rounds, cached):
                targets.append((title, cid, round_))
                vectors.append(vector)

        if not targets:
            self._reply(event, "当前会话下还没有可检索的对话历史。")
            return

        try:
            query_vector = await provider.get_embedding(keyword)
        except Exception:
            logger.error(f"[grep] RAG 查询向量失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 查询向量计算失败，请稍后重试")
            return

        scored = [
            (title, cid, round_, _cosine(query_vector, vector))
            for (title, cid, round_), vector in zip(targets, vectors)
        ]
        scored = [item for item in scored if item[3] > 0]
        if not scored:
            self._reply(event, "没有检索到相关轮次。")
            return
        scored.sort(key=lambda item: item[3], reverse=True)

        page_size = max(1, self._int("page_size", 3))
        total_pages = max(1, (len(scored) + page_size - 1) // page_size)
        page = min(max(1, page), total_pages)
        chunk = scored[(page - 1) * page_size : page * page_size]

        subject_arg = "user" if subject == "user" else "ai"
        header = (
            f"语义检索（RAG，共 {len(scored)} 条 "
            f"| 第 {page} 页 / 共 {total_pages} 页）："
        )
        next_hint = (
            f'*输入 /grep {subject_arg} -rag "{keyword}" {page + 1} 翻页'
            if total_pages > 1
            else None
        )
        self._reply(
            event,
            self._render_entries(
                chunk,
                page,
                total_pages,
                len(conversations) > 1,
                header,
                next_hint,
            ),
        )

    def _pick_embedding_provider(self, providers: list[Any]) -> Any:
        """按配置的 Provider ID 选嵌入模型，留空或找不到时用第一个。"""
        wanted = str(self._raw("rag_embedding_provider", "") or "").strip()
        if wanted:
            for provider in providers:
                config = getattr(provider, "provider_config", {}) or {}
                provider_id = config.get("id") or getattr(provider, "provider_id", "")
                if provider_id == wanted:
                    return provider
            logger.warning(f"[grep] 未找到嵌入模型 {wanted}，回退到第一个可用模型")
        return providers[0]

    async def _rag_vectors(
        self,
        cid: str,
        history_raw: str | None,
        texts: list[str],
        provider: Any,
    ) -> list[list[float]] | None:
        """计算并缓存每个对话的轮次向量，历史长度变化时重算。"""
        signature = (len(history_raw or ""), len(texts))
        cached = self._rag_cache.get(cid)
        if cached and cached[0] == signature:
            return cached[1]
        try:
            vectors = await provider.get_embeddings_batch(texts)
        except Exception:
            logger.error(f"[grep] 批量向量计算失败: {traceback.format_exc()}")
            return None
        if len(vectors) != len(texts):
            logger.error("[grep] 向量数量与文本数量不一致")
            return None
        if len(self._rag_cache) >= 8:
            self._rag_cache.clear()
        self._rag_cache[cid] = (signature, vectors)
        return vectors

    @filter.command("gettop")
    async def cmd_gettop(self, event: AstrMessageEvent) -> None:
        """查看当前对话的轮数"""
        conv = await self._current_conversation(event)
        if conv is None:
            return
        rounds = build_rounds(
            load_history(conv.history),
            strip_reminder=self._bool("strip_system_reminder", True),
        )
        self._reply(
            event,
            f"当前对话（{conv.title or '新对话'}）共 {len(rounds)} 轮。",
        )

    @filter.command("aicomment")
    @_quota_guard("aicomment")
    async def cmd_aicomment(self, event: AstrMessageEvent) -> None:
        """让 AI 按指定要求复述或评论当前对话"""
        if not self._permission_ok(event, "aicomment"):
            return

        requirement = _parse_prompt_text(
            re.sub(
                r"^\s*[^\w]?aicomment\b",
                "",
                event.message_str or "",
                count=1,
                flags=re.IGNORECASE,
            )
        )
        if not requirement:
            self._reply(event, USAGE_AICOMMENT)
            return

        conv = await self._current_conversation(event)
        if conv is None:
            return
        rounds = build_rounds(
            load_history(conv.history),
            strip_reminder=self._bool("strip_system_reminder", True),
        )
        if not rounds:
            self._reply(event, "当前对话还没有历史记录，无法评论。")
            return

        max_rounds = max(1, self._int("comment_max_rounds", 20))
        per_round = max(50, self._int("comment_round_chars", 300))
        lines: List[str] = []
        for index, round_ in enumerate(rounds[-max_rounds:], start=1):
            if round_.user_text:
                lines.append(
                    f"[{index}] 用户：{truncate(inline(round_.user_text), per_round)}"
                )
            if round_.assistant_text:
                lines.append(
                    f"[{index}] AI：{truncate(inline(round_.assistant_text), per_round)}"
                )
        if not lines:
            self._reply(event, "当前对话没有可用的文本内容。")
            return

        prompt = (
            "以下是当前对话记录：\n\n"
            + "\n".join(lines)
            + f"\n\n要求：{requirement}"
        )
        try:
            prov_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                contexts=[
                    SystemMessageSegment(
                        content=[TextPart(text=AICOMMENT_SYSTEM_PROMPT)]
                    ),
                    UserMessageSegment(content=[TextPart(text=prompt)]),
                ],
            )
        except asyncio.TimeoutError:
            self._reply(event, "[FAIL] 调用 LLM 超时")
            return
        except Exception:
            logger.error(f"[aicomment] 调用 LLM 失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 调用 LLM 失败")
            return

        text = str(getattr(resp, "completion_text", "") or "").strip()
        if not text:
            self._reply(event, "[FAIL] 模型返回空回复")
            return
        self._reply(event, text)

    @filter.command("show")
    @_quota_guard("show")
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
    @_quota_guard("fork")
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
    @_quota_guard("rewrite")
    async def cmd_rewrite(self, event: AstrMessageEvent) -> None:
        """改写最后一条用户消息、重发，或切换/查看重写版本"""
        if not self._permission_ok(event, "rewrite"):
            return

        action, arg1, arg2, err = _parse_rewrite_args(event.message_str)
        if err:
            logger.info(f"[rewrite] 解析失败: {err}")
            self._reply(event, f"[FAIL] {err}")
            return
        logger.info(f"[rewrite] action={action}, arg1={repr(arg1)}, arg2={repr(arg2)}")

        if action == "ai" and not self._bool("allow_modify_assistant", True):
            self._reply(event, "[FAIL] 修改 AI 记忆功能未开启")
            return

        if action == "get":
            await self._handle_rewrite_version(event, "get", 0)
            return
        if action in {"set", "view"}:
            await self._handle_rewrite_version(event, action, int(arg1))
            return
        if action == "user":
            if self._rewrite_mode() == "plugin":
                await self._plugin_user_rewrite(event, str(arg1 or ""), str(arg2 or ""))
            else:
                await self._prepare_user_rewrite(event, str(arg1 or ""), str(arg2 or ""))
            return
        if action == "resend":
            if self._rewrite_mode() == "plugin":
                await self._plugin_resend(event)
            else:
                await self._prepare_resend(event)
            return

        conv = await self._current_conversation(event)
        if conv is None:
            return
        history = self._history_list(conv.history)
        if not history:
            self._reply(event, "[FAIL] 对话历史格式异常")
            return
        await self._handle_ai_rewrite(
            event,
            history,
            str(arg1 or ""),
            str(arg2 or ""),
            event.unified_msg_origin,
            conv.cid,
        )

    @filter.command("rewrite_help")
    async def cmd_rewrite_help(self, event: AstrMessageEvent) -> None:
        """查看 /rewrite 用法"""
        self._reply(
            event,
            "【对话增强】改写帮助\n"
            "重发（重新生成上一条回复，不改提示词）：/rewrite\n"
            '修改自己最后一条消息并重新生成回复：/rewrite user|u "旧文本" "新文本"\n'
            '修改 AI 最后一条回复（只改记忆，不重新生成）：/rewrite ai|a "旧文本" "新文本"\n'
            "查看版本状态：/rewrite get（当前已重写 n 次，现在是第几版）\n"
            "切换版本：/rewrite set n（0 为原版）\n"
            "预览某版回复原文：/rewrite view n\n"
            f"当前生成链路：{self._rewrite_mode()}（可在插件配置 rewrite_mode 切换）\n"
            "支持子串替换，若重复多处会提示；文本含空格请用引号或括号包裹。\n"
            "引号可嵌套，例如 /rewrite u “他说“你好”然后走了” “新的句子”。\n"
            "支持的包裹符号：英文双引号、英文单引号、半角圆括号、中文引号、直角引号",
        )

    # ---------------------------------------------------------- 提示词插入/覆盖

    @filter.command("showprompt")
    @_quota_guard("prompt")
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
    @_quota_guard("prompt")
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
    @_quota_guard("prompt")
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
    @_quota_guard("prompt")
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

    # ---------------------------------------------------------- 自动会话标题

    @filter.command("retitle")
    @_quota_guard("title_retitle")
    async def cmd_retitle(self, event: AstrMessageEvent) -> None:
        """基于当前对话的全部历史重新生成标题"""
        if not self._title_enabled():
            self._reply(event, "自动标题功能未开启，请先在插件配置中打开 title_enabled。")
            return
        if not self._permission_ok(event, "title_retitle"):
            return

        conv = await self._current_conversation(event)
        if conv is None:
            return
        rounds = build_rounds(
            load_history(conv.history),
            strip_reminder=self._bool("strip_system_reminder", True),
        )
        if not rounds:
            self._reply(event, "当前对话还没有历史记录，无法生成标题。")
            return

        title = await self._generate_title(event, rounds)
        if not title:
            self._reply(event, "[FAIL] 生成标题失败，请稍后重试")
            return
        try:
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin, conv.cid, title=title
            )
        except Exception:
            logger.error(f"[title] 保存标题失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 标题已生成但保存失败")
            return
        self._reply(event, f"已生成标题：{title}")

    @filter.on_llm_response()
    async def auto_title(self, event: AstrMessageEvent, resp: Any) -> None:
        """第一轮对话结束后自动生成标题。

        选用 on_llm_response 而不是 after_message_sent：AstrBot 的洋葱式调度里，
        本轮历史要等响应阶段结束后才写库，发送后钩子读到的历史不含本轮。
        """
        if event.get_extra("_rewrite_launched"):
            return
        if not self._title_enabled():
            return
        if not self._permission_ok(event, "title_auto", notify=False):
            return

        conv = await self._silent_conversation(event)
        if conv is None:
            return
        if conv.title and conv.title != "新对话":
            return  # 已有标题，不覆盖

        # 此时本轮还没入库，历史为空才说明这是第一轮
        existing = build_rounds(
            load_history(conv.history),
            strip_reminder=self._bool("strip_system_reminder", True),
        )
        if existing:
            return

        reply_text = str(getattr(resp, "completion_text", "") or "").strip()
        if not event.message_str or not reply_text:
            return
        # 放到后台生成，避免额外的一次 LLM 请求拖慢本轮回复
        asyncio.create_task(
            self._auto_title_task(event, conv.cid, event.message_str, reply_text)
        )

    async def _auto_title_task(
        self, event: AstrMessageEvent, cid: str, user_text: str, reply_text: str
    ) -> None:
        """后台生成第一轮标题并写回对话。"""
        try:
            current = Round(
                number=1,
                start=0,
                end=0,
                user_texts=[user_text],
                assistant_texts=[reply_text],
            )
            title = await self._generate_title(event, [current])
            if not title:
                return
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin, cid, title=title
            )
            logger.info(f"[title] 已自动生成标题：{title}")
        except Exception:
            logger.error(f"[title] 保存标题失败: {traceback.format_exc()}")

    # -------------------------------------------------------------- 改写实现

    async def _prepare_user_rewrite(
        self, event: AstrMessageEvent, old: str, new: str
    ) -> None:
        """替换最后一条用户消息，并交给 AstrBot 正常链路重新生成回复。"""
        conv = await self._current_conversation(event)
        if conv is None:
            return
        history = self._history_list(conv.history)
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

        new_text = user_text.replace(old, new, 1)
        content = system_items + [{"type": "text", "text": new_text}]

        rec = await self._turn_record(
            event.unified_msg_origin, conv.cid, history, last_user_idx
        )
        rec.versions.append({"content": content, "assistant": ""})
        version = len(rec.versions)

        if not await self._launch_native_rewrite(
            event, conv, history, rec, new_text, version, content=content
        ):
            rec.versions.pop()
            self._reply(event, "[FAIL] 未能交给 AstrBot 生成回复，已保留原对话")

    async def _prepare_resend(self, event: AstrMessageEvent) -> None:
        """裸 /rewrite：让 AstrBot 重新生成当前版本的回复。"""
        conv = await self._current_conversation(event)
        if conv is None:
            return
        history = self._history_list(conv.history)
        last_user_idx, last_user = self._find_last_by_role(history, "user")
        if last_user_idx == -1:
            self._reply(event, "[FAIL] 没有找到可重发的用户消息")
            return

        user_text, _ = self._split_content(last_user.get("content"))
        rec = await self._turn_record(
            event.unified_msg_origin, conv.cid, history, last_user_idx
        )
        version = int(rec.current or 0)

        if not await self._launch_native_rewrite(
            event, conv, history, rec, user_text, version
        ):
            self._reply(event, "[FAIL] 未能交给 AstrBot 重新生成，已保留原对话")

    # ---- 插件自建流水线（rewrite_mode=plugin） ----
    # 不走 AstrBot 的 Agent 阶段，直接调用 llm_generate：不依赖官方调度条件，
    # 也不会被 CmdMask 的 call_llm 标记影响；代价是模型不能调用工具。

    async def _plugin_generate(
        self, event: AstrMessageEvent, conv: Any, history: List[dict]
    ) -> Optional[str]:
        """用插件流水线生成回复，失败返回 None。"""
        system_prompt = await self._load_system_prompt(
            event.unified_msg_origin, conv.cid
        )
        system_prompt = self._apply_prompt_custom(event, system_prompt)

        contexts: List[Any] = []
        if system_prompt:
            contexts.append(SystemMessageSegment(content=[TextPart(text=system_prompt)]))
        contexts.extend(self._history_to_message_segments(history))

        try:
            prov_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            resp = await self.context.llm_generate(
                chat_provider_id=prov_id, contexts=contexts
            )
        except asyncio.TimeoutError:
            logger.error("[rewrite] 插件流水线调用 LLM 超时")
            self._reply(event, "[FAIL] 调用 LLM 超时，历史未修改")
            return None
        except Exception:
            logger.error(f"[rewrite] 插件流水线调用 LLM 失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 调用 LLM 失败，历史未修改")
            return None

        text = str(getattr(resp, "completion_text", "") or "").strip()
        if not text:
            logger.error("[rewrite] 插件流水线拿到空回复，历史未修改")
            self._reply(event, "[FAIL] 模型返回空回复，历史未修改")
            return None
        return text

    async def _plugin_save_turn(
        self,
        event: AstrMessageEvent,
        conv: Any,
        history: List[dict],
        new_history: List[dict],
        rec: "RewriteRecord",
        version: int,
        assistant_text: str,
    ) -> None:
        """把重写结果写回对话，并记录到对应版本。"""
        # 生成期间可能有新消息入库，按原顺序追加在后面
        try:
            fresh = await self.context.conversation_manager.get_conversation(
                event.unified_msg_origin, conv.cid
            )
            if fresh and fresh.history:
                fresh_history = self._history_list(fresh.history)
                if len(fresh_history) > len(history):
                    new_history.extend(fresh_history[len(history) :])
                    logger.info(
                        f"[rewrite] 追加了 {len(fresh_history) - len(history)} 条并发消息"
                    )
        except Exception:
            logger.error(f"[rewrite] 并发检查失败: {traceback.format_exc()}")

        try:
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin, conv.cid, history=new_history
            )
        except Exception:
            logger.error(f"[rewrite] 保存重写结果失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 重写完成但保存失败")
            return

        async with self._rewrite_lock:
            if version == 0:
                rec.original_assistant = assistant_text
                rec.current = 0
            else:
                index = version - 1
                if 0 <= index < len(rec.versions):
                    rec.versions[index]["assistant"] = assistant_text
                    rec.current = version
            rec.touch()

        # 阻止 AstrBot 再走一遍默认 LLM 链路（否则会重复生成一条回复）
        try:
            event.should_call_llm(True)
        except Exception:
            event.call_llm = True
        self._reply(event, assistant_text)

    async def _plugin_user_rewrite(
        self, event: AstrMessageEvent, old: str, new: str
    ) -> None:
        """插件流水线：替换最后一条用户消息并重新生成回复。"""
        conv = await self._current_conversation(event)
        if conv is None:
            return
        history = self._history_list(conv.history)
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

        new_text = user_text.replace(old, new, 1)
        content = system_items + [{"type": "text", "text": new_text}]
        contexts = history[:last_user_idx] + [{"role": "user", "content": content}]

        rec = await self._turn_record(
            event.unified_msg_origin, conv.cid, history, last_user_idx
        )
        rec.versions.append({"content": content, "assistant": ""})
        version = len(rec.versions)

        assistant_text = await self._plugin_generate(event, conv, contexts)
        if assistant_text is None:
            rec.versions.pop()
            return

        new_history = contexts + [
            {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]}
        ]
        await self._plugin_save_turn(
            event, conv, history, new_history, rec, version, assistant_text
        )

    async def _plugin_resend(self, event: AstrMessageEvent) -> None:
        """插件流水线：重发当前版本，重新生成上一条回复。"""
        conv = await self._current_conversation(event)
        if conv is None:
            return
        history = self._history_list(conv.history)
        last_user_idx, _ = self._find_last_by_role(history, "user")
        if last_user_idx == -1:
            self._reply(event, "[FAIL] 没有找到可重发的用户消息")
            return

        contexts = history[: last_user_idx + 1]
        rec = await self._turn_record(
            event.unified_msg_origin, conv.cid, history, last_user_idx
        )
        version = int(rec.current or 0)

        assistant_text = await self._plugin_generate(event, conv, contexts)
        if assistant_text is None:
            return

        new_history = contexts + [
            {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]}
        ]
        await self._plugin_save_turn(
            event, conv, history, new_history, rec, version, assistant_text
        )

    async def _launch_native_rewrite(
        self,
        event: AstrMessageEvent,
        conv: Any,
        history: List[dict],
        rec: "RewriteRecord",
        user_text: str,
        version: int,
        content: Any = None,
    ) -> bool:
        """截断到目标轮次之前，改掉事件文本，让 AstrBot 官方链路生成回复。"""
        # AstrBot 只在「未发送过消息 + 已唤醒 + call_llm=False」时才进入 Agent 阶段。
        # CmdMask 这类改写指令的插件会调用 should_call_llm(True) 表示“已处理”，
        # 必须在截断历史之前复位，否则历史被截断却不会生成回复（丢对话问题）。
        try:
            event.should_call_llm(False)
        except Exception:  # 兼容旧事件对象
            event.call_llm = False
        if not getattr(event, "is_at_or_wake_command", False):
            event.is_at_or_wake_command = True
        if getattr(event, "_has_send_oper", False):
            logger.warning("[rewrite] 本事件已有发送操作，AstrBot 不会再进入 Agent 阶段")
            return False

        base_len = int(rec.base_len or 0)
        base = history[:base_len]
        try:
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin, conv.cid, history=base
            )
        except Exception:
            logger.error(f"[rewrite] 截断历史失败: {traceback.format_exc()}")
            return False

        self._pending_rewrite[event.unified_msg_origin] = {
            "cid": conv.cid,
            "user": user_text,
            "version": version,
            "content": content,
            "base_len": base_len,
            "event": event,
        }

        event.message_str = user_text
        try:
            event.set_extra("_rewrite_launched", True)  # 让自动标题跳过本事件
        except Exception:
            pass
        try:
            if getattr(event, "message_obj", None) is not None:
                event.message_obj.message_str = user_text
        except Exception:  # 部分平台的事件对象不允许写入
            pass
        event.clear_result()
        logger.info(
            f"[rewrite] 已截断到 {base_len} 条，交给 AstrBot 链路生成第 {version} 版"
            f"（call_llm={getattr(event, 'call_llm', None)}, "
            f"at_or_wake={getattr(event, 'is_at_or_wake_command', None)}）"
        )
        return True

    def _drop_pending(self, umo: str) -> None:
        """清掉挂起的重写任务。"""
        self._pending_rewrite.pop(umo, None)

    async def _turn_record(
        self, umo: str, cid: str, history: List[dict], last_user_idx: int
    ) -> "RewriteRecord":
        """取当前轮的版本记录；换了轮次就重新开始记录。"""
        async with self._rewrite_lock:
            cid_map = self._rewrite_store.setdefault(umo, {})
            rec = cid_map.get(cid)
            if rec is None or int(rec.base_len) != last_user_idx:
                last_user = history[last_user_idx]
                assistant_text = ""
                for msg in history[last_user_idx + 1 :]:
                    if isinstance(msg, dict) and msg.get("role") == "assistant":
                        text, _ = self._split_content(msg.get("content"))
                        if text:
                            assistant_text = text
                rec = RewriteRecord(
                    cid=cid,
                    base_len=last_user_idx,
                    original_content=last_user.get("content"),
                    original_assistant=assistant_text,
                )
                cid_map[cid] = rec
            else:
                rec.touch()
            return rec

    async def _handle_rewrite_version(
        self, event: AstrMessageEvent, action: str, number: int
    ) -> None:
        """处理 /rewrite get|set|view。"""
        conv = await self._current_conversation(event)
        if conv is None:
            return
        history = self._history_list(conv.history)
        last_user_idx, _ = self._find_last_by_role(history, "user")
        async with self._rewrite_lock:
            rec = (self._rewrite_store.get(event.unified_msg_origin) or {}).get(conv.cid)
            if rec is not None and int(rec.base_len) != last_user_idx:
                rec = None  # 记录属于更早的轮次
            if rec is not None:
                rec.touch()
                versions = list(rec.versions)
                total = rec.round_count
                current = int(rec.current or 0)
                original = {
                    "content": rec.original_content,
                    "assistant": rec.original_assistant,
                }
                base_len = int(rec.base_len)

        if rec is None:
            if action == "get":
                self._reply(event, "[rewrite] 当前已重写 0 次，现在是原版")
            else:
                self._reply(event, "[rewrite] 还没有重写记录，当前是原版")
            return

        if action == "get":
            where = "原版" if current == 0 else f"第 {current} 版"
            self._reply(event, f"[rewrite] 当前已重写 {total} 次，现在是{where}")
            return

        if number < 0 or number > total:
            self._reply(event, f"[rewrite] 版本号超出范围，可用 0 ~ {total}（0 为原版）")
            return

        entry = original if number == 0 else versions[number - 1]

        if action == "view":
            text = str(entry.get("assistant") or "")
            if not text:
                self._reply(event, "[rewrite] 该版本还没有生成回复")
                return
            self._reply(event, text)
            return

        restored = history[:base_len]
        restored.append({"role": "user", "content": entry.get("content")})
        if entry.get("assistant"):
            restored.append(
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": entry["assistant"]}],
                }
            )
        try:
            await self.context.conversation_manager.update_conversation(
                event.unified_msg_origin, conv.cid, history=restored
            )
        except Exception:
            logger.error(f"[rewrite] 切换版本失败: {traceback.format_exc()}")
            self._reply(event, "[FAIL] 切换版本失败")
            return

        async with self._rewrite_lock:
            rec.current = number
            rec.touch()
        self._drop_pending(event.unified_msg_origin)
        self._reply(event, "已恢复原版" if number == 0 else f"已切换到第 {number} 版")

    @filter.on_llm_response()
    async def capture_rewrite_reply(
        self, event: AstrMessageEvent, resp: Any
    ) -> None:
        """重写请求的回复生成完成后，把回复正文记进对应版本。

        AstrBot 在响应阶段结束后才把本轮历史写库，所以这里直接从 LLM 响应
        取回复原文，不依赖 conv.history 是否已经更新。
        """
        umo = event.unified_msg_origin
        pending = self._pending_rewrite.get(umo)
        if not pending:
            return
        # 只认发起重写的那个事件，避免同会话的其他消息抢占回填
        if pending.get("event") is not event:
            return

        text = str(getattr(resp, "completion_text", "") or "").strip()
        self._drop_pending(umo)
        if not text:
            logger.warning("[rewrite] 本次重写没有拿到回复文本")
            return

        version = int(pending.get("version") or 0)
        async with self._rewrite_lock:
            rec = (self._rewrite_store.get(umo) or {}).get(
                str(pending.get("cid") or "")
            )
            if rec is None:
                return
            if version == 0:
                rec.original_assistant = text
                rec.current = 0
            else:
                index = version - 1
                if not 0 <= index < len(rec.versions):
                    return
                rec.versions[index]["assistant"] = text
                rec.current = version
            rec.touch()
        logger.info(f"[rewrite] 已记录第 {version} 版回复（{len(text)} 字）")

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
        # 失败、用法提示、功能未开启都不消耗配额，交给 _quota_guard 判断
        if (
            text.startswith("[FAIL]")
            or text.startswith("用法：")
            or "未开启" in text
        ):
            try:
                event.set_extra(QUOTA_FAILED_FLAG, True)
            except Exception:
                pass
        event.set_result(MessageEventResult().message(text).use_t2i(False))

    # ---- 指令配额（懒加载 + UNIX 时间戳） ----

    def _quota_path(self) -> Path:
        """配额数据文件：data/plugin_data/<插件名>/quotas.json。"""
        return StarTools.get_data_dir(PLUGIN_NAME) / "quotas.json"

    def _load_quotas(self) -> dict:
        """懒加载：首次访问时才读磁盘。"""
        if self._quotas is None:
            self._quotas = self._read_quotas()
        return self._quotas

    def _read_quotas(self) -> dict:
        try:
            with self._quota_path().open(encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except Exception:
            logger.error(f"[quota] 读取配额失败: {traceback.format_exc()}")
            return {}
        if not isinstance(data, dict):
            logger.warning("[quota] quotas.json 不是对象，已忽略")
            return {}
        store: dict[str, list[float]] = {}
        for key, stamps in data.items():
            if not isinstance(stamps, list):
                continue
            store[str(key)] = [
                float(ts) for ts in stamps if isinstance(ts, (int, float))
            ]
        return store

    def _save_quotas(self) -> None:
        try:
            path = self._quota_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._quotas or {}, fh, ensure_ascii=False, indent=2)
            tmp.replace(path)
        except Exception:
            logger.error(f"[quota] 保存配额失败: {traceback.format_exc()}")

    @staticmethod
    def _quota_scope(event: AstrMessageEvent) -> str:
        """配额作用域：群聊按群号，私聊按用户 ID。"""
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = ""
        if group_id:
            return f"group:{group_id}"
        try:
            sender_id = event.get_sender_id()
        except Exception:
            sender_id = ""
        if sender_id:
            return f"user:{sender_id}"
        return f"user:{event.unified_msg_origin}"

    def _quota_settings(self, command: str) -> Optional[Tuple[int, int]]:
        """取配额配置；未开启返回 None。"""
        conf = self._permission_config(command)
        if not self._as_bool(conf.get("rate_enabled"), False):
            return None
        try:
            minutes = int(conf.get("rate_limit_minutes") or QUOTA_DEFAULT_MINUTES)
        except (TypeError, ValueError):
            minutes = QUOTA_DEFAULT_MINUTES
        try:
            count = int(conf.get("rate_limit_count") or QUOTA_DEFAULT_COUNT)
        except (TypeError, ValueError):
            count = QUOTA_DEFAULT_COUNT
        return max(1, minutes), max(1, count)

    async def _quota_check(self, event: AstrMessageEvent, command: str) -> bool:
        """检查窗口内次数是否超限；只读，不写时间戳。"""
        settings = self._quota_settings(command)
        if settings is None:
            return True
        minutes, count = settings
        window = minutes * 60
        now = time.time()
        key = f"{self._quota_scope(event)}|{command}"

        async with self._quota_lock:
            stamps = [ts for ts in self._load_quotas().get(key, []) if now - ts < window]
            if len(stamps) < count:
                return True
            wait = int(window - (now - min(stamps))) + 1

        logger.info(f"[quota] {command} 超限：{key} 已用 {len(stamps)}/{count} 次")
        self._reply(
            event,
            f"[FAIL] /{command} 使用过于频繁，请 {wait} 秒后再试"
            f"（每 {minutes} 分钟最多 {count} 次）",
        )
        return False

    async def _quota_commit(self, event: AstrMessageEvent, command: str) -> None:
        """指令成功后记录时间戳并落盘。"""
        settings = self._quota_settings(command)
        if settings is None:
            return
        minutes, _ = settings
        window = minutes * 60
        now = time.time()
        key = f"{self._quota_scope(event)}|{command}"

        async with self._quota_lock:
            store = self._load_quotas()
            stamps = [ts for ts in store.get(key, []) if now - ts < window]
            stamps.append(now)
            store[key] = stamps
            self._save_quotas()
        logger.info(f"[quota] {command} 已记录：{key} 共 {len(stamps)} 次")

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

    # ---- 重写版本暂存 ----
    # 版本记录只放在 self._rewrite_store（内存字典），随插件生命周期，
    # 重载/停用即清空；清理任务与「遗忘」插件一致。

    async def terminate(self) -> None:
        """插件卸载/停用时取消后台任务并清空暂存。"""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        async with self._rewrite_lock:
            self._rewrite_store.clear()
        self._pending_rewrite.clear()

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

    async def _silent_conversation(self, event: AstrMessageEvent) -> Any:
        """取当前对话但不回复任何提示，供后台钩子使用。"""
        try:
            conv_mgr = self.context.conversation_manager
            cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
            if not cid:
                return None
            return await conv_mgr.get_conversation(event.unified_msg_origin, cid)
        except Exception:
            logger.error(f"[title] 获取对话失败: {traceback.format_exc()}")
            return None

    async def _generate_title(self, event: AstrMessageEvent, rounds: list[Round]) -> str:
        """调用 LLM 生成标题，失败返回空字符串。"""
        max_chars = max(1, self._int("title_max_chars", 20))
        lines: list[str] = []
        for round_ in rounds[-10:]:
            if round_.user_text:
                lines.append(f"用户：{inline(round_.user_text)[:300]}")
            if round_.assistant_text:
                lines.append(f"AI：{inline(round_.assistant_text)[:300]}")
        if not lines:
            return ""

        prompt = (
            f"请为下面的对话起一个不超过 {max_chars} 个字的标题：\n\n"
            + "\n".join(lines)
        )
        try:
            prov_id = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            resp = await self.context.llm_generate(
                chat_provider_id=prov_id,
                contexts=[
                    SystemMessageSegment(content=[TextPart(text=TITLE_SYSTEM_PROMPT)]),
                    UserMessageSegment(content=[TextPart(text=prompt)]),
                ],
            )
        except Exception:
            logger.error(f"[title] 生成标题失败: {traceback.format_exc()}")
            return ""
        return _clean_title(getattr(resp, "completion_text", "") or "", max_chars)

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

    # ---- 权限控制 ----

    def _permission_config(self, command: str) -> dict:
        """取某个指令的权限配置，格式异常时按空配置处理。"""
        raw = self.config.get(f"{command}_permission", {}) if self.config else {}
        return raw if isinstance(raw, dict) else {}

    def _prompt_feature_enabled(self) -> bool:
        """提示词功能总开关，默认关闭。"""
        return self._as_bool(self._permission_config("prompt").get("enabled"), False)

    def _title_enabled(self) -> bool:
        """自动标题功能总开关，默认关闭。"""
        return self._as_bool(self._raw("title_enabled", False), False)

    def _prompt_ready(self, event: AstrMessageEvent) -> bool:
        """提示词指令的前置检查：功能开关 + 群聊/名单权限。"""
        if not self._prompt_feature_enabled():
            self._reply(
                event,
                "提示词功能未开启，请先在插件配置中打开 prompt_permission 的 enabled。",
            )
            return False
        return self._permission_ok(event, "prompt")

    async def _load_system_prompt(self, umo: str, cid: str) -> str:
        """加载当前对话使用的人格 system_prompt（取不到返回空字符串）。"""
        try:
            conv = await self.context.conversation_manager.get_conversation(umo, cid)
            persona_id = conv.persona_id if conv else None
            if persona_id:
                persona = await self.context.persona_manager.get_persona(persona_id)
                if persona:
                    return persona.system_prompt or ""
                logger.warning(f"[rewrite] 人格未找到: {persona_id}")
            else:
                default_persona = await self.context.persona_manager.get_default_persona_v3(
                    umo
                )
                if default_persona:
                    return default_persona.get("prompt") or ""
        except Exception:
            logger.error(f"[rewrite] 加载人格失败: {traceback.format_exc()}")
        return ""

    @staticmethod
    def _history_to_message_segments(history: List[dict]) -> List[Any]:
        """把历史消息转换成 llm_generate 需要的消息段列表。"""
        segments: List[Any] = []
        for msg in history:
            content = msg.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text_parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
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
        # rewrite / prompt / 标题相关默认不允许群聊，其余指令默认允许
        default_group = command not in {"rewrite", "prompt", "title_auto", "title_retitle"}
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
                elif command == "title_auto":
                    self._reply(event, "[FAIL] 没有使用自动标题功能的权限")
                elif command == "title_retitle":
                    self._reply(event, "[FAIL] 没有使用 /retitle 的权限")
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

    def _rewrite_mode(self) -> str:
        """重写走哪条链路：astrbot（默认，可调用工具）或 plugin（插件自建流水线）。"""
        mode = str(self._raw("rewrite_mode", "astrbot") or "astrbot").strip().lower()
        return "plugin" if mode == "plugin" else "astrbot"

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
