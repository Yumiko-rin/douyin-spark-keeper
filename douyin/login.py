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
    JS_CAPTCHA_DETECT,
    LOGIN_BUTTON,
    LOGIN_PANEL,
    LOGIN_SUCCESS_MARKERS,
    QR_CANVAS,
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
        """返回二维码 PNG 的 base64；抓不到返回 None。

        页面上可能同时有多张 data:image 图片（小图标 + 二维码），
        取面积最大的那张，并过滤掉小于 120px 的图标。
        """
        try:
            imgs = page.locator('img[src^="data:image"]')
            n = min(await imgs.count(), 10)
            best, best_area = None, 0.0
            for i in range(n):
                loc = imgs.nth(i)
                try:
                    if not await loc.is_visible():
                        continue
                    box = await loc.bounding_box()
                    if box and box["width"] * box["height"] > best_area:
                        best, best_area = loc, box["width"] * box["height"]
                except PWError:
                    continue
            if best is not None and best_area >= 120 * 120:
                png = await best.screenshot(timeout=5000)
                return base64.b64encode(png).decode()
        except PWError as e:
            log.debug("data:image 二维码截图失败：%s", e)
        # canvas 形态兜底
        for sel in QR_CANVAS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    png = await loc.screenshot(timeout=5000)
                    return base64.b64encode(png).decode()
            except PWError:
                continue
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

    _JS_CAPTCHA = None  # 已迁移至 selectors.JS_CAPTCHA_DETECT（多信号：文本/元素/iframe）

    async def _detect_captcha(self, page: Page) -> str | None:
        """检测滑块/安全验证，返回命中信号（无则 None）。"""
        try:
            return await page.evaluate(JS_CAPTCHA_DETECT)
        except PWError:
            return None

    _JS_CLICK_LOGIN = """() => {
      const els = [...document.querySelectorAll('button,div,span,a')]
        .filter(e => (e.innerText || '').trim() === '登录' && e.offsetWidth > 0);
      if (!els.length) return null;
      els.sort((a, b) => b.getBoundingClientRect().x - a.getBoundingClientRect().x);
      els[0].click();
      return els[0].tagName + '.' + (els[0].className || '').toString().slice(0, 40);
    }"""

    async def _open_login_page(self, page: Page) -> str | None:
        """打开首页并点出登录框，返回二维码（有则）。

        用 JS 点「最右侧的可见『登录』元素」（实测即右上角按钮）：
        文本定位比 CSS 选择器更抗改版，也避开同选择器下第一个匹配不可见的问题。
        """
        try:
            clicked = await page.evaluate(self._JS_CLICK_LOGIN)
            if clicked:
                log.info("已点击登录入口：%s", clicked)
            else:
                btn = await self._first_locator(page, LOGIN_BUTTON, timeout_ms=2000)
                if btn is not None:
                    await btn.click(timeout=3000)
        except PWError as e:
            log.debug("点击登录入口失败：%s", e)
        return await self._wait_capture(page, timeout_ms=8000)

    async def start(self, account: str, headless: bool | None = None) -> dict:
        """抓取登录二维码。

        headless=None（默认）时优先开**可视浏览器窗口**：登录时人工在场，
        滑块验证直接拖、二维码直接扫窗口里的码，都最快最稳；
        无显示环境（Linux 服务器/Docker）启动失败时自动退回无头模式。
        """
        if headless is None:
            try:
                return await self._start(account, visible=True)
            except Exception as e:
                log.warning("账号 %s 可视窗口登录失败，退回无头模式：%s", account, e)
                await self._safe_reset(account)
                return await self._start(account, visible=False)
        return await self._start(account, visible=not headless)

    async def _safe_reset(self, account: str) -> None:
        try:
            await self.pool.close(account)
        except Exception:
            pass

    async def _start(self, account: str, visible: bool) -> dict:
        # 先按目标模式重建上下文，避免模式不匹配复用旧会话
        await self._safe_reset(account)
        ctx = await self.pool.context_for(account, headless=not visible)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                        timeout=30_000)

        if await self.pool.is_logged_in(ctx):
            return {"logged_in": True, "qr_base64": None, "captcha": None,
                    "visible": visible, "tip": "该账号已登录，无需重新扫码"}

        # 先直接等二维码（部分入口自动弹登录框），再尝试点登录按钮
        qr = await self._wait_capture(page, timeout_ms=5000)
        if qr is None:
            qr = await self._open_login_page(page)
        captcha = await self._detect_captcha(page)

        # 无头模式被滑块验证拦截 → 换可视窗口人工过验证
        if qr is None and captcha and not visible:
            log.warning("账号 %s 登录页出现滑块验证（%s），切换为可视浏览器窗口",
                        account, captcha)
            return {**await self._start(account, visible=True), "captcha": captcha}

        # 无验证码但也没抓到码 → 刷新重试一次
        if qr is None and not captcha:
            await page.reload(wait_until="domcontentloaded")
            qr = await self._open_login_page(page)
            captcha = await self._detect_captcha(page)

        if qr is not None:
            tip = "请用抖音 App 扫一扫（直接扫弹出的浏览器窗口里的码更快）"
        elif captcha:
            tip = ("抖音弹出了滑块/安全验证：请在弹出的浏览器窗口里拖动滑块完成验证，"
                   "页面进入登录页后点「重新出码」")
        else:
            tip = "二维码抓取失败，可在控制台重试；若反复失败请改用 Cookie 导入"
        return {"logged_in": False, "qr_base64": qr, "captcha": captcha,
                "visible": visible, "tip": tip,
                "page_base64": await self._page_shot(page)}

    async def _page_shot(self, page: Page) -> str | None:
        """当前页面实拍（诊断用：控制台直接显示浏览器里看到的画面）。"""
        try:
            png = await page.screenshot(timeout=8000)
            return base64.b64encode(png).decode()
        except PWError as e:
            log.debug("页面截图失败：%s", e)
            return None

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
            # 登录成功但备份缺失时补一份 storage_state（供 Actions/换机迁移）
            backup = self.pool.accounts_dir / account / "storage_state.json"
            if not backup.is_file():
                await self._backup_state(ctx, account)
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
