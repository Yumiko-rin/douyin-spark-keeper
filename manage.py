"""CLI 管理工具：不开控制台也能完成所有操作。

用法：
  python manage.py serve                      # 启动服务（同 python app.py）
  python manage.py accounts                   # 查看账号列表与状态
  python manage.py login 大号                 # 出二维码并等待扫码（保存 login_qr.png）
  python manage.py sync 大号                  # 同步好友
  python manage.py send 大号 --dry            # 模拟演练
  python manage.py send 大号 --select 侯,小明  # 临时只发给指定好友
  python manage.py check 大号                 # 登录态巡检
  python manage.py cookies 大号 --file cookie.txt   # 导入 Cookie 免扫码
  python manage.py cookies 大号 --file state.json   # 导入 storage_state（换机/Actions）
  python manage.py notify-test                # 测试推送渠道
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from pathlib import Path

from douyin.models import NotLoggedIn
from spark import __version__
from spark.clock import Clock
from spark.config import AccountConfig, GlobalConfig, load_env
from spark.logger import setup_logging
from spark.scheduler import SparkScheduler
from spark.store import Store


def build() -> tuple[GlobalConfig, Store, Clock, SparkScheduler]:
    load_env(Path(__file__).parent / ".env")
    gcfg = GlobalConfig.from_env()
    setup_logging(gcfg.data_dir, "INFO")
    store = Store(gcfg.db_path())
    clock = Clock(gcfg.tz)
    from douyin.session import BrowserPool

    pool = BrowserPool(gcfg.accounts_dir(), headless=True,
                       max_contexts=gcfg.max_contexts, tz=gcfg.tz, proxy=gcfg.proxy)
    return gcfg, store, clock, SparkScheduler(gcfg, store, clock, pool)


def cmd_accounts(gcfg, store, clock, sched) -> None:
    names = sched.account_names()
    if not names:
        print("（还没有账号，先用控制台或 login 命令添加）")
        return
    for name in names:
        cfg = AccountConfig.load(gcfg.account_config_path(name))
        friends = store.list_friends(name)
        selected = sum(1 for f in friends if f["selected"])
        s = store.get_day_summary(name)
        today = f"ok={s['ok']}/fail={s['failed']}" if s else "未发送"
        print(f"- {name}: 每日 {cfg.send_time} 勾选 {selected}/{len(friends)} 今日[{today}] "
              f"启用={cfg.enabled}")


async def cmd_login(gcfg, store, clock, sched, name: str) -> None:
    d = gcfg.account_dir(name)
    if not (d / "config.json").is_file():
        d.mkdir(parents=True, exist_ok=True)
        AccountConfig().save(d / "config.json")
        sched.add_account(name)
    print(f"正在为账号「{name}」获取二维码…")
    r = await sched.login.start(name, headless=True)
    if r.get("logged_in"):
        print("该账号已登录，无需重新扫码。")
        return
    qr = r.get("qr_base64")
    if not qr:
        print("二维码获取失败，请改用 Web 控制台重试（python app.py）。")
        return
    qr_path = d / "login_qr.png"
    qr_path.write_bytes(base64.b64decode(qr))
    print(f"二维码已保存：{qr_path}")
    print("请打开该图片，用抖音 App「扫一扫」完成登录，本命令将自动等待…")
    r = await sched.login.wait_login(name, timeout_s=240)
    print(r["tip"])


async def cmd_sync(gcfg, store, clock, sched, name: str) -> None:
    r = await sched.run_sync(name)
    print(f"发现 {r['found']} 人，写入 {r['upserted']}，移除失效 {r['removed']}")
    for f in store.list_friends(name):
        mark = "✓" if f["selected"] else " "
        print(f" {mark} {f['name']}  🔥{f['streak_days']}")


async def cmd_send(gcfg, store, clock, sched, name: str, dry: bool, only: str) -> None:
    if only:
        names = [n.strip() for n in only.split(",") if n.strip()]
        store.set_selected(name, names, True)
        others = [f["name"] for f in store.list_friends(name) if f["name"] not in names]
        store.set_selected(name, others, False)
        print(f"临时只发送给：{'、'.join(names)}")
    try:
        r = await sched.run_send_manual(name, dry=dry, force=True)
    except NotLoggedIn as e:
        print(f"登录态失效：{e}")
        sys.exit(2)
    print(f"结果：成功 {r.get('ok_count', 0)}/{r.get('total', 0)}"
          + ("（演练）" if dry else ""))
    for n, d in (r.get("details") or {}).items():
        print(f"  {'✓' if d['ok'] else '✗'} {n}: {d['detail']} [{d['verified_by']}]")


async def cmd_check(gcfg, store, clock, sched, name: str) -> None:
    h = await sched.pool.health(name)
    print(("✓ " if h["ok"] else "✗ ") + h["detail"] + f"（{h['latency_ms']}ms）")
    if not h["ok"]:
        sys.exit(2)


async def cmd_cookies(gcfg, store, clock, sched, name: str, file: str | None) -> None:
    """导入 Cookie（文本或 storage_state.json）。"""
    raw = Path(file).read_text(encoding="utf-8") if file else sys.stdin.read().strip()
    if not raw:
        print("Cookie 内容为空")
        sys.exit(1)
    _ensure_account(gcfg, sched, name)
    if file and file.endswith(".json"):
        r = await sched.login.restore_state(name, file)
    else:
        r = await sched.login.import_cookies(name, raw)
    print(("✓ " if r["logged_in"] else "✗ ") + r["tip"] + f"（导入 {r['imported']} 条）")
    if not r["logged_in"]:
        sys.exit(2)


def _ensure_account(gcfg, sched, name: str) -> None:
    d = gcfg.account_dir(name)
    if not (d / "config.json").is_file():
        d.mkdir(parents=True, exist_ok=True)
        AccountConfig().save(d / "config.json")
        sched.add_account(name)


def cmd_init(gcfg, store, clock, sched, name: str, config_path: str | None) -> None:
    """初始化账号（可选从 JSON 文件加载配置，供 GitHub Actions 等无状态环境用）。"""
    _ensure_account(gcfg, sched, name)
    if config_path:
        import json as _json

        data = _json.loads(Path(config_path).read_text(encoding="utf-8"))
        cfg = AccountConfig.from_json(data)
        cfg.save(gcfg.account_config_path(name))
    loaded = AccountConfig.load(gcfg.account_config_path(name))
    print(f"账号「{name}」已就绪：发送 {loaded.send_time}，"
          f"白名单 {len(loaded.friend_allowlist)} 人，启用={loaded.enabled}")


def cmd_notify_test(gcfg, store, clock, sched) -> None:
    from spark.notify import notify_all

    cfg = gcfg.load_notify()
    channels = cfg.enabled_channels()
    if not channels:
        print("未配置任何通知渠道（data/global.json 或控制台「通知」页）。")
        sys.exit(1)
    results = asyncio.run(notify_all(cfg, "[火花管家] 测试推送", "CLI 测试消息。"))
    for r in results:
        print(("✓ " if r["ok"] else "✗ ") + r["channel"] + (f"：{r['error']}" if r.get("error") else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="火花管家 Pro CLI")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("accounts")
    p_init = sub.add_parser("init", help="初始化账号（可从 JSON 文件加载配置）")
    p_init.add_argument("account")
    p_init.add_argument("--config", default=None, help="配置 JSON 文件路径")
    p_login = sub.add_parser("login"); p_login.add_argument("account")
    p_sync = sub.add_parser("sync"); p_sync.add_argument("account")
    p_send = sub.add_parser("send"); p_send.add_argument("account")
    p_send.add_argument("--dry", action="store_true", help="模拟演练，不真实发送")
    p_send.add_argument("--select", default="", help="临时只发给这些好友（逗号分隔）")
    p_check = sub.add_parser("check"); p_check.add_argument("account")
    p_ck = sub.add_parser("cookies", help="导入 Cookie 免扫码（文本串或 storage_state.json）")
    p_ck.add_argument("account")
    p_ck.add_argument("--file", default=None, help="Cookie 文件路径；省略则读 stdin")
    sub.add_parser("notify-test")
    args = parser.parse_args()

    gcfg, store, clock, sched = build()

    async def runner():
        if args.cmd == "serve":
            import uvicorn

            from app import app as fastapi_app

            await clock.sync(gcfg.ntp_servers)
            sched.start()
            await uvicorn.Server(uvicorn.Config(
                fastapi_app, host=gcfg.host, port=gcfg.port, log_level="warning")).serve()
        elif args.cmd == "accounts":
            cmd_accounts(gcfg, store, clock, sched)
        elif args.cmd == "init":
            cmd_init(gcfg, store, clock, sched, args.account, args.config)
        elif args.cmd == "login":
            await cmd_login(gcfg, store, clock, sched, args.account)
        elif args.cmd == "sync":
            await cmd_sync(gcfg, store, clock, sched, args.account)
        elif args.cmd == "send":
            await cmd_send(gcfg, store, clock, sched, args.account, args.dry, args.select)
        elif args.cmd == "check":
            await cmd_check(gcfg, store, clock, sched, args.account)
        elif args.cmd == "cookies":
            await cmd_cookies(gcfg, store, clock, sched, args.account, args.file)
        elif args.cmd == "notify-test":
            cmd_notify_test(gcfg, store, clock, sched)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
