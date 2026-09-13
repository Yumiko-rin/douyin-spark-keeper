"""会话行解析：从原始行文本中提取好友昵称与火花天数。

纯函数，不依赖浏览器，全部有单元测试。判定策略：
宁可漏（不给无关会话发消息），不可错发。
"""
from __future__ import annotations

import re

from douyin.models import Friend

# 「火花392天」「火花 392 天」
SPARK_DAYS_RE = re.compile(r"火花\s*(\d{1,4})\s*天?")
# 「392天」
DAY_RE = re.compile(r"(\d{1,4})\s*天")
# 裸数字行（Docker 极简页面形态下火花天数独立成行）
BARE_NUM_RE = re.compile(r"^\d{1,4}$")

# 这些开头的行不是昵称
_NON_NAME_PREFIX = ("分享", "在线", "离线", "系统", "官方")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}")
_BRACKET_RE = re.compile(r"^[\[［(（].+[\]］)）]$")


def extract_days(text: str) -> int | None:
    m = SPARK_DAYS_RE.search(text)
    if m:
        return int(m.group(1))
    m = DAY_RE.search(text)
    if m and "火花" in text:
        return int(m.group(1))
    # 「🔥365」「🔥 365」形态：行内有火花图标且带数字
    if ("火花" in text or "🔥" in text):
        m = re.search(r"(\d{1,4})", text)
        if m:
            return int(m.group(1))
    return None


def extract_name(aria: str, lines: list[str]) -> str | None:
    """昵称提取：优先 aria-label 的「昵称，火花N天」形态，再退化到文本行。"""
    if aria:
        # 「侯，火花392天」/「侯 火花392天」
        m = re.match(r"(.+?)[,，\s]+火花", aria)
        if m:
            return m.group(1).strip()
        # aria 里只有昵称
        if _plausible_name(aria):
            return aria.strip()
    for line in lines:
        name = _plausible_name(line)
        if name:
            return name
    return None


def _plausible_name(text: str) -> str | None:
    t = (text or "").strip()
    if not t or len(t) > 30:
        return None
    if t.isdigit() or BARE_NUM_RE.match(t):
        return None
    if _TIME_RE.match(t) or _BRACKET_RE.match(t):
        return None
    if t.startswith(_NON_NAME_PREFIX):
        return None
    if "火花" in t or "天" in t:
        # 「xxx，火花392天」形态也能提取
        m = re.match(r"(.+?)[,，\s]+火花", t)
        if m:
            return m.group(1).strip()
        return None
    return t


def parse_row(row: dict) -> Friend | None:
    """把 JS 采集的行 {text, aria, title} 解析成 Friend。

    有可提取的昵称就收录（火花天数缺失记 0）——会话列表本身就是真实会话，
    是否发送由用户勾选决定；这里只负责「宁全勿漏」地列出并尽力识别火花天数。
    """
    text = (row.get("text") or "").strip()
    aria = (row.get("aria") or "").strip()
    title = (row.get("title") or "").strip()
    if not any((text, aria, title)):
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # 行首是系统/官方/分享等前缀 → 整行是系统通知/公告，不是好友会话
    if lines and lines[0].startswith(_NON_NAME_PREFIX):
        return None
    candidates = [aria, title] + lines

    days: int | None = None
    for c in candidates:
        days = extract_days(c)
        if days is not None:
            break
    # 行内没有明确「火花N天」，但行文本带火花/🔥 图标且有裸数字 → 视为天数
    if days is None:
        has_spark_mark = any(("火花" in c or "🔥" in c) for c in candidates)
        if has_spark_mark:
            for line in lines:
                if BARE_NUM_RE.match(line):
                    days = int(line)
                    break

    name = extract_name(aria or title, lines)
    if not name:
        return None
    return Friend(name=name, streak_days=days or 0, raw=text or aria)
