"""实验性 API 探针（默认不参与发送流程）。

抖音网页 IM 走 protobuf + 私有签名，直接 HTTP 调用改版风险高。
本模块只做两件事：
1. 从页面性能条目里发现当前版本实际使用的 IM 接口（诊断/逆向参考资料）；
2. 在页面上下文内 fetch 接口（自动带上 cookie/msToken），供后续演进验证。
发送主流程仍由 dom.py 驱动，稳定第一。
"""
from __future__ import annotations

import logging

from playwright.async_api import Page

log = logging.getLogger("apidrv")

_JS_PERF_IM = r"""
() => {
  const urls = performance.getEntriesByType('resource')
    .map(e => e.name)
    .filter(u => u.includes('/im/') || u.includes('imapi') || u.includes('/msg/'));
  return [...new Set(urls)].slice(0, 50);
}
"""

# 已知/猜定的发送类接口形态（仅供验证，不保证可用）
CANDIDATE_SEND_PATHS = [
    "/aweme/v1/web/im/send_msg/",
    "/aweme/v1/im/send_msg/",
]


async def discover_im_endpoints(page: Page) -> list[str]:
    """列出当前页面会话期间实际请求过的 IM 接口。"""
    try:
        urls = await page.evaluate(_JS_PERF_IM)
        return list(urls)
    except Exception as e:
        log.debug("IM 接口发现失败：%s", e)
        return []


async def page_fetch(page: Page, url: str, method: str = "GET",
                     body: dict | None = None) -> dict:
    """在页面上下文内发请求：继承站点 cookie，可复用页面自身签名参数。

    返回 {status, body}。仅供实验与诊断。
    """
    js = """
    async ([url, method, body]) => {
      try {
        const opt = { method, credentials: 'include',
                      headers: { 'content-type': 'application/json' } };
        if (body !== null) opt.body = JSON.stringify(body);
        const resp = await fetch(url, opt);
        const text = await resp.text();
        return { status: resp.status, body: text.slice(0, 2000) };
      } catch (e) {
        return { status: 0, body: String(e) };
      }
    }
    """
    return await page.evaluate(js, [url, method, body])
