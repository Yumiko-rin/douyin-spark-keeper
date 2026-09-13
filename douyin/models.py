"""抖音领域模型。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Friend:
    """会话列表中的一名好友。"""

    name: str
    streak_days: int = 0
    raw: str = ""          # 原始行文本（调试用）
    selected: bool = False

    def to_row(self) -> dict:
        return {
            "name": self.name,
            "streak_days": self.streak_days,
            "raw": self.raw,
            "selected": int(self.selected),
        }


class NotLoggedIn(Exception):
    """登录态失效，需要重新扫码。"""


class SendVerifyError(Exception):
    """消息已键入但未能确认送达（DOM 与网络层均无证据）。"""


@dataclass
class SendOutcome:
    friend: str
    ok: bool
    detail: str = ""
    latency_ms: int = 0
    message: str = ""
    verified_by: str = ""  # dom / network / dry
