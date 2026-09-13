"""SQLite 存储测试。"""
from __future__ import annotations

from pathlib import Path

from spark.store import Store


def make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "t.db")


def test_upsert_preserves_selection(tmp_path):
    s = make_store(tmp_path)
    s.upsert_friends("a", [{"name": "侯", "streak_days": 392, "raw": "x"},
                           {"name": "乙", "streak_days": 0, "raw": "y"}])
    # 有火花的默认勾选
    names = {f["name"] for f in s.selected_friends("a")}
    assert names == {"侯"}
    # 手动勾选后，重新同步不丢状态
    s.set_selected("a", ["乙"], True)
    s.upsert_friends("a", [{"name": "侯", "streak_days": 393, "raw": "x"},
                           {"name": "乙", "streak_days": 0, "raw": "y"}])
    names = {f["name"] for f in s.selected_friends("a")}
    assert names == {"侯", "乙"}
    days = {f["name"]: f["streak_days"] for f in s.list_friends("a")}
    assert days["侯"] == 393


def test_delete_stale_friends(tmp_path):
    s = make_store(tmp_path)
    s.upsert_friends("a", [{"name": "保留", "streak_days": 1},
                           {"name": "失效", "streak_days": 0}])
    removed = s.delete_friends_not_seen("a", {"保留"})
    assert removed == 1
    assert [f["name"] for f in s.list_friends("a")] == ["保留"]


def test_send_flow_and_summary(tmp_path):
    s = make_store(tmp_path)
    s.record_send("a", "侯", "ok", detail="发送成功", latency_ms=1200, message="hi")
    s.record_send("a", "乙", "fail", detail="未确认")
    s.record_send("a", "丙", "dry", detail="演练")
    ok = s.sent_ok_names("a")
    assert "侯" in ok and "乙" not in ok
    s.save_day_summary("a", total=3, ok=1, failed=1, dry=1, attempts=2)
    summary = s.get_day_summary("a")
    assert summary["ok"] == 1 and summary["attempts"] == 2
    assert len(s.history("a")) == 3
    stats = s.stats_overview("a")
    assert stats["ok_days"] == 1 and stats["current_streak"] == 1


def test_accounts_isolated(tmp_path):
    s = make_store(tmp_path)
    s.upsert_friends("a", [{"name": "侯", "streak_days": 1}])
    s.upsert_friends("b", [{"name": "乙", "streak_days": 2}])
    assert {f["name"] for f in s.list_friends("a")} == {"侯"}
    assert {f["name"] for f in s.list_friends("b")} == {"乙"}
