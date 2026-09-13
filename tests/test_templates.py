"""文案模板测试。"""
from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from spark import templates

TZ = ZoneInfo("Asia/Shanghai")


def test_render_vars():
    now = datetime(2026, 9, 13, 21, 0, tzinfo=TZ)
    pool = ["{friend}，{weekday}愉快！{date} {time} 火花{streak}天"]
    msg, tpl = templates.build_message(pool, "小明", 392, now, random.Random(1))
    assert msg == "小明，周日愉快！2026-09-13 21:00 火花392天"
    assert tpl == pool[0]  # 返回的是所用模板原文，便于日志与查重


def test_unknown_var_kept_verbatim():
    out = templates.render("{friend} {unknown}", {"friend": "A"})
    assert out == "A {unknown}"


def test_validate_pool():
    assert templates.validate_pool(["{friend} 🔥"]) == []
    problems = templates.validate_pool(["", "{oops}", "x" * 501])
    assert len(problems) == 3


def test_pick_avoids_repeat_when_possible():
    pool = ["a", "b"]
    rng = random.Random(7)
    seen = {templates.pick_template(pool, rng) for _ in range(20)}
    assert seen == {"a", "b"}


def test_extra_templates_priority():
    now = datetime(2026, 9, 13, tzinfo=TZ)
    msg, _ = templates.build_message(["公共"], "A", 1, now, random.Random(1),
                                     extra_templates=["专属"])
    assert msg == "专属"


def test_ctx_extra_merged():
    """动态变量（一言/节日）通过 ctx_extra 注入。"""
    now = datetime(2026, 9, 13, tzinfo=TZ)
    msg, _ = templates.build_message(
        ["{festival} {hitokoto}"], "A", 1, now, random.Random(1),
        ctx_extra={"festival": "国庆快乐！", "hitokoto": "人间值得。"})
    assert msg == "国庆快乐！ 人间值得。"


def test_new_vars_allowed():
    assert templates.validate_pool(["{hitokoto}", "{festival} {streak}"]) == []
