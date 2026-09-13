"""配置模型测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from spark.config import AccountConfig, GlobalConfig, atomic_write_json, load_json


def test_config_roundtrip(tmp_path: Path):
    p = tmp_path / "config.json"
    cfg = AccountConfig()
    cfg.send_time = "22:30"
    cfg.gap_seconds = [5, 15]
    cfg.per_friend = {"侯": {"messages": ["{friend} 专属"], "enabled": True}}
    cfg.save(p)
    loaded = AccountConfig.load(p)
    assert loaded.send_time == "22:30"
    assert loaded.gap_range() == (5, 15)
    assert loaded.per_friend["侯"]["messages"] == ["{friend} 专属"]


def test_config_validation():
    with pytest.raises(ValueError):
        AccountConfig.from_json({"send_time": "25:00"})
    with pytest.raises(ValueError):
        AccountConfig.from_json({"messages": ["", " "]})
    with pytest.raises(ValueError):
        AccountConfig.from_json({"jitter_minutes": 500})


def test_gap_clamped():
    assert AccountConfig.from_json({"gap_seconds": [0, 1]}).gap_range() == (3, 3)
    assert AccountConfig.from_json({"gap_seconds": [30, 10]}).gap_range() == (30, 30)


def test_corrupt_json_backed_up(tmp_path: Path):
    p = tmp_path / "config.json"
    p.write_text("{broken", encoding="utf-8")
    cfg = AccountConfig.load(p)
    assert cfg.send_time == "21:00"  # 回退默认值
    assert p.with_suffix(".json.corrupt").exists()  # 原文件已备份


def test_account_name_guard():
    g = GlobalConfig()
    g.data_dir = Path("./data")
    assert "main" in str(g.account_dir("main"))
    with pytest.raises(ValueError):
        g.account_dir("../evil")


def test_atomic_write(tmp_path: Path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1})
    assert load_json(p, None) == {"a": 1}
    assert not p.with_suffix(".json.tmp").exists()


def test_send_window_validation():
    cfg = AccountConfig.from_json({"send_window": ["20:00", "23:00"]})
    assert cfg.send_window == ["20:00", "23:00"]
    with pytest.raises(ValueError):
        AccountConfig.from_json({"send_window": ["23:00", "20:00"]})
    with pytest.raises(ValueError):
        AccountConfig.from_json({"send_window": ["20:00"]})


def test_festivals_validation():
    cfg = AccountConfig.from_json({"festivals": {"01-01": "元旦快乐"}})
    assert cfg.festivals == {"01-01": "元旦快乐"}
    with pytest.raises(ValueError):
        AccountConfig.from_json({"festivals": {"1月1日": "x"}})
    with pytest.raises(ValueError):
        AccountConfig.from_json({"festivals": {"02-30": "不存在的日期"}})


def test_allowlist_roundtrip(tmp_path: Path):
    p = tmp_path / "config.json"
    AccountConfig.from_json({"friend_allowlist": ["侯", "小明"]}).save(p)
    assert AccountConfig.load(p).friend_allowlist == ["侯", "小明"]
