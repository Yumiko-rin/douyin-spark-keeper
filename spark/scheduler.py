"""调度引擎：每账号一个常驻协程，按「里程碑」推进每一天。

里程碑（按当日时刻排序）：
  health  —— 登录态巡检 + 好友同步 + 昨日漏发提醒
  prewarm —— 发送前 N 分钟预热浏览器（到点后零冷启动）
  send    —— 准时发送；整轮失败则在保底截止前按间隔重试

设计要点：
- 抖动用 账号+日期 做随机种子：重算计划不会让发送时刻漂移；
- 等待循环每 ≤15 秒醒来检查一次配置变更，最后一次精确睡到毫秒级；
- 手动触发（Web/CLI）与定时任务共用一套发送函数，用账号级锁防并发；
- 任何一步的异常都只影响当天该步骤，绝不拖垮整个调度循环。
"""
from __future__ import annotations

import asyncio
import logging
import random
import traceback
from datetime import datetime, timedelta

import httpx

from douyin import dom
from douyin.login import LoginFlow
from douyin.models import NotLoggedIn, SendOutcome
from douyin.session import BrowserPool
from spark import plan as planf
from spark import templates
from spark.clock import Clock
from spark.config import AccountConfig, GlobalConfig
from spark.notify import notify_all
from spark.store import Store

log = logging.getLogger("scheduler")


class SparkScheduler:
    def __init__(self, gcfg: GlobalConfig, store: Store, clock: Clock, pool: BrowserPool):
        self.gcfg = gcfg
        self.store = store
        self.clock = clock
        self.pool = pool
        self.login = LoginFlow(pool)
        self._tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._done: set[tuple[str, str, str]] = set()      # (account, date, kind)
        self._notified: set[tuple[str, str, str]] = set()  # 当日同类通知只发一次
        self._status: dict[str, dict] = {}
        self._stop = asyncio.Event()
        self._bg: list[asyncio.Task] = []

    # ================= 生命周期 =================
    def account_names(self) -> list[str]:
        root = self.gcfg.accounts_dir()
        if not root.is_dir():
            return []
        return sorted(p.parent.name for p in root.glob("*/config.json"))

    def _lock_for(self, account: str) -> asyncio.Lock:
        return self._locks.setdefault(account, asyncio.Lock())

    def start(self) -> None:
        self._stop.clear()
        for name in self.account_names():
            self.add_account(name)
        self._bg.append(asyncio.create_task(self._clock_loop(), name="clock-sync"))
        log.info("调度器已启动，账号：%s", self.account_names() or "（无）")

    async def stop(self) -> None:
        self._stop.set()
        for t in self._bg + list(self._tasks.values()):
            t.cancel()
        await asyncio.gather(*self._bg, *self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._bg.clear()
        await self.pool.stop()

    def add_account(self, name: str) -> None:
        if name in self._tasks and not self._tasks[name].done():
            return
        self._tasks[name] = asyncio.create_task(self._loop(name), name=f"loop:{name}")

    def remove_account(self, name: str) -> None:
        t = self._tasks.pop(name, None)
        if t:
            t.cancel()
        self._status.pop(name, None)

    def status(self) -> dict[str, dict]:
        out = {}
        for name in self.account_names():
            st = dict(self._status.get(name, {}))
            st["config"] = self._load_cfg(name).to_json()
            out[name] = st
        return out

    def _load_cfg(self, account: str) -> AccountConfig:
        try:
            return AccountConfig.load(self.gcfg.account_config_path(account))
        except (ValueError, OSError) as e:
            log.error("账号 %s 配置损坏，使用默认配置：%s", account, e)
            return AccountConfig()

    # ================= 账号主循环 =================
    async def _loop(self, account: str) -> None:
        log.info("账号 %s 调度循环启动", account)
        while not self._stop.is_set():
            try:
                await self._cycle(account)
            except asyncio.CancelledError:
                raise
            except NotLoggedIn as e:
                await self._notify_once(account, "login_expired",
                                        f"[火花管家] {account} 登录态失效",
                                        f"{e}\n请打开控制台重新扫码登录。", "critical")
                await self._nap(account, 1800)
            except Exception as e:
                log.error("账号 %s 循环异常：%s\n%s", account, e, traceback.format_exc())
                await self._nap(account, 300)

    async def _nap(self, account: str, seconds: float) -> None:
        self._set_status(account, note=f"异常退避 {int(seconds)}s")
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _cycle(self, account: str) -> None:
        cfg = self._load_cfg(account)
        now = self.clock.now()
        self._set_status(account, now=now.isoformat(timespec="seconds"))

        if not cfg.enabled:
            self._set_status(account, next_action="已停用")
            if await self._sleep_or_stop(60):
                return
            return

        # 启动补偿：已过发送时刻但未到保底截止，且今天没发成 → 立即补发
        if (planf.should_catch_up(now, cfg.send_time, cfg.deadline_time)
                and not self._send_done(account, now)):
            log.warning("账号 %s 触发启动补偿：已过发送时刻，立即补发", account)
            self._set_status(account, next_action="启动补偿发送")
            await self._guarded_send(account, cfg, manual=False)
            self._done.add((account, now.date().isoformat(), "send"))
            return

        # 计算下一个里程碑（发送时刻跨天自动顺延；当日已执行的不再重复）
        self._prune_done(now)
        milestones = self._milestones(account, cfg, now)
        kind, dt = min(milestones, key=lambda kv: kv[1])
        if dt > now:
            self._set_status(account, next_action=f"{kind} @ {dt.isoformat(timespec='minutes')}",
                             next_send=self._next_send_iso(milestones))
            if await self._wait_until(dt):
                return
            return  # 醒来后重算（配置可能已改）
        # dt <= now：执行该里程碑
        self._done.add((account, dt.date().isoformat(), kind))
        self._set_status(account, next_action=f"执行 {kind}")
        if kind == "health":
            await self._run_health(account, cfg)
        elif kind == "prewarm":
            await self._run_prewarm(account, cfg)
        elif kind == "send":
            await self._guarded_send(account, cfg, manual=False)

    def _prune_done(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=2)).date().isoformat()
        self._done = {e for e in self._done if e[1] >= cutoff}

    def _milestones(self, account: str, cfg: AccountConfig,
                    now: datetime) -> list[tuple[str, datetime]]:
        send_dt = self._send_dt(account, cfg, now)
        out: list[tuple[str, datetime]] = [("send", send_dt)]
        if cfg.prewarm_minutes > 0:
            out.append(("prewarm", send_dt - timedelta(minutes=cfg.prewarm_minutes)))
        out.append(("health", planf.today_at(cfg.health_check_time, now)))
        out = [(k, dt) for k, dt in out if (account, dt.date().isoformat(), k) not in self._done]
        return out or [("health", planf.today_at(cfg.health_check_time, now) + timedelta(days=1))]

    def _send_dt(self, account: str, cfg: AccountConfig, now: datetime) -> datetime:
        """下一次发送时刻：抖动按「账号+日期」定种子，同日重算结果一致。

        send_window 非空时用窗口随机模式，否则用 send_time + jitter。
        """
        for day in (now, now + timedelta(days=1)):
            rng = random.Random(f"{account}|{day.date().isoformat()}")
            if cfg.send_window:
                dt_ = planf.window_send_time(day, cfg.send_window, rng)
            else:
                dt_ = planf.next_send_time(day, cfg.send_time, cfg.jitter_minutes,
                                           rng, grace_minutes=0)
            if dt_ > now:
                return dt_
        return planf.today_at(cfg.send_time, now + timedelta(days=1))

    def _next_send_iso(self, milestones: list[tuple[str, datetime]]) -> str | None:
        sends = [dt for k, dt in milestones if k == "send"]
        return sends[0].isoformat(timespec="minutes") if sends else None

    def _send_done(self, account: str, now: datetime) -> bool:
        if (account, now.date().isoformat(), "send") in self._done:
            return True
        s = self.store.get_day_summary(account, now.date().isoformat())
        return bool(s and s["ok"])  # dry-run 不算完成

    async def _wait_until(self, dt: datetime) -> bool:
        """睡到 dt；返回 True 表示收到停止信号。每 15s 醒来重查，最后一段精确睡。"""
        while True:
            remaining = (dt - self.clock.now()).total_seconds()
            if remaining <= 0:
                return False
            if remaining > 15:
                if await self._sleep_or_stop(15):
                    return True
            else:
                if await self._sleep_or_stop(remaining):
                    return True
                # 贴近时刻时再细睡一次，消化最后几十毫秒
                if (dt - self.clock.now()).total_seconds() > 0.02:
                    await asyncio.sleep(max(0.0, (dt - self.clock.now()).total_seconds()))
                return False

    async def _sleep_or_stop(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, seconds))
            return True
        except TimeoutError:
            return False

    def _set_status(self, account: str, **fields) -> None:
        st = self._status.setdefault(account, {})
        st.update(fields)
        st["updated_at"] = self.clock.now().isoformat(timespec="seconds")

    # ================= 里程碑实现 =================
    async def _run_health(self, account: str, cfg: AccountConfig) -> None:
        log.info("账号 %s 巡检开始", account)
        self._set_status(account, last_action="巡检")
        # 顺带校时（每天一次足够）
        await asyncio.get_running_loop().run_in_executor(
            None, self.clock.sync, self.gcfg.ntp_servers)

        if cfg.auto_sync_friends:
            try:
                async with self._lock_for(account):
                    r = await self.run_sync(account)
                log.info("账号 %s 好友同步：%s", account, r)
            except NotLoggedIn:
                await self._notify_login_expired(account)
                return
            except Exception as e:
                log.warning("账号 %s 好友同步失败：%s", account, e)

        try:
            async with self._lock_for(account):
                h = await self.pool.health(account)
            self._set_status(account, login_ok=h["ok"])
            if not h["ok"]:
                await self._notify_login_expired(account)
            else:
                log.info("账号 %s 登录态正常（%dms）", account, h["latency_ms"])
        except Exception as e:
            log.warning("账号 %s 巡检异常：%s", account, e)

        await self._missed_yesterday_check(account)

    async def _missed_yesterday_check(self, account: str) -> None:
        yesterday = (self.clock.now() - timedelta(days=1)).date().isoformat()
        s = self.store.get_day_summary(account, yesterday)
        has_friends = self.store.list_friends(account)
        if not has_friends:
            return  # 还没配置过好友，不打扰
        if s is None or (not s["ok"] and not s["dry"]):
            await self._notify_once(
                account, f"missed:{yesterday}",
                f"[火花管家] {account} 昨日未发送成功",
                f"{yesterday} 没有成功发送记录，火花可能已中断。\n请检查账号登录态与好友勾选。",
                "critical")

    async def _run_prewarm(self, account: str, cfg: AccountConfig) -> None:
        log.info("账号 %s 预热浏览器", account)
        self._set_status(account, last_action="预热")
        try:
            _ctx, page = await self.pool.chat_page(account, headless=cfg.headless)
            self._set_status(account, login_ok=True)
            log.info("账号 %s 预热完成，聊天页已就绪：%s", account, page.url[:80])
        except NotLoggedIn:
            await self._notify_login_expired(account)
        except Exception as e:
            log.warning("账号 %s 预热失败（发送时会重试）：%s", account, e)

    async def _notify_login_expired(self, account: str) -> None:
        await self._notify_once(
            account, "login_expired",
            f"[火花管家] {account} 登录态失效",
            "无法确认登录状态，火花即将不保！\n请尽快打开控制台重新扫码登录。",
            "critical")

    # ---------- 发送 ----------
    async def _guarded_send(self, account: str, cfg: AccountConfig, manual: bool) -> dict:
        try:
            async with self._lock_for(account):
                return await self.run_send(account, cfg, manual=manual)
        except NotLoggedIn as e:
            log.error("账号 %s 发送中止：%s", account, e)
            await self._notify_login_expired(account)
            self.store.save_day_summary(account, total=0, ok=0, failed=0,
                                        note="登录态失效")
            return {"ok": False, "error": str(e)}
        except Exception as e:
            log.error("账号 %s 发送异常：%s\n%s", account, e, traceback.format_exc())
            return {"ok": False, "error": str(e)}

    async def run_send_manual(self, account: str, dry: bool = False,
                              force: bool = False) -> dict:
        """Web/CLI 手动触发入口（NotLoggedIn 原样抛出，由调用方决定提示方式）。"""
        cfg = self._load_cfg(account)
        async with self._lock_for(account):
            return await self.run_send(account, cfg, dry=dry, manual=True, force=force)

    async def run_send(self, account: str, cfg: AccountConfig, dry: bool = False,
                       manual: bool = False, force: bool = False) -> dict:
        """发送主流程：可被定时任务、Web、CLI 共同调用（外部需持有账号锁）。"""
        summary = self.store.get_day_summary(account)
        # 仅当今天已有成功发送才跳过；dry-run 与失败结果都不阻塞正式发送
        if summary and not force and summary["ok"]:
            note = "今日已发送（如需重复发送请勾选强制）" if manual else "今日已发送，跳过"
            log.info("账号 %s %s", account, note)
            return {"ok": True, "note": note, **summary}

        friends = self._resolve_friends(account, cfg)
        if not friends:
            self.store.save_day_summary(account, total=0, ok=0, failed=0, note="无勾选好友")
            await self._notify(account, cfg,
                               f"[火花管家] {account} 没有可发送的好友",
                               "请到控制台同步好友并勾选要保火花的人，"
                               "或在配置里填写 friend_allowlist。", "critical")
            return {"ok": False, "error": "没有勾选的好友"}

        _ctx, page = await self.pool.chat_page(account, headless=cfg.headless)
        trace_path = None
        if cfg.debug_trace and not dry:
            trace_path = self.gcfg.data_dir / "traces" / \
                f"{account}-{self.clock.now():%Y%m%d}.zip"
            try:
                await _ctx.tracing.start(screenshots=True, snapshots=True)
            except Exception as e:
                log.debug("开启 Trace 失败：%s", e)
                trace_path = None
        rng = random.Random()
        lo, hi = cfg.gap_range()
        deadline = planf.today_at(cfg.deadline_time, self.clock.now())
        pending = [f["name"] for f in friends]
        results: dict[str, SendOutcome] = {}
        attempt = 0
        dry_count = 0
        risk_halt: str | None = None
        msg_ctx = await self._message_ctx(cfg)

        while pending:
            attempt += 1
            still_pending = []
            for i, name in enumerate(pending):
                pcfg = cfg.per_friend.get(name, {})
                msg, _template = templates.build_message(
                    cfg.messages, name, self._streak_of(friends, name),
                    self.clock.now(), rng, extra_templates=pcfg.get("messages"),
                    ctx_extra=msg_ctx)
                if dry:
                    outcome = SendOutcome(name, True, "演练：未真实发送", 0, msg, "dry")
                    dry_count += 1
                else:
                    outcome = await dom.send_to_friend(
                        page, name, msg, attempts=2,
                        allow_first_message=cfg.allow_first_message,
                        search_key=pcfg.get("search_name"))
                self.store.record_send(account, name, "dry" if dry else
                                       ("ok" if outcome.ok else "fail"),
                                       detail=outcome.detail, latency_ms=outcome.latency_ms,
                                       message=msg)
                results[name] = outcome
                log.info("账号 %s → %s：%s（%s，%dms）", account, name,
                         "✓ " + outcome.detail if outcome.ok else "✗ " + outcome.detail,
                         outcome.verified_by, outcome.latency_ms)
                if not outcome.ok:
                    still_pending.append(name)
                    if cfg.debug_screenshot:
                        await dom.snapshot(page, self.gcfg.data_dir / "screenshots",
                                           f"{account}-{name}")
                    # 限流熔断：出现风控提示立即停止整轮，绝不盲目重试
                    risk = await dom.detect_risk_control(page)
                    if risk:
                        risk_halt = risk
                        break
                if i < len(pending) - 1 and not dry:
                    await asyncio.sleep(rng.randint(lo, hi))
            if risk_halt:
                for name in pending:
                    if name not in results:
                        outcome = SendOutcome(name, False, f"风控熔断（{risk_halt}），本轮终止",
                                              0, "", "")
                        results[name] = outcome
                        self.store.record_send(account, name, "fail",
                                               detail=outcome.detail)
                break
            pending = still_pending
            if not pending or dry or manual:
                break
            now = self.clock.now()
            if now >= deadline or attempt >= cfg.max_attempts:
                break
            wait_s = min(cfg.retry_interval_minutes * 60,
                         (deadline - now).total_seconds())
            log.warning("账号 %s 第 %d 轮结束，%d 人失败，%.0f 分钟后重试（截止 %s）",
                        account, attempt, len(pending), wait_s / 60, deadline)
            if await self._sleep_or_stop(wait_s):
                break
            # 长等待后页面状态可能变化，重开一次聊天页
            try:
                _ctx, page = await self.pool.chat_page(account, headless=cfg.headless)
            except Exception as e:
                log.warning("重开聊天页失败：%s", e)

        if trace_path is not None:
            try:
                trace_path.parent.mkdir(parents=True, exist_ok=True)
                await _ctx.tracing.stop(path=str(trace_path))
                log.info("已保存 Playwright Trace：%s", trace_path)
            except Exception as e:
                log.debug("保存 Trace 失败：%s", e)

        ok = [n for n, o in results.items() if o.ok]
        failed = [n for n, o in results.items() if not o.ok]
        total = len(results)
        note = f"失败:{','.join(failed)}" if failed else ""
        if risk_halt:
            note = f"风控熔断:{risk_halt};{note}"
        # 演练不计入成功：否则会把今天标记为已发送，挡住真实发送
        self.store.save_day_summary(account, total=total,
                                    ok=0 if dry else len(ok),
                                    failed=0 if dry else len(failed),
                                    dry=dry_count, attempts=attempt, note=note)
        if risk_halt:
            await self._notify(account, cfg,
                               f"[火花管家] {account} 触发风控熔断 🛑",
                               f"页面出现「{risk_halt}」，已立即停止今日发送，不会自动重试。\n"
                               "建议：调大间隔、减少好友数、检查登录环境，明日再战。",
                               "critical")
        elif failed:
            await self._notify(account, cfg,
                               f"[火花管家] {account} 今日发送未完成",
                               f"成功 {len(ok)}/{total}。\n失败名单：{'、'.join(failed)}"
                               + ("" if manual or risk_halt
                                  else f"\n将在截止 {cfg.deadline_time} 前自动重试。"),
                               "critical")
        elif cfg.notify_on_success:
            await self._notify(account, cfg,
                               f"[火花管家] {account} 今日火花已续上 ✅",
                               f"成功 {len(ok)}/{total}"
                               + ("（演练）" if dry else f"：{'、'.join(ok)}"), "info")
        self._set_status(account, last_action="发送完成",
                         last_result={"total": total, "ok": len(ok),
                                      "failed": len(failed), "dry": dry_count})
        return {"ok": not failed, "total": total, "ok_count": len(ok),
                "failed": failed, "attempts": attempt, "dry": dry_count,
                "risk_halt": risk_halt,
                "details": {n: {"ok": o.ok, "detail": o.detail,
                                "verified_by": o.verified_by} for n, o in results.items()}}

    def _resolve_friends(self, account: str, cfg: AccountConfig) -> list[dict]:
        """发送名单：优先 friend_allowlist（无 DB 场景），否则取勾选的好友。"""
        if cfg.friend_allowlist:
            return [{"name": n, "streak_days": 0}
                    for n in cfg.friend_allowlist[: cfg.max_friends_per_run]
                    if cfg.per_friend.get(n, {}).get("enabled", True)]
        friends = [f for f in self.store.selected_friends(account)
                   if cfg.per_friend.get(f["name"], {}).get("enabled", True)]
        return friends[: cfg.max_friends_per_run]

    async def _message_ctx(self, cfg: AccountConfig) -> dict[str, str]:
        """发送前一次性准备动态文案变量：{festival} 节日祝福、{hitokoto} 一言。"""
        ctx: dict[str, str] = {}
        festival = (cfg.festivals or {}).get(self.clock.now().strftime("%m-%d"))
        if festival:
            ctx["festival"] = festival
        all_msgs = list(cfg.messages)
        for pcfg in cfg.per_friend.values():
            all_msgs.extend(pcfg.get("messages") or [])
        if any("{hitokoto}" in m for m in all_msgs):
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get("https://v1.hitokoto.cn/?max_length=40")
                    data = r.json()
                text = str(data.get("hitokoto", "")).strip()
                if text:
                    ctx["hitokoto"] = text
                    log.info("一言已获取：%s", text[:30])
            except Exception as e:
                log.warning("一言获取失败（文案占位符将留空）：%s", e)
        return ctx

    @staticmethod
    def _streak_of(friends: list[dict], name: str) -> int:
        for f in friends:
            if f["name"] == name:
                return f.get("streak_days") or 0
        return 0

    # ---------- 同步 ----------
    async def run_sync(self, account: str) -> dict:
        cfg = self._load_cfg(account)
        _ctx, page = await self.pool.chat_page(account, headless=cfg.headless)
        friends = await dom.sync_friends(page)
        if not friends:
            # 页面可能尚未渲染完成：刷新重试一次，仍为空则不动本地数据
            log.warning("账号 %s 首次同步 0 行，刷新页面重试", account)
            try:
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)
            except Exception as e:
                log.debug("刷新聊天页失败：%s", e)
            friends = await dom.sync_friends(page)
        rows = [f.to_row() for f in friends]
        upserted = self.store.upsert_friends(account, rows)
        # 同步到 0 行时绝不清空本地好友表（页面异常时不毁数据）
        removed = (self.store.delete_friends_not_seen(account, {f.name for f in friends})
                   if friends else 0)
        if not friends:
            log.warning("账号 %s 同步仍为 0 行，保留本地好友数据不动", account)
        self._set_status(account, last_action="好友同步")
        return {"found": len(friends), "upserted": upserted, "removed": removed}

    # ---------- 巡检 ----------
    async def run_check(self, account: str) -> dict:
        cfg = self._load_cfg(account)
        h = await self.pool.health(account)
        if not h["ok"]:
            await self._notify(account, cfg,
                               f"[火花管家] {account} 登录态检查失败",
                               h["detail"], "critical")
        return h

    # ---------- 通知 ----------
    async def _notify(self, account: str, cfg: AccountConfig,
                      title: str, body: str, level: str) -> None:
        if level == "critical" and not cfg.notify_on_failure:
            return
        if level == "info" and not cfg.notify_on_success:
            return
        await notify_all(self.gcfg.load_notify(), title, body, level)

    async def _notify_once(self, account: str, key: str,
                           title: str, body: str, level: str) -> None:
        dedupe = (account, self.clock.now().date().isoformat(), key)
        if dedupe in self._notified:
            return
        self._notified.add(dedupe)
        cfg = self._load_cfg(account)
        if level == "critical" and not cfg.notify_on_failure:
            return
        await notify_all(self.gcfg.load_notify(), title, body, level)

    # ---------- 校时 ----------
    async def _clock_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self.clock.sync, self.gcfg.ntp_servers)
            except Exception as e:
                log.debug("校时循环异常：%s", e)
            if await self._sleep_or_stop(6 * 3600):
                return
