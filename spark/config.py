"""配置模型与原子读写。

全局运维参数从 .env 读取；每个账号一份 config.json（data/accounts/<名称>/config.json），
通知渠道在 data/global.json。所有写盘都走原子写（临时文件 + os.replace），
进程中途被杀也不会留下半截配置。
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_MESSAGES = [
    "{friend}，火花别灭呀 🔥",
    "今日份火花已续上 ✅",
    "签到~ 续火花 {date}",
    "{weekday}愉快，火花+1 🔥",
]


def load_env(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，# 注释，忽略空行；不覆盖已存在的环境变量。"""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        env[key] = value
        os.environ.setdefault(key, value)
    return env


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # 坏文件不直接覆盖，先备份再返回默认值，避免用户数据静默丢失
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
        return default


def _parse_gap(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        lo, hi = int(value[0]), int(value[1])
    else:
        lo, hi = 8, 20
    lo = max(3, lo)
    hi = max(lo, hi)
    return [lo, hi]


@dataclass
class AccountConfig:
    """每账号定时发送策略。

    默认 jitter_minutes=0：火花关键在「今天发出去」，到点即发最准时；
    想更像人可自行设置 5~15 分钟抖动。
    """

    enabled: bool = True
    send_time: str = "21:00"           # 每日发送时刻（本地时区，经 NTP 校正）
    jitter_minutes: int = 0            # 发送时刻随机抖动（0 = 分秒不差）
    deadline_time: str = "23:55"       # 保底重试截止时刻，超过则放弃并告警
    prewarm_minutes: int = 2           # 发送前 N 分钟预热浏览器
    retry_interval_minutes: int = 10   # 整轮失败后的重试间隔
    max_attempts: int = 8              # 截止前最多整轮尝试次数
    gap_seconds: list[int] = field(default_factory=lambda: [8, 20])  # 好友间停顿区间
    max_friends_per_run: int = 30      # 单轮最多发送人数
    allow_first_message: bool = False  # 是否允许给无会话记录的好友发首条消息
    messages: list[str] = field(default_factory=lambda: list(DEFAULT_MESSAGES))
    per_friend: dict[str, dict] = field(default_factory=dict)  # 好友名 -> {messages,enabled,search_name}
    friend_allowlist: list[str] = field(default_factory=list)  # 非空时直接作为发送名单（Actions/无DB场景）
    send_window: list[str] | None = None  # 发送窗口 ["20:00","23:00"]：窗口内随机时刻，非空时优先于 send_time
    festivals: dict[str, str] = field(default_factory=dict)  # "MM-DD" -> 祝福语，填充 {festival} 变量
    auto_sync_friends: bool = True     # 每天 health_check_time 顺带同步好友列表
    health_check_time: str = "10:00"   # 每日登录态巡检时刻
    notify_on_success: bool = False    # 成功后是否推送摘要
    notify_on_failure: bool = True     # 失败/登录态失效必推送
    risk_halt_notify: bool = True      # 触发限流熔断时立即推送
    debug_screenshot: bool = True      # 发送失败保存页面截图（诊断改版/风控）
    debug_trace: bool = False          # 发送轮保存 Playwright Trace（体积大，按需开）
    headless: bool | None = None       # None 跟随全局 HEADLESS

    # ---- 序列化 ----
    def to_json(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, data: dict) -> AccountConfig:
        base = cls()
        merged = base.to_json()
        for key in merged:
            if key in data and data[key] is not None:
                merged[key] = data[key]
        merged["gap_seconds"] = _parse_gap(data.get("gap_seconds"))
        cfg = cls(**merged)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: Path) -> AccountConfig:
        return cls.from_json(load_json(path, {}))

    def save(self, path: Path) -> None:
        self.validate()
        atomic_write_json(path, self.to_json())

    # ---- 校验 ----
    def validate(self) -> None:
        for name in ("send_time", "deadline_time", "health_check_time"):
            _parse_hhmm_str(getattr(self, name), name)
        if self.send_window is not None:
            if (not isinstance(self.send_window, list) or len(self.send_window) != 2):
                raise ValueError("send_window 需为 [\"HH:MM\", \"HH:MM\"]")
            start, end = (_parse_hhmm_str(v, "send_window") for v in self.send_window)
            if start >= end:
                raise ValueError("send_window 起点需早于终点")
        if self.jitter_minutes < 0 or self.jitter_minutes > 120:
            raise ValueError("jitter_minutes 需在 0~120 之间")
        if self.prewarm_minutes < 0 or self.prewarm_minutes > 30:
            raise ValueError("prewarm_minutes 需在 0~30 之间")
        if self.max_friends_per_run < 1:
            raise ValueError("max_friends_per_run 至少为 1")
        if not self.messages or not any(m.strip() for m in self.messages):
            raise ValueError("messages 文案池不能为空")
        for key, value in self.festivals.items():
            try:
                month, day = key.split("-")
                date(2024, int(month), int(day))
            except (ValueError, AttributeError, TypeError):
                raise ValueError(f"festivals 键需为 MM-DD 格式：{key!r}") from None
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"festivals 值需为非空字符串：{key!r}")

    def gap_range(self) -> tuple[int, int]:
        return int(self.gap_seconds[0]), int(self.gap_seconds[1])


@dataclass
class NotifyConfig:
    """推送渠道，任一渠道留空即禁用。除文件外也可用同名大写环境变量（加 NOTIFY_ 前缀）配置。"""

    serverchan_sendkey: str = ""   # Server酱 https://sct.ftqq.com/
    bark_url: str = ""             # 如 https://api.day.app/你的Key
    telegram_token: str = ""
    telegram_chat_id: str = ""
    wecom_webhook: str = ""        # 企业微信群机器人 webhook
    dingtalk_webhook: str = ""     # 钉钉群机器人 webhook（含 access_token）
    dingtalk_secret: str = ""      # 钉钉加签密钥（未开启加签则留空）
    custom_webhook: str = ""       # 自定义：POST {"title","body","level"}
    proxy: str = ""                # 访问 Telegram 需要的代理，如 http://127.0.0.1:7890

    def to_json(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_json(cls, data: dict) -> NotifyConfig:
        base = cls().to_json()
        base.update({k: v for k, v in (data or {}).items() if k in base})
        # 环境变量兜底（GitHub Actions 等无文件场景）：文件值优先
        for key in base:
            env_val = os.environ.get(("NOTIFY_" + key).upper())
            if env_val and not base[key]:
                base[key] = env_val
        return cls(**base)

    def enabled_channels(self) -> list[str]:
        c = self
        names = []
        if c.serverchan_sendkey:
            names.append("serverchan")
        if c.bark_url:
            names.append("bark")
        if c.telegram_token and c.telegram_chat_id:
            names.append("telegram")
        if c.wecom_webhook:
            names.append("wecom")
        if c.dingtalk_webhook:
            names.append("dingtalk")
        if c.custom_webhook:
            names.append("webhook")
        return names


def _parse_hhmm_str(value: str, field_name: str) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return h, m
    except (ValueError, AttributeError):
        raise ValueError(f"{field_name} 需为 HH:MM 格式，当前为 {value!r}") from None


@dataclass
class GlobalConfig:
    host: str = "127.0.0.1"
    port: int = 8020
    auth_token: str = ""
    tz: str = "Asia/Shanghai"
    data_dir: Path = Path("./data")
    headless: bool = True
    ntp_servers: list[str] = field(
        default_factory=lambda: ["ntp.aliyun.com", "ntp.tencent.com", "cn.pool.ntp.org"]
    )
    log_level: str = "INFO"
    max_contexts: int = 5
    proxy: str = ""   # 浏览器全局代理，如 http://127.0.0.1:7890（风控建议同城直连）

    @classmethod
    def from_env(cls) -> GlobalConfig:
        env = os.environ
        cfg = cls(
            host=env.get("HOST", "127.0.0.1"),
            port=int(env.get("PORT", "8020")),
            auth_token=env.get("AUTH_TOKEN", ""),
            tz=env.get("TZ", "Asia/Shanghai"),
            data_dir=Path(env.get("DATA_DIR", "./data")).resolve(),
            headless=env.get("HEADLESS", "true").lower() not in ("0", "false", "no"),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            max_contexts=max(1, int(env.get("MAX_CONTEXTS", "5"))),
            proxy=env.get("PROXY", ""),
        )
        if env.get("NTP_SERVERS"):
            cfg.ntp_servers = [s.strip() for s in env["NTP_SERVERS"].split(",") if s.strip()]
        return cfg

    # ---- 常用路径 ----
    def accounts_dir(self) -> Path:
        return self.data_dir / "accounts"

    def account_dir(self, name: str) -> Path:
        # 名称只允许安全字符，防路径穿越
        safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_一-鿿")
        if not safe or safe != name:
            raise ValueError("账号名只能包含中文、字母、数字、- 和 _")
        return self.accounts_dir() / safe

    def account_config_path(self, name: str) -> Path:
        return self.account_dir(name) / "config.json"

    def global_json_path(self) -> Path:
        return self.data_dir / "global.json"

    def db_path(self) -> Path:
        return self.data_dir / "spark.db"

    # ---- 通知配置 ----
    def load_notify(self) -> NotifyConfig:
        data = load_json(self.global_json_path(), {})
        return NotifyConfig.from_json(data.get("notify", {}))

    def save_notify(self, notify: NotifyConfig) -> None:
        data = load_json(self.global_json_path(), {})
        data["notify"] = notify.to_json()
        atomic_write_json(self.global_json_path(), data)
