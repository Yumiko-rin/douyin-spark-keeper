"""Cookie 解析与导入。

支持两种格式（兼容同类项目通用的配置方式，从浏览器 F12 复制即可用）：
1. 浏览器导出的 JSON 数组：[{"name": "...", "value": "...", ...}, ...]
2. 文本串："k1=v1; k2=v2; k3=v3"（每行一条也行）
缺省字段自动补全 domain=.douyin.com, path=/。
"""
from __future__ import annotations

import json

DOUYIN_DOMAIN = ".douyin.com"


def parse_cookies(raw: str) -> list[dict]:
    """把用户粘贴的 Cookie 解析成 Playwright add_cookies 需要的列表。"""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Cookie 内容为空")
    # 先按 JSON 试
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, list):
        return _normalize(data)
    if isinstance(data, dict) and all(
            isinstance(v, str) for v in list(data.values())[:5]):
        # {"name": "value", ...} 形态
        return _normalize([{"name": k, "value": v} for k, v in data.items()])

    # k=v; k2=v2 文本形态（支持换行分隔）
    cookies: list[dict] = []
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if not name:
            continue
        cookies.append({"name": name, "value": value.strip()})
    if not cookies:
        raise ValueError("未能解析出任何 Cookie，请检查格式")
    return _normalize(cookies)


def _normalize(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or "value" not in item:
            continue
        cookie = {
            "name": name,
            "value": str(item["value"]),
            "domain": str(item.get("domain") or DOUYIN_DOMAIN),
            "path": str(item.get("path") or "/"),
        }
        if cookie["domain"] and not cookie["domain"].startswith("."):
            cookie["domain"] = "." + cookie["domain"].lstrip(".")
        for key in ("expires", "httpOnly", "secure", "sameSite"):
            if key in item and item[key] is not None:
                cookie[key] = item[key]
        out.append(cookie)
    if not out:
        raise ValueError("Cookie 中没有有效条目（需含 name 与 value）")
    return out
