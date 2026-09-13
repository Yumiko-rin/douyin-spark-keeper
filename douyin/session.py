"""浏览器会话池：共享一个 Playwright 实例，每账号一个持久化上下文。

- launch_persistent_context：登录态（cookie/localStorage/IndexedDB）落盘在
  accounts/<名>/profile，进程重启无需重新扫码，比 storage_state 方案更稳。
- 轻量反检测：禁用 AutomationControlled、隐藏 navigator.webdriver、中文环境。
- 上下文数量上限 + LRU 淘汰，多账号服务器部署不会撑爆内存。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from pathlib import Path

from playwright.async_api import BrowserContext, Playwright, async_playwright

from douyin.models import NotLoggedIn
from douyin.selectors import CHAT_URL, LOGIN_COOKIE_KEYS

log = logging.getLogger("session")

_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
"""


class BrowserPool:
    def __init__(self, accounts_dir: Path, headless: bool = True, max_contexts: int = 5,
                 tz: str = "Asia/Shanghai", proxy: str = ""):
        self.accounts_dir = accounts_dir
        self.headless = headless
        self.max_contexts = max(1, max_contexts)
        self.tz = tz
        self.proxy = proxy.strip()
        self._pw: Playwright | None = None
        self._contexts: OrderedDict[str, BrowserContext] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._starting = False

    async def start(self) -> None:
        if self._pw is None:
            self._pw = await async_playwright().start()
            log.info("Playwright 已启动（headless=%s）", self.headless)

    async def stop(self) -> None:
        for name in list(self._contexts):
            await self.close(name)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
            log.info("Playwright 已关闭")

    def _lock_for(self, account: str) -> asyncio.Lock:
        return self._locks.setdefault(account, asyncio.Lock())

    async def context_for(self, account: str, headless: bool | None = None) -> BrowserContext:
        """取（或创建）账号的持久化上下文，带 LRU 淘汰与崩溃重建。"""
        async with self._lock_for(account):
            ctx = self._contexts.get(account)
            if ctx is not None and ctx.browser is not None:
                self._contexts.move_to_end(account)
                return ctx
            if ctx is not None:  # 浏览器进程已崩，清掉重建
                await self._discard(account)
            await self.start()
            assert self._pw is not None
            if len(self._contexts) >= self.max_contexts:
                evicted, _ = self._contexts.popitem(last=False)
                log.info("上下文数量达到上限，关闭最久未用的账号：%s", evicted)
                await self._safe_close(evicted)
            profile_dir = self.accounts_dir / account / "profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            headless = self.headless if headless is None else headless
            args = [
                "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            launch_kwargs: dict = {
                "headless": headless,
                "args": args,
                "ignore_default_args": ["--enable-automation"],
                "locale": "zh-CN",
                "timezone_id": self.tz,
                "viewport": {"width": 1366, "height": 850},
            }
            if self.proxy:
                launch_kwargs["proxy"] = {"server": self.proxy}
            ctx = await self._pw.chromium.launch_persistent_context(
                str(profile_dir), **launch_kwargs)
            await ctx.add_init_script(_STEALTH_JS)
            self._contexts[account] = ctx
            log.info("浏览器上下文已就绪：%s（headless=%s）", account, headless)
            return ctx

    async def _discard(self, account: str) -> None:
        self._contexts.pop(account, None)

    async def _safe_close(self, account: str) -> None:
        ctx = self._contexts.pop(account, None)
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass

    async def close(self, account: str) -> None:
        async with self._lock_for(account):
            await self._safe_close(account)

    # ---------- 高层 API ----------
    async def chat_page(self, account: str, headless: bool | None = None):
        """返回已打开聊天页的 (context, page)。未登录抛 NotLoggedIn。"""
        ctx = await self.context_for(account, headless=headless)
        if not await self.is_logged_in(ctx):
            raise NotLoggedIn(f"账号 {account} 登录态失效，请在控制台重新扫码")
        page = await self._ensure_chat_page(ctx)
        return ctx, page

    async def is_logged_in(self, ctx: BrowserContext) -> bool:
        cookies = {c["name"]: c for c in await ctx.cookies()}
        return any(cookies.get(k, {}).get("value") for k in LOGIN_COOKIE_KEYS)

    async def _ensure_chat_page(self, ctx: BrowserContext):
        for page in ctx.pages:
            if "douyin.com/chat" in page.url and not page.is_closed():
                if page.url.startswith("about:"):
                    continue
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                return page
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        for attempt, url in enumerate((CHAT_URL, CHAT_URL + "?isPopup=1")):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2500)
                return page
            except Exception as e:
                log.debug("打开聊天页失败（第 %d 次）：%s", attempt + 1, e)
        raise RuntimeError("聊天页打开失败，请检查网络或稍后重试")

    async def health(self, account: str) -> dict:
        """巡检：cookie 是否健在 + 聊天页能否打开。返回给通知用的摘要。"""
        started = time.time()
        try:
            ctx, page = await self.chat_page(account)
            ok = await self.is_logged_in(ctx)
            return {
                "ok": ok, "latency_ms": int((time.time() - started) * 1000),
                "detail": "登录态正常" if ok else "cookie 缺失",
                "url": page.url,
            }
        except NotLoggedIn as e:
            return {"ok": False, "latency_ms": int((time.time() - started) * 1000),
                    "detail": str(e), "url": ""}
        except Exception as e:
            return {"ok": False, "latency_ms": int((time.time() - started) * 1000),
                    "detail": f"浏览器异常：{e}", "url": ""}
