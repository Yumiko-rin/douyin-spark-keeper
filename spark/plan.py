"""纯函数式调度时间计算，便于单元测试。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def parse_hhmm(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时刻需为 HH:MM：{value!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"时刻超出范围：{value!r}")
    return h, m


def today_at(hhmm: str, now: datetime) -> datetime:
    h, m = parse_hhmm(hhmm)
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def with_jitter(dt: datetime, jitter_minutes: int, rng: random.Random) -> datetime:
    if jitter_minutes <= 0:
        return dt
    return dt + timedelta(minutes=rng.randint(-jitter_minutes, jitter_minutes))


def next_send_time(
    now: datetime,
    send_time: str,
    jitter_minutes: int,
    rng: random.Random,
    grace_minutes: int = 0,
) -> datetime:
    """下一次发送时刻（已含抖动）。

    - 未到时刻：今天；
    - 已过但在宽限窗口内（grace_minutes）：仍返回今天该时刻，供补偿逻辑；
    - 已过且超出宽限：顺延到明天，绝不返回过去的时刻。
    """
    target = with_jitter(today_at(send_time, now), jitter_minutes, rng)
    grace_end = target + timedelta(minutes=max(0, grace_minutes))
    if now > grace_end:
        target = with_jitter(
            today_at(send_time, now + timedelta(days=1)), jitter_minutes, rng
        )
    return target


def is_today_missed(now: datetime, send_time: str, deadline_time: str) -> bool:
    """今天是否已错过保底截止（用于启动补偿与漏发判断）。"""
    return now > today_at(deadline_time, now)


def should_catch_up(now: datetime, send_time: str, deadline_time: str, grace_minutes: int = 10) -> bool:
    """启动补偿：已过发送时刻（含小宽限）但还没到保底截止，值得立即补发。"""
    send_dt = today_at(send_time, now) + timedelta(minutes=grace_minutes)
    deadline_dt = today_at(deadline_time, now)
    return send_dt < now <= deadline_dt


def window_send_time(now: datetime, window: list[str], rng: random.Random) -> datetime:
    """发送窗口模式：在 [start, end] 内按 rng 取一个确定性的随机时刻。"""
    if len(window) != 2:
        raise ValueError("窗口需为 [start, end]")
    start, end = today_at(window[0], now), today_at(window[1], now)
    if end <= start:
        raise ValueError("窗口起点需早于终点")
    span = int((end - start).total_seconds())
    return start + timedelta(seconds=rng.randint(0, span))


def format_ctx_time(now: datetime) -> dict[str, str]:
    """模板变量：{date} {time} {weekday} {datetime}"""
    return {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": WEEKDAY_CN[now.weekday()],
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
    }
