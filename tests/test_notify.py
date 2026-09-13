"""通知渠道纯函数测试（不发真实请求）。"""
from __future__ import annotations

import base64
import hashlib
import hmac

from spark.notify import dingtalk_sign


def test_dingtalk_sign_matches_reference_impl():
    secret = "SEC7d941f1b7f"
    ts = 1700000000000
    sign = dingtalk_sign(secret, ts)
    # 与钉钉官方文档算法一致：hmac_sha256(secret, f"{ts}\n{secret}") → base64
    expected = base64.b64encode(
        hmac.new(secret.encode(), f"{ts}\n{secret}".encode(), hashlib.sha256).digest()
    ).decode()
    assert sign == expected
