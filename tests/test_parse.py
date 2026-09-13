"""会话行解析测试：宁漏勿错的判定策略。"""
from __future__ import annotations

from douyin.parse import extract_days, extract_name, parse_row


def test_extract_days_explicit():
    assert extract_days("侯，火花392天") == 392
    assert extract_days("火花 3 天") == 3
    assert extract_days("捏麻麻滴 🔥3") == 3


def test_extract_days_ignores_message_dates():
    """自己发的定时消息预览「续火花 2026-09-13」不能被当成火花天数。"""
    assert extract_days("签到~ 续火花 2026-09-13") is None
    assert extract_days("{friend}，火花别灭呀 🔥") is None
    assert extract_days("续火花 2026/09/13") is None


def test_extract_days_requires_spark_keyword():
    """没有「火花」语境的「30天」不能当火花天数（可能是群公告等）。"""
    assert extract_days("已聊30天") is None


def test_parse_row_aria_form():
    f = parse_row({"text": "侯\n火花392天", "aria": "侯，火花392天", "title": ""})
    assert f is not None
    assert f.name == "侯"
    assert f.streak_days == 392


def test_parse_row_with_time_and_name():
    f = parse_row({"text": "小明\n昨天 21:03\n🔥365", "aria": "", "title": ""})
    assert f is not None and f.name == "小明" and f.streak_days == 365


def test_parse_row_bare_number_with_spark_mark():
    """Docker 极简页面：火花天数独立成行。"""
    f = parse_row({"text": "小红\n🔥\n42", "aria": "", "title": ""})
    assert f is not None and f.name == "小红" and f.streak_days == 42


def test_parse_row_rejects_noise():
    assert parse_row({"text": "系统通知\n点此查看", "aria": "", "title": ""}) is None
    assert parse_row({"text": "", "aria": "", "title": ""}) is None
    assert parse_row({"text": "21:03", "aria": "", "title": ""}) is None
    assert parse_row({"text": "分享给好友", "aria": "", "title": ""}) is None


def test_parse_row_without_spark_still_listed():
    """无火花信号的普通会话也收录（天数记 0），是否发送由用户勾选决定。"""
    f = parse_row({"text": "路人甲\n你好呀", "aria": "", "title": ""})
    assert f is not None
    assert f.name == "路人甲"
    assert f.streak_days == 0


def test_extract_name_from_aria_only():
    assert extract_name("老王，火花12天", []) == "老王"


def test_name_length_guard():
    assert parse_row({"text": "x" * 40 + "\n火花3天", "aria": "", "title": ""}) is None \
        or parse_row({"text": "x" * 40 + "\n火花3天", "aria": "", "title": ""}).name != "x" * 40
