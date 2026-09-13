"""DOM 驱动：好友同步 + 消息发送 + 多层防护。

1. 防错发：打开会话后核对当前会话标题是否为目标好友（借鉴同类项目教训）；
2. 送达确认：DOM 层最近气泡出现文本（主证据）+ IM 接口成功响应（辅证据）；
3. 限流熔断：页面出现「操作频繁/安全验证」立即报告，调度层停止整轮；
4. 失败诊断：失败时保存页面截图，便于排查改版与风控。
确认失败自动重试；宁可重发一条重复消息，也不能让火花断掉。
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path

from playwright.async_api import Error as PWError
from playwright.async_api import Locator, Page

from douyin.models import Friend, SendOutcome
from douyin.parse import parse_row
from douyin.selectors import (
    CONVERSATION_ROW,
    FILE_INPUT,
    JS_COLLECT_ROWS,
    JS_ENABLE_SCREEN_READER,
    JS_TEXT_IN_BUBBLES,
    MESSAGE_INPUT,
    RISK_KEYWORDS,
    SEARCH_BOX,
    SEARCH_RESULT,
)

log = logging.getLogger("dom")

_COLLECT_ROUNDS = 6
IMAGE_PREFIX = "image:"  # 文案池条目以此前缀表示发送本地图片（实验性）
_KEEP_SNAPSHOTS = 20


def find_risk_keyword(text: str) -> str | None:
    """在页面文本中查找限流/风控信号，返回命中的关键词。纯函数可单测。"""
    for kw in RISK_KEYWORDS:
        if kw in text:
            return kw
    return None


async def detect_risk_control(page: Page) -> str | None:
    try:
        body = await page.evaluate("() => document.body ? document.body.innerText : ''")
    except PWError:
        return None
    return find_risk_keyword(body or "")


async def snapshot(page: Page, dir_path: Path, tag: str) -> str | None:
    """保存诊断截图（目录内只保留最近 N 张），失败静默。"""
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / f"{re.sub(r'[^\w-]', '_', tag)}-{time.strftime('%H%M%S')}.png"
        await page.screenshot(path=str(path), timeout=8000)
        for old in sorted(dir_path.glob("*.png"))[:-_KEEP_SNAPSHOTS]:
            old.unlink(missing_ok=True)
        log.info("已保存诊断截图：%s", path.name)
        return str(path)
    except (PWError, OSError) as e:
        log.debug("截图失败：%s", e)
        return None


async def _first_of(page: Page, selector_list: list[str], timeout_ms: int) -> Locator | None:
    """在候选选择器中找到第一个可见元素。"""
    per = max(300, timeout_ms // max(1, len(selector_list)))
    for sel in selector_list:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=per)
            return loc
        except PWError:
            continue
    return None


async def enable_screen_reader(page: Page) -> None:
    """尽力开启读屏标签增强（失败静默，不影响主流程）。"""
    try:
        hit = await page.evaluate(JS_ENABLE_SCREEN_READER)
        if hit:
            log.info("已开启读屏标签：%s", hit)
            await page.wait_for_timeout(800)
    except PWError:
        pass


async def collect_rows(page: Page) -> list[dict]:
    rows: list[dict] = []
    for _ in range(_COLLECT_ROUNDS):
        try:
            rows.extend(await page.evaluate(JS_COLLECT_ROWS))
        except PWError as e:
            log.debug("行采集失败：%s", e)
        await page.wait_for_timeout(700)
    return rows


async def sync_friends(page: Page) -> list[Friend]:
    """从聊天页会话列表解析好友（含火花天数），同名去重，火花多者排前。"""
    await enable_screen_reader(page)
    rows = await collect_rows(page)
    friends: dict[str, Friend] = {}
    for row in rows:
        f = parse_row(row)
        if f and (f.name not in friends or f.streak_days > friends[f.name].streak_days):
            friends[f.name] = f
    log.info("会话解析完成：%d 行 -> %d 名好友", len(rows), len(friends))
    return sorted(friends.values(), key=lambda x: -x.streak_days)


async def open_friend(page: Page, name: str, allow_first_message: bool = False,
                      search_key: str | None = None) -> str:
    """通过搜索定位并打开与指定好友的会话。

    search_key：好友的备注/别名/抖音号（per_friend.search_name），搜索时用它，
    但会话标题校验仍用同步到的昵称 name。
    返回："ok" 已打开；"no_conversation" 会话列表无此人且未允许发首条；"not_found" 找不到。
    """
    key = (search_key or name).strip()
    # 会话列表里是否已有该好友（有 = 至少聊过一次）
    if not allow_first_message:
        has_conversation = False
        for sel in CONVERSATION_ROW:
            try:
                if await page.locator(sel).filter(has_text=name).count():
                    has_conversation = True
                    break
            except PWError:
                continue
        if not has_conversation:
            log.info("「%s」无会话记录且未开启 allow_first_message，跳过", name)
            return "no_conversation"

    box = await _first_of(page, SEARCH_BOX, timeout_ms=8000)
    if box is None:
        log.warning("找不到搜索框")
        return "not_found"
    try:
        await box.click()
        await box.fill("")
        await box.press_sequentially(key, delay=random.randint(25, 60))
        await page.wait_for_timeout(1200)
    except PWError as e:
        log.warning("搜索输入失败：%s", e)
        return "not_found"

    # 优先精确匹配搜索结果（备注或昵称都可能出现在结果文本里）
    for sel in SEARCH_RESULT:
        for text_key in dict.fromkeys((key, name)):
            loc = page.locator(sel).filter(has_text=text_key)
            if await loc.count():
                try:
                    await loc.first.click(timeout=4000)
                    await page.wait_for_timeout(1200)
                    return "ok"
                except PWError:
                    continue

    # 兜底：直接点会话列表里带该名字的行
    for sel in CONVERSATION_ROW:
        loc = page.locator(sel).filter(has_text=name)
        if await loc.count():
            try:
                await loc.first.click(timeout=4000)
                await page.wait_for_timeout(1200)
                return "ok"
            except PWError:
                continue
    log.warning("未找到好友「%s」的会话入口", name)
    return "not_found"


_JS_HEADER_NAMES = r"""
() => {
  const sels = ['[class*="chat-header"]', '[class*="conversation-header"]',
                '[class*="header"] [class*="name"]', '[class*="chat"] [class*="title"]'];
  const names = [];
  for (const s of sels) {
    document.querySelectorAll(s).forEach(el => {
      if (el.offsetParent && el.innerText) names.push(el.innerText.trim());
    });
  }
  return [...new Set(names)].slice(0, 10);
}
"""


async def verify_chat_target(page: Page, name: str) -> bool:
    """防错发校验：当前打开会话的标题区应包含目标好友昵称。

    找不到任何标题元素（选择器失效/页面形态不同）时放行并告警（fail-open），
    找到了但不包含目标名则拦截（fail-closed）——宁可失败也不能发错人。
    """
    try:
        headers = await page.evaluate(_JS_HEADER_NAMES)
    except PWError:
        return True  # 页面异常时不做拦截，交给送达确认兜底
    if not headers:
        log.warning("会话标题校验：未找到标题元素（可能改版），放行并记录")
        return True
    if any(name in h for h in headers):
        return True
    log.warning("会话标题校验拦截：目标「%s」，当前标题 %s", name, headers[:3])
    return False


async def text_in_bubbles(page: Page, text: str) -> bool:
    try:
        data = await page.evaluate(JS_TEXT_IN_BUBBLES, text)
        return bool(data and data.get("found"))
    except PWError:
        return False


class _ImListener:
    """记录发送期间 IM 接口的响应状态，作为送达的辅证据。"""

    def __init__(self, page: Page):
        self.page = page
        self.hits: list[tuple[int, str]] = []
        self._handler = self._on_response

    def _on_response(self, resp) -> None:
        url = resp.url
        if "/im/" in url and any(k in url for k in ("send", "msg", "message")):
            self.hits.append((resp.status, url[:200]))

    def __enter__(self):
        self.page.on("response", self._handler)
        return self

    def __exit__(self, *exc):
        try:
            self.page.remove_listener("response", self._handler)
        except Exception:
            pass

    def has_ok(self) -> bool:
        return any(200 <= s < 400 for s, _ in self.hits)


async def send_text(page: Page, text: str) -> bool:
    """把文本键入输入框并回车发送。"""
    inp = await _first_of(page, MESSAGE_INPUT, timeout_ms=8000)
    if inp is None:
        log.warning("找不到消息输入框")
        return False
    try:
        await inp.click()
        await page.wait_for_timeout(300)
        await inp.press_sequentially(text, delay=random.randint(15, 45))
        await page.wait_for_timeout(random.randint(200, 600))  # 拟人停顿
        await page.keyboard.press("Enter")
        return True
    except PWError as e:
        log.warning("输入/发送失败：%s", e)
        return False


async def send_image(page: Page, image_path: str) -> bool:
    """通过文件输入发送本地图片（PNG/JPG/GIF/WebP）。实验性，依赖上传控件存在。"""
    for sel in FILE_INPUT:
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.set_input_files(image_path)
                await page.wait_for_timeout(1800)
                # 部分版本上传后需点「发送」确认
                btn = await _first_of(page, SEND_BUTTON_HINT, timeout_ms=4000)
                if btn is not None:
                    await btn.click(timeout=3000)
                return True
        except (PWError, FileNotFoundError) as e:
            log.debug("图片上传失败（%s）：%s", sel, e)
    log.warning("找不到图片上传入口")
    return False


# 上传后可能出现的确认按钮（发送/确定）
SEND_BUTTON_HINT = ['button:has-text("发送")', '[class*="send"] button']


async def wait_verification(page: Page, text: str, listener: _ImListener,
                             timeout_s: float = 6.0) -> str:
    """轮询确认送达，返回证据类型（dom/network/""）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await text_in_bubbles(page, text):
            return "dom"
        if listener.has_ok():
            return "network"
        await page.wait_for_timeout(500)
    return ""


async def send_to_friend(page: Page, name: str, text: str, attempts: int = 2,
                         allow_duplicate: bool = True,
                         allow_first_message: bool = False,
                         search_key: str | None = None) -> SendOutcome:
    """给单个好友发一条消息/图片并确认送达。返回 SendOutcome。

    text 以 "image:" 前缀表示发送本地图片（实验性），验证只依赖网络层证据。
    """
    started = time.monotonic()
    is_image = text.startswith(IMAGE_PREFIX)
    image_path = text[len(IMAGE_PREFIX):].strip() if is_image else ""
    if is_image and not Path(image_path).is_file():
        return SendOutcome(name, False, f"图片不存在：{image_path}", 0, text, "")

    with _ImListener(page) as listener:
        seen_before = (not is_image) and await text_in_bubbles(page, text)
        for attempt in range(1, attempts + 1):
            opened = await open_friend(page, name,
                                       allow_first_message=allow_first_message,
                                       search_key=search_key)
            if opened != "ok":
                if opened == "no_conversation":
                    return SendOutcome(name, True, "无会话记录，按配置跳过",
                                       _ms(started), text, "skipped")
                await page.wait_for_timeout(1000 * attempt)
                continue
            # 防错发：确认当前会话确实是目标好友
            if not await verify_chat_target(page, name):
                return SendOutcome(name, False, "防错发校验拦截：会话标题与目标不符",
                                   _ms(started), text, "blocked")
            # 上一次尝试可能已发出但确认失败：气泡里已有 → 直接判定成功，避免重发
            if attempt > 1 and not is_image and await text_in_bubbles(page, text):
                return SendOutcome(name, True, "前次已送达", _ms(started), text, "dom")
            if seen_before and not allow_duplicate:
                return SendOutcome(name, True, "消息已存在，跳过", _ms(started), text, "dedup")

            if is_image:
                sent = await send_image(page, image_path)
                if not sent:
                    continue
                # 图片没有文本气泡可查，只认网络层成功响应
                if await _wait_network_ok(listener, timeout_s=8.0):
                    return SendOutcome(name, True, "图片发送成功", _ms(started), text, "network")
            else:
                if not await send_text(page, text):
                    continue
                evidence = await wait_verification(page, text, listener)
                if evidence:
                    return SendOutcome(name, True, "发送成功", _ms(started), text, evidence)
            log.warning("「%s」第 %d 次发送未能确认送达", name, attempt)
            await page.wait_for_timeout(1500)

        detail = "发送未确认（DOM 与网络层均无证据）"
        # 最后再查一次气泡，网络慢时轮询窗口可能不够
        if not is_image and await text_in_bubbles(page, text):
            return SendOutcome(name, True, "延迟确认送达", _ms(started), text, "dom")
        return SendOutcome(name, False, detail, _ms(started), text, "")


async def _wait_network_ok(listener: _ImListener, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if listener.has_ok():
            return True
        await listener.page.wait_for_timeout(500)
    return False


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
