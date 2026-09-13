"""日志：控制台 + 滚动文件 + 内存环形缓冲 + SSE 实时广播。

控制台前端通过 /api/logs/stream 订阅，无需轮询；历史 600 行走 /api/logs/recent。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

RECENT_LIMIT = 600


class LogBus:
    """环形缓冲 + asyncio 队列广播。SSE 订阅端各自持有队列，互不阻塞写日志方。"""

    def __init__(self, limit: int = RECENT_LIMIT):
        self.recent: deque[str] = deque(maxlen=limit)
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, line: str) -> None:
        self.recent.append(line)
        for q in list(self._subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:  # 队列设了上限时丢弃最旧的，保住日志主流程
                pass

    def subscribe(self, maxsize: int = 500) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        for line in list(self.recent):
            q.put_nowait(line)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)


log_bus = LogBus()


class _BusHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            log_bus.publish(line)
        except Exception:  # 日志广播永不影响主流程
            pass


def setup_logging(data_dir: Path, level: str = "INFO") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    file_h = RotatingFileHandler(
        data_dir / "spark.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    root.addHandler(file_h)

    bus_h = _BusHandler()
    bus_h.setFormatter(fmt)
    root.addHandler(bus_h)

    # 降噪：第三方库只留警告
    for noisy in ("httpx", "httpcore", "websockets", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
