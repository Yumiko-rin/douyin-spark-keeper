"""Cookie 解析测试。"""
from __future__ import annotations

import pytest

from douyin.cookies import parse_cookies


def test_parse_kv_text():
    cookies = parse_cookies("sessionid=abc123; sid_tt=xyz; ttwid=1|0x1")
    by_name = {c["name"]: c for c in cookies}
    assert by_name["sessionid"]["value"] == "abc123"
    assert by_name["sessionid"]["domain"] == ".douyin.com"
    assert by_name["sessionid"]["path"] == "/"
    assert len(cookies) == 3


def test_parse_multiline_and_json_array():
    text = "sessionid=abc\nuid_tt=42"
    assert {c["name"] for c in parse_cookies(text)} == {"sessionid", "uid_tt"}

    raw = '[{"name":"sessionid","value":"v1","domain":"douyin.com","secure":true}]'
    cookies = parse_cookies(raw)
    assert cookies[0]["domain"] == ".douyin.com"  # 自动补点
    assert cookies[0]["secure"] is True


def test_parse_json_dict_form():
    raw = '{"sessionid": "v", "ttwid": "w"}'
    cookies = parse_cookies(raw)
    assert {c["name"] for c in cookies} == {"sessionid", "ttwid"}


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_cookies("")
    with pytest.raises(ValueError):
        parse_cookies("没有等号的内容")
    with pytest.raises(ValueError):
        parse_cookies('[{"value": "有 value 没 name"}]')
