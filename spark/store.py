"""SQLite 持久化：好友表、发送流水、每日汇总。

小数据量，直接用 stdlib sqlite3 + 线程锁；WAL 模式下 Web 线程与调度线程互不阻塞。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS friends (
    account     TEXT NOT NULL,
    name        TEXT NOT NULL,
    streak_days INTEGER DEFAULT 0,
    selected    INTEGER DEFAULT 0,
    last_seen   TEXT,
    raw         TEXT,
    updated_at  TEXT,
    PRIMARY KEY (account, name)
);
CREATE TABLE IF NOT EXISTS send_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account    TEXT NOT NULL,
    friend     TEXT NOT NULL,
    date       TEXT NOT NULL,
    status     TEXT NOT NULL,           -- ok / dry / fail
    detail     TEXT,
    latency_ms INTEGER,
    message    TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS day_summary (
    account    TEXT NOT NULL,
    date       TEXT NOT NULL,
    total      INTEGER,
    ok         INTEGER,
    failed     INTEGER,
    dry        INTEGER DEFAULT 0,
    attempts   INTEGER DEFAULT 1,
    note       TEXT,
    created_at TEXT,
    PRIMARY KEY (account, date)
);
CREATE INDEX IF NOT EXISTS idx_sendlog ON send_log (account, date);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- 好友 ----------
    def upsert_friends(self, account: str, friends: list[dict]) -> int:
        """friends: [{name, streak_days, raw}]；保留已有 selected 状态。"""
        now = _now_iso()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "SELECT name, selected FROM friends WHERE account=?", (account,)
            )
            prev_selected = {r["name"]: r["selected"] for r in cur.fetchall()}
            n = 0
            for f in friends:
                selected = prev_selected.get(f["name"])
                if selected is None:
                    # 首次发现：有火花天数的默认勾选
                    selected = 1 if (f.get("streak_days") or 0) >= 1 else 0
                self._conn.execute(
                    """INSERT INTO friends (account,name,streak_days,selected,last_seen,raw,updated_at)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(account,name) DO UPDATE SET
                         streak_days=excluded.streak_days,
                         last_seen=excluded.last_seen,
                         raw=excluded.raw,
                         updated_at=excluded.updated_at""",
                    (account, f["name"], f.get("streak_days") or 0, selected, now,
                     f.get("raw", ""), now),
                )
                n += 1
            return n

    def list_friends(self, account: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM friends WHERE account=? ORDER BY streak_days DESC, name",
                (account,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_selected(self, account: str, names: list[str], selected: bool) -> int:
        with self._lock, self._conn:
            cur = self._conn.cursor()
            n = 0
            for name in names:
                cur.execute(
                    "UPDATE friends SET selected=?, updated_at=? WHERE account=? AND name=?",
                    (1 if selected else 0, _now_iso(), account, name),
                )
                n += cur.rowcount
            return n

    def selected_friends(self, account: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM friends WHERE account=? AND selected=1 ORDER BY streak_days DESC",
                (account,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_friends_not_seen(self, account: str, seen_names: set[str]) -> int:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT name FROM friends WHERE account=?", (account,)
            ).fetchall()
            stale = [r["name"] for r in rows if r["name"] not in seen_names]
            for name in stale:
                self._conn.execute(
                    "DELETE FROM friends WHERE account=? AND name=?", (account, name)
                )
            return len(stale)

    # ---------- 发送流水 ----------
    def record_send(self, account: str, friend: str, status: str, detail: str = "",
                    latency_ms: int = 0, message: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO send_log (account,friend,date,status,detail,latency_ms,message,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (account, friend, _today(), status, detail, latency_ms, message, _now_iso()),
            )

    def sent_ok_names(self, account: str, date: str | None = None) -> set[str]:
        date = date or _today()
        with self._lock:
            rows = self._conn.execute(
                "SELECT friend FROM send_log WHERE account=? AND date=? AND status IN ('ok','dry')",
                (account, date),
            ).fetchall()
        return {r["friend"] for r in rows}

    def history(self, account: str, days: int = 7) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT date, friend, status, detail, latency_ms, message, created_at
                   FROM send_log WHERE account=?
                   ORDER BY id DESC LIMIT ?""",
                (account, max(1, days) * 200),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_day_summary(self, account: str, total: int, ok: int, failed: int,
                         dry: int = 0, attempts: int = 1, note: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO day_summary (account,date,total,ok,failed,dry,attempts,note,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(account,date) DO UPDATE SET
                     total=excluded.total, ok=excluded.ok, failed=excluded.failed,
                     dry=excluded.dry, attempts=excluded.attempts, note=excluded.note,
                     created_at=excluded.created_at""",
                (account, _today(), total, ok, failed, dry, attempts, note, _now_iso()),
            )

    def get_day_summary(self, account: str, date: str | None = None) -> dict | None:
        date = date or _today()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM day_summary WHERE account=? AND date=?", (account, date)
            ).fetchone()
        return dict(row) if row else None

    def stats_overview(self, account: str) -> dict:
        """最近 30 天成功天数与连续成功天数（火花保住了多少天）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT date, SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_cnt
                   FROM send_log WHERE account=? GROUP BY date ORDER BY date DESC LIMIT 60""",
                (account,),
            ).fetchall()
        streak = 0
        for r in rows:
            if r["ok_cnt"] and r["ok_cnt"] > 0:
                streak += 1
            else:
                break
        return {
            "ok_days": sum(1 for r in rows if r["ok_cnt"]),
            "current_streak": streak,
        }


def _today() -> str:
    from datetime import datetime

    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
