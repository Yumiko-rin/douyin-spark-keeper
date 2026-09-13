"""DOM 层纯函数单元测试（无需浏览器）。"""
from __future__ import annotations

from douyin.dom import find_risk_keyword


def test_risk_keyword_detected():
    assert find_risk_keyword("系统提示：操作频繁，请休息一下") == "操作频繁"
    assert find_risk_keyword("需要完成安全验证") == "安全验证"
    assert find_risk_keyword("请稍后再试") == "请稍后再试"


def test_risk_keyword_clean_page():
    assert find_risk_keyword("正常的聊天内容 火花392天") is None
    assert find_risk_keyword("") is None


def test_risk_keyword_priority():
    """多个命中时按关键词表顺序取第一个（太频繁优先于泛化词）。"""
    text = "操作太频繁 安全验证"
    assert find_risk_keyword(text) == "操作太频繁"
