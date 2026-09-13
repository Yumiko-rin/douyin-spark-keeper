"""推送通知：Server酱 / Bark / Telegram / 企业微信 / 钉钉 / 自定义 webhook。

原则：通知失败只记日志，绝不影响发送主流程。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
from urllib.parse import quote

import httpx

from spark.config import NotifyConfig

log = logging.getLogger("notify")

_TIMEOUT = 10.0


async def notify_all(cfg: NotifyConfig, title: str, body: str, level: str = "info") -> list[dict]:
    """并发推送到所有已配置渠道，返回 [{channel, ok, error}]。"""
    tasks = []
    if cfg.serverchan_sendkey:
        tasks.append(_serverchan(cfg.serverchan_sendkey, title, body))
    if cfg.bark_url:
        tasks.append(_bark(cfg.bark_url, title, body, level))
    if cfg.telegram_token and cfg.telegram_chat_id:
        tasks.append(_telegram(cfg, title, body))
    if cfg.wecom_webhook:
        tasks.append(_wecom(cfg.wecom_webhook, title, body))
    if cfg.dingtalk_webhook:
        tasks.append(_dingtalk(cfg, title, body))
    if cfg.custom_webhook:
        tasks.append(_custom(cfg.custom_webhook, title, body, level))
    if not tasks:
        return []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"channel": "?", "ok": False, "error": str(r)})
            log.warning("通知渠道异常：%s", r)
        else:
            out.append(r)
            if not r["ok"]:
                log.warning("通知 %s 失败：%s", r["channel"], r["error"])
            else:
                log.info("通知 %s 已送达", r["channel"])
    return out


def dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    """钉钉加签：HMAC-SHA256(secret, "{ts}\n{secret}") 后 Base64。纯函数可单测。"""
    string_to_sign = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


async def _post_json(url: str, payload: dict, proxy: str = "") -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT, proxy=proxy or None) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return {"ok": True, "error": "", "status": resp.status_code}


async def _serverchan(key: str, title: str, body: str) -> dict:
    url = f"https://sctapi.ftqq.com/{key}.send"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(url, data={"title": title[:32], "desp": body})
        resp.raise_for_status()
        return {"channel": "serverchan", "ok": True, "error": ""}


async def _bark(base: str, title: str, body: str, level: str) -> dict:
    base = base.rstrip("/")
    sound = "alert" if level == "critical" else "chime"
    url = f"{base}/{quote(title, safe='')}/{quote(body, safe='')}?group=spark&sound={sound}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return {"channel": "bark", "ok": True, "error": ""}


async def _telegram(cfg: NotifyConfig, title: str, body: str) -> dict:
    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    r = await _post_json(
        url,
        {"chat_id": cfg.telegram_chat_id, "text": f"{title}\n{body}"},
        proxy=cfg.proxy,
    )
    return {"channel": "telegram", **r}


async def _wecom(webhook: str, title: str, body: str) -> dict:
    r = await _post_json(
        webhook, {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
    )
    return {"channel": "wecom", **r}


async def _dingtalk(cfg: NotifyConfig, title: str, body: str) -> dict:
    url = cfg.dingtalk_webhook
    if cfg.dingtalk_secret:
        ts = int(time.time() * 1000)
        sign = quote(dingtalk_sign(cfg.dingtalk_secret, ts), safe="")
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"
    r = await _post_json(
        url, {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
    )
    return {"channel": "dingtalk", **r}


async def _custom(webhook: str, title: str, body: str, level: str) -> dict:
    r = await _post_json(webhook, {"title": title, "body": body, "level": level})
    return {"channel": "webhook", **r}
