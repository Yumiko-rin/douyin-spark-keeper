"""选择器注册表：抖音改版时的唯一维护点。

每个目标给一组候选（CSS / 文本 / role），按序尝试，全部失败才算找不到。
新增一种页面形态只需在这里加一个候选，不改业务代码。
"""
from __future__ import annotations

# ---------- 登录 ----------
LOGIN_BUTTON = [
    'button:has-text("扫码登录")',
    '[class*="login"] button:has-text("登录")',
    'button:has-text("登录")',
]
LOGIN_PANEL = [
    '[id*="login"] [class*="panel"]',
    '[class*="login-panel"]',
    '[class*="login"] [class*="qrcode"]',
    '[id*="login-pannel"]',  # 抖音历史拼写 pannel
]
QR_IMAGE = [
    '[class*="qrcode"] img[src^="data:image"]',
    '[id*="login"] img[src^="data:image"]',
    '[class*="login"] img[src^="data:image"]',
    'img[src^="data:image/png"]',
]
QR_CANVAS = [
    '[class*="qrcode"] canvas',
    '[id*="login"] canvas',
    '[class*="login"] canvas',
]
QR_REFRESH = [
    '[class*="qrcode"] [class*="refresh"]',
    '[class*="login"] [class*="refresh"]',
    '[class*="qrcode"] :text("点击刷新")',
    '[class*="login"] :text("刷新")',
]
LOGIN_SUCCESS_MARKERS = [  # 登录后页面出现、未登录没有的元素
    '[data-e2e="avatar"]',
    '[class*="avatar"] img[src*="aweme"]',
]

# ---------- 页面 ----------
CHAT_URL = "https://www.douyin.com/chat"
CHAT_URL_FALLBACK = "https://www.douyin.com/chat?isPopup=1"
HOME_URL = "https://www.douyin.com/"

# ---------- 登录态 ----------
LOGIN_COOKIE_KEYS = ["sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt"]

# ---------- 会话列表 / 好友同步 ----------
SEARCH_BOX = [
    'input[placeholder*="搜索"]',
    '[class*="search"] input',
    'input[type="search"]',
]
CONVERSATION_ROW = [
    '[class*="conversation"]',
    '[class*="session-item"]',
    '[class*="chat-item"]',
    '[class*="message-item"]',
    '[class*="contact-item"]',
]
SEARCH_RESULT = [
    '[class*="search-result"] [class*="item"]',
    '[class*="user-card"]',
    '[class*="search-result"] li',
    '[class*="result"] li',
]

# ---------- 消息输入 / 发送 ----------
MESSAGE_INPUT = [
    '[class*="input"] [contenteditable="true"]',
    'textarea[placeholder*="发消息"]',
    '[contenteditable="true"]',
]
SEND_BUTTON = [
    '[class*="send"] button:has-text("发送")',
    'button:has-text("发送")',
]
OUTGOING_MESSAGE = [  # 已发出的消息气泡（用于送达确认）
    '[class*="message-list"] [class*="content"]',
    '[class*="msg-content"]',
    '[class*="message-item"] [class*="text"]',
    '[class*="bubble"]',
]
CHAT_HEADER = [  # 当前打开会话的标题/对方昵称区域（防错发校验）
    '[class*="chat-header"]',
    '[class*="conversation-header"]',
    '[class*="header"] [class*="name"]',
    '[class*="chat"] [class*="title"]',
]
FILE_INPUT = [  # 聊天输入区的图片上传入口
    '[class*="input"] input[type="file"]',
    '[class*="chat"] input[type="file"]',
    'input[type="file"]',
]

# 限流/风控信号（出现即熔断，立即停止整轮发送）
RISK_KEYWORDS = ("操作太频繁", "操作频繁", "安全验证", "请稍后再试", "存在异常", "验证码")

# ---------- JS：滑块/安全验证检测（验证码常在跨域 iframe 里，需多信号） ----------
JS_CAPTCHA_DETECT = r"""
() => {
  const hit = (t) => t.includes('请完成下列验证') ||
    (t.includes('拖动') && (t.includes('滑块') || t.includes('拼图') || t.includes('按住')));
  const main = document.body ? document.body.innerText : '';
  if (hit(main)) return 'text';
  if (document.querySelector('[class*="captcha" i], [id*="captcha" i]')) return 'element';
  for (const f of document.querySelectorAll('iframe')) {
    if (/captcha|verify|sfec/i.test(f.src || '')) return 'iframe-src';
    try {
      const d = f.contentDocument;
      if (d && d.body && hit(d.body.innerText)) return 'iframe-text';
    } catch (e) { /* cross-origin */ }
  }
  return null;
}
"""

# JS：页面内批量采集会话行（比逐个 locator 快一个数量级） ----------
JS_COLLECT_ROWS = r"""
() => {
  const seen = new Set();
  const rows = [];
  const pushRow = (el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    const text = (el.innerText || '').trim();
    const aria = el.getAttribute('aria-label') || '';
    const title = el.getAttribute('title') || '';
    if (!text && !aria && !title) return;
    rows.push({ text, aria, title });
  };
  const sels = ['[class*="conversation"]', '[class*="session-item"]', '[class*="chat-item"]',
                '[class*="message-item"]', '[class*="contact-item"]'];
  for (const sel of sels) {
    document.querySelectorAll(sel).forEach(pushRow);
  }
  if (rows.length === 0) {
    document.querySelectorAll('li').forEach(pushRow);
  }
  return rows.slice(0, 300);
}
"""

# JS：尝试开启「读屏标签」增强（开启后行元素带 aria-label，如「xxx，火花392天」）
JS_ENABLE_SCREEN_READER = r"""
() => {
  try {
    const nodes = document.querySelectorAll('[role="switch"], [class*="switch"], label, span, div');
    for (const el of nodes) {
      const t = (el.innerText || el.getAttribute('aria-label') || el.title || '').trim();
      if ((t.includes('读屏') || t.includes('无障碍')) && el.offsetWidth > 0) {
        el.click();
        return t;
      }
    }
  } catch (e) { /* best effort */ }
  return null;
}
"""

# JS：送达确认 —— 最近消息气泡中是否已出现指定文本
JS_TEXT_IN_BUBBLES = r"""
(text) => {
  const sels = ['[class*="message-list"]', '[class*="msg-list"]', '[class*="chat"]'];
  let root = null;
  for (const s of sels) {
    root = document.querySelector(s);
    if (root) break;
  }
  root = root || document;
  const nodes = root.querySelectorAll(
    '[class*="content"], [class*="msg-text"], [class*="bubble"], [class*="message-item"]');
  const hits = [];
  for (const n of nodes) {
    const t = (n.innerText || '').trim();
    if (t.includes(text)) hits.push(t);
  }
  return { found: hits.length > 0, count: hits.length };
}
"""
