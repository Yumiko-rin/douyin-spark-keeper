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

    async def _wait_capture(self, page: Page, timeout_ms: int = 8000) -> str | None:
        """快速轮询等二维码出现，一出现立刻截图（不再固定睡大觉）。"""
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            qr = await self.capture_qr(page)
            if qr is not None:
                return qr
            await page.wait_for_timeout(200)
        return None

    _JS_CAPTCHA = """() => {
        const t = document.body ? document.body.innerText : '';
        return t.includes('请完成下列验证') || t.includes('拖动') ||
               (t.includes('安全验证') && t.includes('继续'));
    }"""

    async def _detect_captcha(self, page: Page) -> bool:
        """检测是否被抖音滑块/安全验证拦截。"""
        try:
            return bool(await page.evaluate(self._JS_CAPTCHA))
        except PWError:
            return False

    async def _open_login_page(self, page: Page) -> str | None:
        """打开首页并点出登录框，返回二维码（有则）。"""
        btn = await self._first_locator(page, LOGIN_BUTTON, timeout_ms=4000)
        if btn is not None:
            try:
                await btn.click(timeout=3000)
            except PWError:
                pass
        return await self._wait_capture(page, timeout_ms=8000)

    async def start(self, account: str, headless: bool = True) -> dict:
        """打开登录页并抓取二维码。返回 {qr_base64, captcha, tip}。

        遇到滑块验证且当前是无头模式时，自动改开可视浏览器窗口，
        让用户直接拖滑块、扫窗口里的码（比截图传网页更快更稳）。
        """
        ctx = await self.pool.context_for(account, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                        timeout=30_000)

        if await self.pool.is_logged_in(ctx):
            return {"logged_in": True, "qr_base64": None, "captcha": False,
                    "tip": "该账号已登录，无需重新扫码"}

        # 先直接等二维码（部分入口自动弹登录框），再尝试点登录按钮
        qr = await self._wait_capture(page, timeout_ms=5000)
        if qr is None:
            qr = await self._open_login_page(page)
        captcha = await self._detect_captcha(page)

        # 滑块验证挡路 → 换可视窗口人工过验证
        if qr is None and captcha and headless:
            log.warning("账号 %s 登录页出现滑块验证，切换为可视浏览器窗口", account)
            try:
                await self.pool.close(account)
                ctx = await self.pool.context_for(account, headless=False)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://www.douyin.com/",
                                wait_until="domcontentloaded", timeout=30_000)
                headless = False
                qr = await self._open_login_page(page)
                captcha = await self._detect_captcha(page)
            except Exception as e:
                log.warning("可视窗口启动失败（服务器无显示环境？）：%s", e)

        # 仍无二维码且没被验证码拦截 → 刷新重试一次
        if qr is None and not captcha:
            await page.reload(wait_until="domcontentloaded")
            qr = await self._open_login_page(page)

        if qr is not None:
            tip = "请用抖音 App 扫一扫（若弹出了浏览器窗口，直接扫窗口里的码更快）"
        elif captcha:
            tip = ("抖音弹出了滑块/安全验证：请在弹出的浏览器窗口里完成验证，"
                   "然后点「重新出码」")
        else:
            tip = "二维码抓取失败，可在控制台重试；若反复失败请改用 Cookie 导入"
        return {"logged_in": False, "qr_base64": qr, "captcha": captcha, "tip": tip}

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
            # 被滑块/安全验证拦截时不傻等：提示用户先完成验证
            if await self._detect_captcha(page):
                log.warning("账号 %s 等待扫码期间出现滑块验证", account)
                return {"logged_in": False,
                        "tip": "页面出现滑块/安全验证：请在浏览器窗口完成验证后，点「重新出码」"}
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
