"""调度时间计算测试：准时性的根基。"""
from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from spark import plan

TZ = ZoneInfo("Asia/Shanghai")


def dt(h, m, s=0, day=13, month=9, year=2026):
    return datetime(year, month, day, h, m, s, tzinfo=TZ)


def test_parse_hhmm():
    assert plan.parse_hhmm("21:00") == (21, 0)
    assert plan.parse_hhmm("0:05") == (0, 5)
    for bad in ("21", "25:00", "12:60", "aa:bb", "21:00:00"):
        try:
            plan.parse_hhmm(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应该不合法")


def test_today_at():
    assert plan.today_at("21:00", dt(10, 30)) == dt(21, 0)


def test_next_send_before_target():
    now = dt(10, 30)
    nxt = plan.next_send_time(now, "21:00", 0, random.Random(1))
    assert nxt == dt(21, 0)


def test_next_send_after_deadline_rolls_to_tomorrow():
    now = dt(23, 58)
    nxt = plan.next_send_time(now, "21:00", 0, random.Random(1))
    assert nxt.day == 14 and nxt.hour == 21


def test_next_send_within_grace_stays_today():
    """发送时刻已过 5 分钟，但在宽限期内：仍返回今天该时刻（供补偿逻辑）。"""
    now = dt(21, 5)
    nxt = plan.next_send_time(now, "21:00", 0, random.Random(1), grace_minutes=10)
    assert nxt.day == 13


def test_jitter_deterministic_by_seed():
    """同一种子抖动结果一致（调度重算不漂移）。"""
    now = dt(10, 0)
    r1 = plan.next_send_time(now, "21:00", 30, random.Random("a|2026-09-13"))
    r2 = plan.next_send_time(now, "21:00", 30, random.Random("a|2026-09-13"))
    assert r1 == r2
    assert abs((r1 - dt(21, 0)).total_seconds()) <= 30 * 60


def test_jitter_past_target_pushes_to_tomorrow():
    """负抖动落在过去时必须顺延，不能死等一个过去的时刻。"""
    now = dt(20, 45)  # 若抖到 20:30 之前，今天已过
    nxt = plan.next_send_time(now, "21:00", 60, random.Random("x"))
    assert nxt > now


def test_should_catch_up():
    assert plan.should_catch_up(dt(21, 30), "21:00", "23:55") is True
    assert plan.should_catch_up(dt(20, 30), "21:00", "23:55") is False
    assert plan.should_catch_up(dt(23, 56), "21:00", "23:55") is False  # 已过截止


def test_window_send_time_inside_and_deterministic():
    rng = random.Random("a|2026-09-13")
    t1 = plan.window_send_time(dt(10, 0), ["20:00", "23:00"], rng)
    t2 = plan.window_send_time(dt(10, 0), ["20:00", "23:00"], random.Random("a|2026-09-13"))
    assert t1 == t2  # 同种子结果确定，调度重算不漂移
    assert dt(20, 0) <= t1 <= dt(23, 0)


def test_window_send_time_rejects_bad_window():
    for bad in (["23:00", "20:00"], ["20:00"], []):
        try:
            plan.window_send_time(dt(10, 0), bad, random.Random(1))
        except ValueError:
            continue
        raise AssertionError(f"{bad} 应该不合法")


def test_is_today_missed():
    assert plan.is_today_missed(dt(23, 56), "21:00", "23:55") is True
    assert plan.is_today_missed(dt(12, 0), "21:00", "23:55") is False


def test_format_ctx_time():
    ctx = plan.format_ctx_time(dt(9, 8, day=13, month=9, year=2026))
    assert ctx["date"] == "2026-09-13"
    assert ctx["time"] == "09:08"
    assert ctx["weekday"] == "周日"  # 2026-09-13 是周日
