"""会话历史的轮次解析与文本清洗。

AstrBot 的会话历史是 ``[{"role": ..., "content": [...]}, ...]`` 形式的数组。
这里把它按"轮"切分：一条 user 消息开始一轮，紧跟其后的所有 assistant 消息
（以及它们之间的 tool 记录）都属于这一轮。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

#: 历史里附带的内部标记，展示时应去掉。
_MSG_ID_RE = re.compile(r"\[MSG_ID:\s*[^\]]*\]")
_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)


@dataclass
class Round:
    """一轮对话。"""

    number: int
    """从 1 开始的轮次编号（按时间线从早到晚）。"""

    start: int
    """该轮在原始历史数组中的起始下标。"""

    end: int
    """该轮在原始历史数组中的结束下标（含）。"""

    user_texts: list[str] = field(default_factory=list)
    assistant_texts: list[str] = field(default_factory=list)

    @property
    def user_text(self) -> str:
        return "\n".join(self.user_texts)

    @property
    def assistant_text(self) -> str:
        return "\n".join(self.assistant_texts)


def load_history(raw: str | None) -> list[dict]:
    """把会话历史字符串解析成消息列表，解析失败返回空列表。"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def extract_texts(content: object, *, strip_reminder: bool = True) -> list[str]:
    """取出消息内容里的纯文本片段，跳过图片等非文本部分。"""
    if isinstance(content, str):
        pieces = [content]
    elif isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
            elif isinstance(part, str):
                pieces.append(part)
    else:
        return []

    cleaned = [clean_text(piece, strip_reminder=strip_reminder) for piece in pieces]
    return [piece for piece in cleaned if piece.strip()]


def clean_text(text: str, *, strip_reminder: bool = True) -> str:
    """去掉历史中的内部标记，保留正文。"""
    text = _MSG_ID_RE.sub("", text)
    if strip_reminder:
        text = _REMINDER_RE.sub("", text)
    return text.strip()


def build_rounds(
    history: list[dict],
    *,
    strip_reminder: bool = True,
) -> list[Round]:
    """按时间线把历史切分成轮次。

    - 一条 ``user`` 记录开始新一轮；
    - 后续的 ``assistant`` 记录并入当前轮（可能有多条）；
    - ``tool`` 记录不计入展示文本，但落在轮次区间内，便于原样复制。
    """
    if not history:
        return []

    starts: list[int] = []
    for index, record in enumerate(history):
        if record.get("role") == "user":
            starts.append(index)
    if not starts or starts[0] != 0:
        starts.insert(0, 0)  # 历史不是以 user 开头时，把第一条也算作一轮

    rounds: list[Round] = []
    for order, start in enumerate(starts):
        end = starts[order + 1] - 1 if order + 1 < len(starts) else len(history) - 1
        rounds.append(Round(number=order + 1, start=start, end=end))

    for round_ in rounds:
        for record in history[round_.start : round_.end + 1]:
            role = record.get("role")
            texts = extract_texts(record.get("content"), strip_reminder=strip_reminder)
            if not texts:
                continue
            if role == "user":
                round_.user_texts.extend(texts)
            elif role == "assistant":
                round_.assistant_texts.extend(texts)

    return rounds


def truncate(text: str, limit: int) -> str:
    """按字数截断，超出部分用省略号表示。"""
    if limit > 0 and len(text) > limit:
        return text[:limit] + "..."
    return text


def inline(text: str) -> str:
    """把换行和连续空白压成单个空格，便于在一条消息里紧凑展示。"""
    return re.sub(r"\s+", " ", text).strip()


def display_text(text: str, limit: int) -> str:
    """压缩为单行后截断，用于 /grep 的紧凑输出。"""
    return truncate(inline(text), limit)


def round_matches(round_: Round, subject: str, keyword: str) -> bool:
    """判断该轮在指定主体上是否命中关键词（大小写不敏感）。

    匹配前会把换行压成空格，这样带空格的检索词可以跨过原文的换行。
    """
    needle = inline(keyword).lower()
    if not needle:
        return False
    texts = round_.user_texts if subject == "user" else round_.assistant_texts
    return any(needle in inline(text).lower() for text in texts)
