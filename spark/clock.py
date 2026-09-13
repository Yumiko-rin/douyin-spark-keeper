"""校时时钟：SNTP 优先，HTTP Date 兜底，最后退回本机时钟。

所有定时判断都经 Clock.now()，本机时钟漂移几十秒也不影响「准时」。
"""
from __future__ import annotations

import logging
import socket
import struct
import time as _time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("clock")

_NTP_EPOCH_DELTA = 2208988800  # 1900-1970 秒差


def sntp_offset(host: str, timeout: float = 3.0) -> float:
    """单次 SNTP 请求，返回 (服务器时间 - 本机时间) 秒。同步阻塞，调用方放线程里。"""
    packet = bytearray(48)
    packet[0] = 0x1B  # LI=0, VN=3, Mode=3(client)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        t0 = _time.time()
        sock.sendto(bytes(packet), (host, 123))
        data, _ = sock.recvfrom(48)
        t3 = _time.time()
        (server_ts,) = struct.unpack("!I", data[40:44])  # Transmit Timestamp 整秒部分
        server_time = server_ts - _NTP_EPOCH_DELTA
        # 经典单次近似：offset = server - (t0+t3)/2，误差约 RTT/2
        return server_time - (t0 + t3) / 2
    finally:
        sock.close()


def http_offset(url: str, timeout: float = 5.0) -> float:
    """用 HTTP 响应头 Date 估算偏移（精度约 1~2 秒，SNTP 不可用时兜底）。"""
    import email.utils

    import httpx

    t0 = _time.time()
    resp = httpx.head(url, timeout=timeout, follow_redirects=True)
    t3 = _time.time()
    date_hdr = resp.headers.get("date")
    if not date_hdr:
        raise ValueError(f"{url} 未返回 Date 头")
    server_dt = email.utils.parsedate_to_datetime(date_hdr)
    if server_dt.tzinfo is None:
        server_dt = server_dt.replace(tzinfo=UTC)
    server_ts = server_dt.timestamp()
    return server_ts - (t0 + t3) / 2


class Clock:
    def __init__(self, tz_name: str = "Asia/Shanghai"):
        self.tz = ZoneInfo(tz_name)
        self._offset = 0.0
        self._source = "local"
        self._last_sync: datetime | None = None

    def now(self) -> datetime:
        return datetime.now(self.tz) + timedelta(seconds=self._offset)

    @property
    def source(self) -> str:
        return self._source

    @property
    def offset(self) -> float:
        return round(self._offset, 3)

    @property
    def last_sync(self) -> datetime | None:
        return self._last_sync

    def sync(self, ntp_servers: list[str]) -> dict:
        """依次尝试 NTP，再尝试 HTTP Date；全部失败保持本机时钟（不抛异常）。"""
        for host in ntp_servers:
            try:
                self._offset = sntp_offset(host)
                self._source = f"ntp:{host}"
                self._last_sync = datetime.now(self.tz)
                log.info("校时成功（%s）：本机偏差 %+.2f 秒", self._source, self._offset)
                return {"ok": True, "source": self._source, "offset": self._offset}
            except Exception as e:
                log.debug("NTP %s 失败：%s", host, e)
        for url in ("https://www.taobao.com", "https://www.baidu.com"):
            try:
                self._offset = http_offset(url)
                self._source = f"http:{url}"
                self._last_sync = datetime.now(self.tz)
                log.info("HTTP 校时成功（%s）：本机偏差 %+.2f 秒", self._source, self._offset)
                return {"ok": True, "source": self._source, "offset": self._offset}
            except Exception as e:
                log.debug("HTTP 校时 %s 失败：%s", url, e)
        log.warning("校时全部失败，继续使用本机时钟")
        return {"ok": False, "source": "local", "offset": self._offset}
