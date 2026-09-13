/* 火花管家 Pro 控制台（原生 JS，零依赖） */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

let TOKEN = localStorage.getItem("spark_token") || "";
let STATE = null;           // /api/state 结果缓存
let currentAccount = localStorage.getItem("spark_account") || "";
let friendsCache = [];
let loginPolling = false;

/* ---------------- 基础 ---------------- */
async function api(path, opts = {}) {
  const resp = await fetch("/api" + path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      "X-Auth-Token": TOKEN,
      ...(opts.headers || {}),
    },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (resp.status === 401) { showTokenGate("令牌已失效，请重新输入"); throw new Error("unauthorized"); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail || data);
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return data;
}

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.style.borderColor = isErr ? "var(--err)" : "var(--line)";
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 3200);
}

function showTokenGate(err = "") {
  $("#tokenGate").classList.remove("hidden");
  $("#tokenErr").textContent = err;
}

async function busy(label, fn) {
  const ind = $("#runIndicator");
  ind.textContent = "⏳ " + label;
  $$("main button").forEach((b) => (b.disabled = true));
  try { return await fn(); }
  catch (e) { if (e.message !== "unauthorized") toast(e.message, true); return null; }
  finally {
    $$("main button").forEach((b) => (b.disabled = false));
    ind.textContent = "";
  }
}

/* ---------------- 标签页 ---------------- */
$("#nav").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-tab]");
  if (!btn) return;
  $$("#nav button").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + btn.dataset.tab));
  if (btn.dataset.tab === "friends") loadFriends();
  if (btn.dataset.tab === "schedule") loadConfig();
  if (btn.dataset.tab === "notify") loadNotify();
  if (btn.dataset.tab === "logs") startLogStream();
});

/* ---------------- 令牌 ---------------- */
/* 支持 /?token=xxx 直达：双击启动脚本自动带令牌打开，无需手动输入 */
(() => {
  const urlToken = new URLSearchParams(location.search).get("token");
  if (urlToken) {
    TOKEN = urlToken.trim();
    localStorage.setItem("spark_token", TOKEN);
    history.replaceState(null, "", location.pathname); // 把令牌从地址栏清掉
  }
})();

$("#btnToken").addEventListener("click", async () => {
  TOKEN = $("#tokenInput").value.trim();
  try {
    await api("/state");
    localStorage.setItem("spark_token", TOKEN);
    $("#tokenGate").classList.add("hidden");
    $("#tokenErr").textContent = "";
    boot();
  } catch (e) { showTokenGate("令牌无效或服务不可用"); }
});
$("#tokenInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#btnToken").click(); });

/* ---------------- 状态总览 ---------------- */
async function refreshState() {
  STATE = await api("/state");
  renderAccountSelect();
  renderCards();
  renderClock();
}

function renderClock() {
  const c = STATE.clock || {};
  const drift = Math.abs(c.offset_s || 0);
  $("#clockInfo").textContent =
    `服务器 ${STATE.server_time} ｜ 校时 ${c.source}（偏差 ${c.offset_s}s${drift > 30 ? " ⚠" : ""}）`;
}

function renderAccountSelect() {
  const sel = $("#accountSel");
  const names = Object.keys(STATE.accounts);
  if (!names.includes(currentAccount)) currentAccount = names[0] || "";
  sel.innerHTML = names.map((n) => `<option ${n === currentAccount ? "selected" : ""}>${esc(n)}</option>`).join("");
  sel.style.display = names.length ? "" : "none";
}

$("#accountSel").addEventListener("change", (e) => {
  currentAccount = e.target.value;
  localStorage.setItem("spark_account", currentAccount);
  refreshState();
});

function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function renderCards() {
  const wrap = $("#cards");
  const names = Object.keys(STATE.accounts);
  $("#guideCard").style.display = names.length ? "none" : "";
  wrap.innerHTML = names.map((name) => {
    const a = STATE.accounts[name];
    const cfg = a.config || {};
    const st = a.status || {};
    const today = a.today;
    const loginBadge = st.login_ok === true ? '<span class="badge ok">已登录</span>'
      : st.login_ok === false ? '<span class="badge bad">登录失效</span>'
      : '<span class="badge unknown">登录态未知</span>';
    const enabledBadge = cfg.enabled === false ? '<span class="badge bad">已停用</span>' : "";
    const todayLine = today
      ? `今日：成功 ${today.ok || 0} / 失败 ${today.failed || 0}${today.dry ? "（演练 " + today.dry + "）" : ""}`
      : "今日：尚未发送";
    return `<div class="card">
      <h3>${esc(name)} ${loginBadge} ${enabledBadge}</h3>
      <div class="stat-row">
        <div class="stat"><b>${a.friends_selected || 0}</b><span class="muted">勾选好友 / 共 ${a.friends_total || 0}</span></div>
        <div class="stat"><b>${a.stats ? a.stats.current_streak : 0}</b><span class="muted">连续保住天数</span></div>
      </div>
      <p class="muted">${esc(todayLine)}<br>
      下次发送：${esc(st.next_send || cfg.send_time || "-")}<br>
      ${esc(st.next_action || st.last_action || "")}</p>
    </div>`;
  }).join("");
}

/* ---------------- 账号操作 ---------------- */
$("#btnAddAccount").addEventListener("click", async () => {
  const name = prompt("给这个抖音账号起个管理名（如：大号 / 小号）：");
  if (!name) return;
  await busy("添加账号", async () => {
    await api("/accounts", { method: "POST", body: { name } });
    currentAccount = name;
    localStorage.setItem("spark_account", name);
    await refreshState();
    toast("已添加，请先扫码登录");
  });
});

$("#btnLogin").addEventListener("click", async () => {
  if (!currentAccount) return toast("请先添加账号", true);
  await openLoginModal();
});

async function openLoginModal() {
  $("#loginModal").classList.remove("hidden");
  $("#loginAccount").textContent = currentAccount;
  $("#qrBox").innerHTML = '<span class="muted">正在获取二维码…</span>';
  $("#loginTip").textContent = "请用抖音 App「扫一扫」";
  await busy("获取二维码", async () => {
    const r = await api(`/accounts/${enc(currentAccount)}/login/start`, { method: "POST", body: {} });
    if (r.logged_in) {
      $("#qrBox").innerHTML = '<span style="color:#4ade80;font-size:30px">✓ 已登录</span>';
      $("#loginTip").textContent = "该账号已登录，无需重新扫码";
      setTimeout(closeLoginModal, 1200);
      refreshState();
      return;
    }
    if (r.qr_base64) {
      $("#qrBox").innerHTML = `<img src="data:image/png;base64,${r.qr_base64}" alt="二维码">`;
      pollLogin();
    } else if (r.page_base64) {
      // 抓不到码时展示浏览器里的真实画面（可能是滑块验证/登录框形态变化）
      $("#qrBox").innerHTML = `<img src="data:image/png;base64,${r.page_base64}" style="max-width:100%;max-height:260px;object-fit:contain" alt="页面截图">`;
      $("#loginTip").textContent = r.tip;
    } else {
      $("#qrBox").innerHTML = '<span class="muted">二维码获取失败，请重试</span>';
      $("#loginTip").textContent = r.tip || "";
    }
  });
}

function enc(s) { return encodeURIComponent(s); }

async function pollLogin() {
  if (loginPolling) return;
  loginPolling = true;
  try {
    const r = await api(`/accounts/${enc(currentAccount)}/login/wait?timeout=150`);
    loginPolling = false;
    if (r.logged_in) {
      $("#qrBox").innerHTML = '<span style="color:#4ade80;font-size:30px">✓ 登录成功</span>';
      $("#loginTip").textContent = "登录态已保存，可以关闭窗口";
      toast("扫码登录成功 🎉");
      refreshState();
      setTimeout(closeLoginModal, 1500);
    } else {
      $("#loginTip").textContent = r.tip || "等待超时，请重新出码";
    }
  } catch (e) { loginPolling = false; }
}

function closeLoginModal() { $("#loginModal").classList.add("hidden"); }
$("#btnLoginClose").addEventListener("click", closeLoginModal);
$("#btnQrRefresh").addEventListener("click", openLoginModal);

$("#btnImportCookie").addEventListener("click", async () => {
  const raw = $("#cookieRaw").value.trim();
  if (!raw) return toast("请先粘贴 Cookie 内容", true);
  await busy("导入 Cookie", async () => {
    const r = await api(`/accounts/${enc(currentAccount)}/cookies`, {
      method: "POST", body: { raw },
    });
    toast(r.tip + `（导入 ${r.imported} 条）`, !r.logged_in);
    if (r.logged_in) {
      $("#qrBox").innerHTML = '<span style="color:#4ade80;font-size:30px">✓ Cookie 登录成功</span>';
      setTimeout(closeLoginModal, 1500);
      refreshState();
    }
  });
});

$("#btnDry").addEventListener("click", () => runSend(true));
$("#btnSend").addEventListener("click", () => {
  if (!confirm(`确定现在向勾选的好友真实发送消息吗？（账号：${currentAccount}）`)) return;
  runSend(false);
});
async function runSend(dry) {
  if (!currentAccount) return toast("请先添加账号", true);
  const force = $("#forceSend")?.checked || false;
  await busy(dry ? "模拟演练" : "发送中", async () => {
    const r = await api(`/accounts/${enc(currentAccount)}/run/send`, {
      method: "POST", body: { dry, force },
    });
    const lines = Object.entries(r.details || {})
      .map(([n, d]) => `${d.ok ? "✓" : "✗"} ${n}：${d.detail}（${d.verified_by}）`);
    toast((dry ? "演练完成：" : "发送完成：") + `成功 ${r.ok_count ?? 0}/${r.total ?? 0}` +
      (lines.length ? "\n" + lines.join("\n") : ""));
    refreshState();
  });
}

$("#btnSync").addEventListener("click", syncFriends);
$("#btnSync2").addEventListener("click", syncFriends);
async function syncFriends() {
  if (!currentAccount) return toast("请先添加账号", true);
  await busy("同步好友", async () => {
    const r = await api(`/accounts/${enc(currentAccount)}/friends/sync`, { method: "POST" });
    toast(`同步完成：发现 ${r.found} 人，新增/更新 ${r.upserted}，移除失效 ${r.removed}`);
    if ($$("#nav button").find((b) => b.classList.contains("active"))?.dataset.tab === "friends") loadFriends();
    refreshState();
  });
}

$("#btnCheck").addEventListener("click", async () => {
  if (!currentAccount) return toast("请先添加账号", true);
  await busy("检查登录态", async () => {
    const r = await api(`/accounts/${enc(currentAccount)}/run/check`, { method: "POST" });
    toast(r.ok ? `登录态正常（${r.latency_ms}ms）` : `异常：${r.detail}`, !r.ok);
  });
});

/* ---------------- 好友 ---------------- */
async function loadFriends() {
  if (!currentAccount) return;
  try {
    friendsCache = await api(`/accounts/${enc(currentAccount)}/friends`);
  } catch (e) { friendsCache = []; }
  renderFriends();
}

function renderFriends() {
  const tbody = $("#friendsTable tbody");
  $("#friendsEmpty").style.display = friendsCache.length ? "none" : "";
  tbody.innerHTML = friendsCache.map((f, i) => `
    <tr>
      <td><input type="checkbox" data-i="${i}" class="f-sel" ${f.selected ? "checked" : ""}></td>
      <td>${esc(f.name)}</td>
      <td>${f.streak_days ? `<span class="streak">🔥 ${f.streak_days} 天</span>` : '<span class="muted">—</span>'}</td>
      <td>${f.per_friend && f.per_friend.messages && f.per_friend.messages.length ? `<span class="muted">${f.per_friend.messages.length} 条专属</span>` : '<span class="muted">公共池</span>'}</td>
      <td><button class="ghost f-edit" data-i="${i}">编辑</button></td>
    </tr>`).join("");
}

$("#friendsTable").addEventListener("change", async (e) => {
  const chk = e.target.closest(".f-sel");
  if (!chk) return;
  const f = friendsCache[chk.dataset.i];
  try {
    await api(`/accounts/${enc(currentAccount)}/friends/select`, {
      method: "POST", body: { names: [f.name], selected: chk.checked },
    });
    f.selected = chk.checked ? 1 : 0;
  } catch (err) { chk.checked = !chk.checked; toast(err.message, true); }
});

$("#chkAll").addEventListener("change", async (e) => {
  const selected = e.target.checked;
  await busy("更新勾选", async () => {
    await api(`/accounts/${enc(currentAccount)}/friends/select`, {
      method: "POST", body: { names: friendsCache.map((f) => f.name), selected },
    });
    friendsCache.forEach((f) => (f.selected = selected ? 1 : 0));
    renderFriends();
  });
});
$("#btnSelectAll").addEventListener("click", () => { $("#chkAll").checked = true; $("#chkAll").dispatchEvent(new Event("change")); });
$("#btnSelectNone").addEventListener("click", () => { $("#chkAll").checked = false; $("#chkAll").dispatchEvent(new Event("change")); });
$("#btnSelectSpark").addEventListener("click", async () => {
  await busy("更新勾选", async () => {
    await api(`/accounts/${enc(currentAccount)}/friends/select`, {
      method: "POST", body: { names: friendsCache.map((f) => f.name), selected: false },
    });
    const spark = friendsCache.filter((f) => f.streak_days >= 1).map((f) => f.name);
    await api(`/accounts/${enc(currentAccount)}/friends/select`, {
      method: "POST", body: { names: spark, selected: true },
    });
    friendsCache.forEach((f) => (f.selected = f.streak_days >= 1 ? 1 : 0));
    renderFriends();
    toast(`已勾选 ${spark.length} 个有火花的好友`);
  });
});

let editIndex = -1;
$("#friendsTable").addEventListener("click", (e) => {
  const btn = e.target.closest(".f-edit");
  if (!btn) return;
  editIndex = +btn.dataset.i;
  const f = friendsCache[editIndex];
  $("#friendModalName").textContent = f.name;
  $("#friendMessages").value = (f.per_friend?.messages || []).join("\n");
  $("#friendEnabled").checked = !(f.per_friend?.enabled === false);
  $("#friendModal").classList.remove("hidden");
});
$("#btnFriendClose").addEventListener("click", () => $("#friendModal").classList.add("hidden"));
$("#btnFriendSave").addEventListener("click", async () => {
  if (editIndex < 0) return;
  const f = friendsCache[editIndex];
  const messages = $("#friendMessages").value.split("\n").map((s) => s.trim()).filter(Boolean);
  const enabled = $("#friendEnabled").checked;
  await busy("保存专属文案", async () => {
    const cfg = await api(`/accounts/${enc(currentAccount)}/config`);
    cfg.per_friend = cfg.per_friend || {};
    if (messages.length || !enabled) cfg.per_friend[f.name] = { messages, enabled };
    else delete cfg.per_friend[f.name];
    await api(`/accounts/${enc(currentAccount)}/config`, { method: "PUT", body: cfg });
    $("#friendModal").classList.add("hidden");
    toast("已保存");
    loadFriends();
  });
});

/* ---------------- 定时配置 ---------------- */
async function loadConfig() {
  if (!currentAccount) return;
  const c = await api(`/accounts/${enc(currentAccount)}/config`);
  const map = {
    cfg_send_time: c.send_time, cfg_jitter_minutes: c.jitter_minutes,
    cfg_deadline_time: c.deadline_time, cfg_retry_interval_minutes: c.retry_interval_minutes,
    cfg_prewarm_minutes: c.prewarm_minutes, cfg_max_friends_per_run: c.max_friends_per_run,
    cfg_gap_lo: c.gap_seconds?.[0], cfg_gap_hi: c.gap_seconds?.[1],
    cfg_health_check_time: c.health_check_time,
    cfg_window_start: c.send_window?.[0] || "", cfg_window_end: c.send_window?.[1] || "",
  };
  for (const [id, v] of Object.entries(map)) {
    const el = $("#" + id);
    if (el && v !== undefined && v !== null) el.value = v;
  }
  $("#cfg_messages").value = (c.messages || []).join("\n");
  $("#cfg_festivals").value = Object.entries(c.festivals || {})
    .map(([k, v]) => `${k}=${v}`).join("\n");
  $("#cfg_allowlist").value = (c.friend_allowlist || []).join("\n");
  $("#cfg_enabled").checked = !!c.enabled;
  $("#cfg_allow_first_message").checked = !!c.allow_first_message;
  $("#cfg_notify_on_success").checked = !!c.notify_on_success;
  $("#cfg_notify_on_failure").checked = !!c.notify_on_failure;
}

$("#btnSaveCfg").addEventListener("click", async () => {
  await busy("保存配置", async () => {
    const c = await api(`/accounts/${enc(currentAccount)}/config`);
    c.send_time = $("#cfg_send_time").value.trim();
    c.jitter_minutes = +$("#cfg_jitter_minutes").value || 0;
    c.deadline_time = $("#cfg_deadline_time").value.trim();
    c.retry_interval_minutes = +$("#cfg_retry_interval_minutes").value || 10;
    c.prewarm_minutes = +$("#cfg_prewarm_minutes").value || 0;
    c.max_friends_per_run = +$("#cfg_max_friends_per_run").value || 30;
    c.gap_seconds = [+$("#cfg_gap_lo").value || 8, +$("#cfg_gap_hi").value || 20];
    c.health_check_time = $("#cfg_health_check_time").value.trim();
    const ws = $("#cfg_window_start").value.trim(), we = $("#cfg_window_end").value.trim();
    c.send_window = ws && we ? [ws, we] : null;
    c.messages = $("#cfg_messages").value.split("\n").map((s) => s.trim()).filter(Boolean);
    c.festivals = {};
    $("#cfg_festivals").value.split("\n").map((s) => s.trim()).filter(Boolean)
      .forEach((line) => {
        const i = line.indexOf("=");
        if (i > 0) c.festivals[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      });
    c.friend_allowlist = $("#cfg_allowlist").value.split("\n")
      .map((s) => s.trim()).filter(Boolean);
    c.enabled = $("#cfg_enabled").checked;
    c.allow_first_message = $("#cfg_allow_first_message").checked;
    c.notify_on_success = $("#cfg_notify_on_success").checked;
    c.notify_on_failure = $("#cfg_notify_on_failure").checked;
    await api(`/accounts/${enc(currentAccount)}/config`, { method: "PUT", body: c });
    toast("定时配置已保存");
    refreshState();
  });
});

/* ---------------- 通知 ---------------- */
async function loadNotify() {
  const n = await api("/notify");
  for (const [k, v] of Object.entries(n)) {
    const el = $("#nt_" + k);
    if (el) el.value = v || "";
  }
  renderNotifyChannels();
}

function renderNotifyChannels() {
  $("#notifyChannels").textContent = STATE?.notify_channels?.length
    ? "已启用：" + STATE.notify_channels.join("、") : "（未启用任何渠道）";
}

$("#btnSaveNotify").addEventListener("click", async () => {
  await busy("保存通知配置", async () => {
    const body = {};
    ["serverchan_sendkey", "bark_url", "telegram_token", "telegram_chat_id",
      "wecom_webhook", "dingtalk_webhook", "dingtalk_secret", "custom_webhook", "proxy"]
      .forEach((k) => { body[k] = $("#nt_" + k).value.trim(); });
    const r = await api("/notify", { method: "PUT", body });
    await refreshState();
    renderNotifyChannels();
    toast("已保存，启用渠道：" + (r.enabled.join("、") || "无"));
  });
});

$("#btnTestNotify").addEventListener("click", async () => {
  await busy("测试推送", async () => {
    const r = await api("/notify/test", { method: "POST" });
    const bad = r.filter((x) => !x.ok);
    toast(bad.length ? "部分渠道失败：" + bad.map((x) => x.channel).join("、") : "测试推送已发出 ✅", bad.length > 0);
  });
});

/* ---------------- 日志 ---------------- */
let logStream = null;
function startLogStream() {
  const box = $("#logBox");
  if (logStream) return;
  api("/logs/recent?n=600").then((r) => {
    if (!box.textContent) box.textContent = r.lines.join("\n");
    box.scrollTop = box.scrollHeight;
  }).catch(() => {});
  const es = new EventSource(`/api/logs/stream?token=${encodeURIComponent(TOKEN)}`);
  es.onmessage = (ev) => {
    try {
      const { line } = JSON.parse(ev.data);
      box.textContent += (box.textContent ? "\n" : "") + line;
      if ($("#autoScroll").checked) box.scrollTop = box.scrollHeight;
    } catch { /* ignore */ }
  };
  es.onerror = () => { /* EventSource 自动重连 */ };
  logStream = es;
}
$("#btnClearLog").addEventListener("click", () => { $("#logBox").textContent = ""; });

/* ---------------- 启动 ---------------- */
async function boot() {
  try {
    await refreshState();
    setInterval(refreshState, 15000);
  } catch (e) { /* boot 前已处理 401 */ }
}

/* 启动：先直接尝试——本机免令牌模式直接进入；需要令牌时 401 会自动弹出输入框 */
boot();
