"""文案模板：随机挑选 + 变量渲染，发送前可校验合法性。

支持变量：{friend} 好友名、{date} 2026-09-13、{time} 21:00、
{weekday} 周六、{datetime} 完整时间、{streak} 当前火花天数、
{hitokoto} 一言（发送时联网获取）、{festival} 当日节日祝福（配置 festivals）。
"""
from __future__ import annotations

import random
import string
from datetime import datetime

from spark.plan import format_ctx_time

ALLOWED_VARS = {"friend", "date", "time", "weekday", "datetime", "streak",
                "hitokoto", "festival"}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"  # 未知占位符原样保留，不炸


def render(template: str, ctx: dict) -> str:
    return template.format_map(_SafeDict(**ctx))


def validate_pool(pool: list[str]) -> list[str]:
    """返回问题列表（空列表 = 合法）。"""
    problems: list[str] = []
    for i, tpl in enumerate(pool):
        if not tpl or not tpl.strip():
            problems.append(f"第 {i + 1} 条为空")
            continue
        for var in _extract_vars(tpl):
            if var not in ALLOWED_VARS:
                problems.append(f"第 {i + 1} 条含未知变量 {{{var}}}，可用：{sorted(ALLOWED_VARS)}")
        if len(tpl) > 500:
            problems.append(f"第 {i + 1} 条超过 500 字")
    return problems


def _extract_vars(tpl: str) -> set[str]:
    out = set()
    parser = string.Formatter()
    for _, field_name, _, _ in parser.parse(tpl):
        if field_name:
            out.add(field_name.split(".")[0].split("[")[0])
    return out


def pick_template(pool: list[str], rng: random.Random, exclude: str | None = None) -> str:
    candidates = [t for t in pool if t and t.strip() and t != exclude] or \
                 [t for t in pool if t and t.strip()]
    return rng.choice(candidates)


def build_message(
    pool: list[str],
    friend_name: str,
    streak_days: int,
    now: datetime,
    rng: random.Random,
    extra_templates: list[str] | None = None,
    ctx_extra: dict[str, str] | None = None,
) -> tuple[str, str]:
    """返回 (最终消息, 使用的模板)。好友专属文案优先于公共池。

    ctx_extra：调度层在发送时注入的动态变量（hitokoto / festival 等）。
    """
    pool_all = list(extra_templates or []) or list(pool)
    template = pick_template(pool_all, rng)
    ctx = format_ctx_time(now)
    ctx["friend"] = friend_name
    ctx["streak"] = str(streak_days or 0)
    ctx.update(ctx_extra or {})
    return render(template, ctx), template
