"""应用上下文与 REST/SSE 路由。所有 /api/* 需 X-Auth-Token 头。"""
from __future__ import annotations

import asyncio
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse

from douyin.models import NotLoggedIn
from douyin.session import BrowserPool
from spark import __version__, templates
from spark.clock import Clock
from spark.config import AccountConfig, GlobalConfig, NotifyConfig, load_json
from spark.logger import RECENT_LIMIT, log_bus
from spark.notify import notify_all
from spark.scheduler import SparkScheduler
from spark.store import Store


@dataclass
class AppContext:
    gcfg: GlobalConfig
    store: Store
    clock: Clock
    pool: BrowserPool
    sched: SparkScheduler


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def auth_passes(auth_token: str, host: str, token: str | None) -> bool:
    """鉴权判定：设置了令牌则必须匹配；未设置令牌时仅本机监听允许匿名访问。"""
    if auth_token:
        return bool(token) and hmac.compare_digest(token, auth_token)
    return host in _LOOPBACK_HOSTS


def build_router(ctx: AppContext) -> APIRouter:
    router = APIRouter(prefix="/api")

    def guard(token: str | None = Header(default=None, alias="X-Auth-Token")) -> None:
        if not auth_passes(ctx.gcfg.auth_token, ctx.gcfg.host, token):
            raise HTTPException(401, "需要访问令牌")

    def account_dir(name: str) -> Path:
        try:
            d = ctx.gcfg.account_dir(name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if not (d / "config.json").is_file() and not d.is_dir():
            raise HTTPException(404, f"账号 {name} 不存在")
        return d

    # ---------------- 总览 ----------------
    @router.get("/state")
    async def state(_: None = Depends(guard)):
        accounts = {}
        for name in ctx.sched.account_names():
            friends = ctx.store.list_friends(name)
            summary = ctx.store.get_day_summary(name)
            accounts[name] = {
                "friends_total": len(friends),
                "friends_selected": sum(1 for f in friends if f["selected"]),
                "today": summary,
                "stats": ctx.store.stats_overview(name),
            }
        for name, extra in ctx.sched.status().items():
            accounts.setdefault(name, {})
            accounts[name]["status"] = {k: v for k, v in extra.items() if k != "config"}
            accounts[name]["config"] = extra["config"]
        return {
            "version": __version__,
            "server_time": ctx.clock.now().isoformat(timespec="seconds"),
            "clock": {"source": ctx.clock.source, "offset_s": ctx.clock.offset,
                      "last_sync": str(ctx.clock.last_sync or "")},
            "accounts": accounts,
            "notify_channels": ctx.gcfg.load_notify().enabled_channels(),
        }

    @router.get("/clock")
    async def clock_info(_: None = Depends(guard)):
        return {"now": ctx.clock.now().isoformat(timespec="seconds"),
                "source": ctx.clock.source, "offset_s": ctx.clock.offset}

    # ---------------- 账号管理 ----------------
    @router.post("/accounts")
    async def add_account(body: dict, _: None = Depends(guard)):
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 30:
            raise HTTPException(400, "账号名需为 1~30 个字符")
        d = ctx.gcfg.account_dir(name)  # 顺带校验名称合法性
        if d.is_dir():
            raise HTTPException(409, f"账号 {name} 已存在")
        d.mkdir(parents=True)
        AccountConfig().save(d / "config.json")
        ctx.sched.add_account(name)
        return {"ok": True}

    @router.delete("/accounts/{name}")
    async def remove_account(name: str, _: None = Depends(guard)):
        d = account_dir(name)
        ctx.sched.remove_account(name)
        await ctx.pool.close(name)
        archive = ctx.gcfg.data_dir / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / f"{name}.{datetime.now().astimezone().strftime('%Y%m%d%H%M%S')}"
        d.rename(target)  # 归档而非删除，可手动恢复
        return {"ok": True, "archived_to": str(target)}

    # ---------------- 登录 ----------------
    @router.post("/accounts/{name}/login/start")
    async def login_start(name: str, body: dict | None = None,
                          _: None = Depends(guard)):
        account_dir(name)
        cfg = ctx.sched._load_cfg(name)
        headless = (body or {}).get("headless")
        if headless is None:
            headless = cfg.headless if cfg.headless is not None else ctx.gcfg.headless
        return await ctx.sched.login.start(name, headless=headless)

    @router.get("/accounts/{name}/login/wait")
    async def login_wait(name: str, timeout: int = 150,
                         _: None = Depends(guard)):
        account_dir(name)
        timeout = min(max(timeout, 10), 300)
        return await ctx.sched.login.wait_login(name, timeout_s=timeout)

    @router.get("/accounts/{name}/login/status")
    async def login_status(name: str, _: None = Depends(guard)):
        return await ctx.sched.login.status(name)

    @router.post("/accounts/{name}/cookies")
    async def import_cookies(name: str, body: dict, _: None = Depends(guard)):
        """导入 Cookie 免扫码：raw 为 k=v 文本串或浏览器导出的 JSON 数组。"""
        account_dir(name)
        raw = str(body.get("raw", "")).strip()
        if not raw:
            raise HTTPException(400, "raw 不能为空")
        try:
            return await ctx.sched.login.import_cookies(name, raw)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    # ---------------- 配置 ----------------
    @router.get("/accounts/{name}/config")
    async def get_config(name: str, _: None = Depends(guard)):
        return AccountConfig.load(ctx.gcfg.account_config_path(name)).to_json()

    @router.put("/accounts/{name}/config")
    async def put_config(name: str, body: dict, _: None = Depends(guard)):
        account_dir(name)
        try:
            cfg = AccountConfig.from_json(body)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f"配置不合法：{e}") from None
        problems = templates.validate_pool(cfg.messages)
        if problems:
            raise HTTPException(400, "；".join(problems))
        cfg.save(ctx.gcfg.account_config_path(name))
        return {"ok": True}

    # ---------------- 好友 ----------------
    @router.get("/accounts/{name}/friends")
    async def get_friends(name: str, _: None = Depends(guard)):
        account_dir(name)
        friends = ctx.store.list_friends(name)
        per_friend = ctx.sched._load_cfg(name).per_friend
        for f in friends:
            f["per_friend"] = per_friend.get(f["name"], {})
        return friends

    @router.post("/accounts/{name}/friends/sync")
    async def sync_friends(name: str, _: None = Depends(guard)):
        account_dir(name)
        try:
            async with ctx.sched._lock_for(name):
                return await ctx.sched.run_sync(name)
        except NotLoggedIn as e:
            raise HTTPException(409, f"{e}（请先到「账号」页扫码登录）") from None

    @router.post("/accounts/{name}/friends/select")
    async def select_friends(name: str, body: dict, _: None = Depends(guard)):
        account_dir(name)
        n = ctx.store.set_selected(name, body.get("names") or [],
                                   bool(body.get("selected", True)))
        return {"ok": True, "updated": n}

    # ---------------- 运行控制 ----------------
    @router.post("/accounts/{name}/run/send")
    async def run_send(name: str, body: dict, _: None = Depends(guard)):
        account_dir(name)
        dry = bool(body.get("dry", False))
        force = bool(body.get("force", False))
        try:
            return await ctx.sched.run_send_manual(name, dry=dry, force=force)
        except NotLoggedIn as e:
            raise HTTPException(409, f"{e}（请先扫码登录）") from None

    @router.post("/accounts/{name}/run/check")
    async def run_check(name: str, _: None = Depends(guard)):
        account_dir(name)
        return await ctx.sched.run_check(name)

    # ---------------- 历史 ----------------
    @router.get("/accounts/{name}/history")
    async def history(name: str, days: int = 7, _: None = Depends(guard)):
        account_dir(name)
        return {"log": ctx.store.history(name, days),
                "summary": ctx.store.get_day_summary(name),
                "stats": ctx.store.stats_overview(name)}

    # ---------------- 通知 ----------------
    @router.get("/notify")
    async def get_notify(_: None = Depends(guard)):
        return ctx.gcfg.load_notify().to_json()

    @router.put("/notify")
    async def put_notify(body: dict, _: None = Depends(guard)):
        ctx.gcfg.save_notify(NotifyConfig.from_json(body))
        return {"ok": True, "enabled": ctx.gcfg.load_notify().enabled_channels()}

    @router.post("/notify/test")
    async def test_notify(_: None = Depends(guard)):
        r = await notify_all(ctx.gcfg.load_notify(),
                             "[火花管家] 测试推送",
                             f"这是一条测试消息，服务端时间 {ctx.clock.now():%H:%M:%S}。")
        if not r:
            raise HTTPException(400, "未配置任何通知渠道")
        return r

    # ---------------- 日志 ----------------
    @router.get("/logs/recent")
    async def logs_recent(n: int = RECENT_LIMIT, _: None = Depends(guard)):
        lines = list(log_bus.recent)
        return {"lines": lines[-min(max(n, 10), 2000):]}

    @router.get("/logs/stream")
    async def logs_stream(token: str = ""):
        if not auth_passes(ctx.gcfg.auth_token, ctx.gcfg.host, token):
            raise HTTPException(401, "需要访问令牌")
        queue = log_bus.subscribe()

        async def gen():
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                log_bus.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ---------------- 备份 ----------------
    @router.get("/backup")
    async def backup(_: None = Depends(guard)):
        out = {}
        for name in ctx.sched.account_names():
            out[name] = load_json(ctx.gcfg.account_config_path(name), {})
        return {"accounts": out, "notify": ctx.gcfg.load_notify().to_json()}

    return router
