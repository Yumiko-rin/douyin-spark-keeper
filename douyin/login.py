"""扫码登录流程：出二维码 → 等待扫描确认 → 校验 cookie → 完成。

二维码抓取三级降级：img[data:image] → canvas 元素截图 → 登录面板整体截图。
二维码过期自动点刷新（最多 5 次）。持久化上下文会自动落盘 cookie，
这里额外导出一份 storage_state.json 作为备份。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path

from playwright.async_api import BrowserContext, Page
from playwright.async_api import Error as PWError

from douyin.cookies import parse_cookies
from douyin.selectors import (
    LOGIN_BUTTON,
    LOGIN_PANEL,
    LOGIN_SUCCESS_MARKERS,
    QR_CANVAS,
    QR_IMAGE,
    QR_REFRESH,
)
from douyin.session import BrowserPool

log = logging.getLogger("login")

QR_MAX_REFRESH = 5


class LoginFlow:
    def __init__(self, pool: BrowserPool):
        self.pool = pool

    async def _first_locator(self, page: Page, selector_list: list[str], timeout_ms: int = 4000):
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            for sel in selector_list:
                loc = page.locator(sel).first
                try:
                    if await loc.count() and await loc.is_visible():
                        return loc
                except PWError:
                    continue
            await page.wait_for_timeout(400)
        return None

    async def capture_qr(self, page: Page) -> str | None:
        """返回二维码 PNG 的 base64；抓不到返回 None。"""
        for selector_list in (QR_IMAGE, QR_CANVAS):
            loc = await self._first_locator(page, selector_list, timeout_ms=1500)
            if loc is None:
                continue
            try:
                png = await loc.screenshot(timeout=5000)
                return base64.b64encode(png).decode()
            except PWError as e:
                log.debug("二维码截图失败（%s）：%s", selector_list[0], e)
        # 整个登录面板兜底（包含二维码和说明文字，用户仍可扫）
        panel = await self._first_locator(page, LOGIN_PANEL, timeout_ms=1500)
        if panel is not None:
            try:
                png = await panel.screenshot(timeout=5000)
                return base64.b64encode(png).decode()
            except PWError:
                pass
        return None

    async def start(self, account: str, headless: bool = True) -> dict:
        """打开登录页并抓取二维码。返回 {qr_base64, qr_ok, tip}。"""
        ctx = await self.pool.context_for(account, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                        timeout=30_000)
        await page.wait_for_timeout(1500)

        if await self.pool.is_logged_in(ctx):
            return {"logged_in": True, "qr_base64": None,
                    "tip": "该账号已登录，无需重新扫码"}

        # 尝试点出登录面板；部分入口直接就是面板
        btn = await self._first_locator(page, LOGIN_BUTTON, timeout_ms=5000)
        if btn is not None:
            try:
                await btn.click(timeout=3000)
            except PWError:
                pass
        await page.wait_for_timeout(1500)
        qr = await self.capture_qr(page)
        if qr is None:
            # 刷新一次页面重试
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)
            btn = await self._first_locator(page, LOGIN_BUTTON, timeout_ms=5000)
            if btn is not None:
                try:
                    await btn.click(timeout=3000)
                except PWError:
                    pass
            await page.wait_for_timeout(1500)
            qr = await self.capture_qr(page)
        return {
            "logged_in": False,
            "qr_base64": qr,
            "tip": "请用抖音 App 扫一扫" if qr else "二维码抓取失败，可在控制台重试",
        }

    async def wait_login(self, account: str, timeout_s: int = 180,
                         poll_s: float = 2.0) -> dict:
        """轮询等待扫码确认；成功则导出 storage_state 备份。"""
        ctx = await self.pool.context_for(account)
        deadline = asyncio.get_event_loop().time() + timeout_s
        refreshed = 0
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        while asyncio.get_event_loop().time() < deadline:
            if await self.pool.is_logged_in(ctx):
                await self._backup_state(ctx, account)
                # 等页面跳转完成，确认登录面板消失
                await page.wait_for_timeout(2000)
                log.info("账号 %s 扫码登录成功", account)
                return {"logged_in": True, "tip": "登录成功"}
            # 二维码过期处理：面板还在但 img 消失/出现刷新按钮
            if refreshed < QR_MAX_REFRESH:
                refresh = await self._first_locator(page, QR_REFRESH, timeout_ms=800)
                if refresh is not None:
                    try:
                        await refresh.click(timeout=2000)
                        refreshed += 1
                        log.info("账号 %s 二维码已刷新（第 %d 次）", account, refreshed)
                        await page.wait_for_timeout(2500)
                    except PWError:
                        pass
            await asyncio.sleep(poll_s)
        return {"logged_in": False, "tip": "等待超时，请重新获取二维码"}

    async def status(self, account: str) -> dict:
        ctx = await self.pool.context_for(account)
        logged = await self.pool.is_logged_in(ctx)
        nickname = None
        if logged:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if "douyin.com" not in page.url:
                try:
                    await page.goto("https://www.douyin.com/",
                                    wait_until="domcontentloaded", timeout=20_000)
                except PWError:
                    pass
            for sel in LOGIN_SUCCESS_MARKERS:
                try:
                    loc = page.locator(sel).first
                    if await loc.count():
                        nickname = await loc.get_attribute("alt") or "已登录"
                        break
                except PWError:
                    continue
        return {"logged_in": logged, "nickname": nickname}

    async def import_cookies(self, account: str, raw: str) -> dict:
        """导入用户粘贴的 Cookie（JSON 数组或 k=v 文本），免扫码恢复登录态。

        注入后导航一次抖音首页让会话生效，并导出 storage_state 备份。
        """
        cookies = parse_cookies(raw)
        ctx = await self.pool.context_for(account)
        await ctx.add_cookies(cookies)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await page.goto("https://www.douyin.com/",
                            wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2500)
        except PWError as e:
            log.warning("导入 Cookie 后导航失败（可稍后重试检查）：%s", e)
        logged = await self.pool.is_logged_in(ctx)
        await self._backup_state(ctx, account)
        log.info("账号 %s 导入 %d 条 Cookie，登录态：%s", account, len(cookies), logged)
        return {"imported": len(cookies), "logged_in": logged,
                "tip": "Cookie 已生效" if logged else
                       "Cookie 已注入但未检测到登录态，可能已过期或缺失关键字段（sessionid）"}

    async def restore_state(self, account: str, state_path: str) -> dict:
        """从 storage_state.json（Playwright 导出格式）恢复登录态。

        GitHub Actions / 换机迁移场景：Cookie 与 localStorage 一次性注入持久化档案。
        """
        try:
            data = json.loads(Path(state_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"无法读取 storage_state 文件：{e}") from None
        cookies = parse_cookies(json.dumps(data.get("cookies", [])))
        ctx = await self.pool.context_for(account)
        await ctx.add_cookies(cookies)
        origins = data.get("origins") or []
        if origins:
            # storage_state 里 localStorage 是 {name,value} 字典列表，转成 [k,v] 对
            mapping = {
                o.get("origin"): [[i.get("name"), i.get("value")]
                                  for i in o.get("localStorage", [])]
                for o in origins if o.get("origin")
            }
            script = (
                "const MAP = " + json.dumps(mapping, ensure_ascii=False) + ";\n"
                "const items = MAP[location.origin];\n"
                "if (items) for (const [k, v] of items) "
                "try { localStorage.setItem(k, v) } catch (e) {}"
            )
            await ctx.add_init_script(script)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto("https://www.douyin.com/",
                                wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2000)
            except PWError as e:
                log.debug("恢复 localStorage 导航失败：%s", e)
        logged = await self.pool.is_logged_in(ctx)
        await self._backup_state(ctx, account)
        log.info("账号 %s 从 storage_state 恢复（%d 条 Cookie，%d 个 origin），登录态：%s",
                 account, len(cookies), len(origins), logged)
        return {"imported": len(cookies), "origins": len(origins), "logged_in": logged,
                "tip": "登录态已恢复" if logged else
                       "已恢复但未检测到登录态，storage_state 可能已过期"}

    async def _backup_state(self, ctx: BrowserContext, account: str) -> None:
        try:
            path = self.pool.accounts_dir / account / "storage_state.json"
            await ctx.storage_state(path=str(path))
        except (PWError, OSError) as e:
            log.debug("导出 storage_state 备份失败（不影响使用）：%s", e)
