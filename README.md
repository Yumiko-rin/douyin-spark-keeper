# 🔥 火花管家 Pro —— 抖音火花自动续期

比同类项目更**准时、更稳、更快**的抖音「火花」自动续期工具。每天到点自动给勾选的好友发一条消息，火花不断。

> 火花规则：双方连续互发消息 3 天点亮 🔥，断一天清零，连续 30 天升级为红色「聊得火热」。本工具负责你这一侧每天不缺席。

---

## 功能全景

- **准时发送**：NTP/HTTP 双重校时，本机时钟漂移也不影响；默认零抖动到点秒发
- **三种时刻模式**：精确时刻 + 抖动 ／ **发送窗口**（如 20:00~23:00 内按天随机选一个确定时刻）／ 保底重试
- **预热机制**：发送前自动打开聊天页，到点即发，无冷启动延迟
- **防错发校验**：发送前核对当前会话标题是否为目标好友，杜绝「发给上一个人」（同类项目踩过的坑）
- **送达确认**：每条消息双层验证（DOM 气泡 + IM 接口响应），未确认自动重试，重发前查重避免刷屏
- **限流熔断**：页面出现「操作频繁／安全验证」立即停止整轮并推送告警，绝不盲目重试火上浇油
- **保底重试**：整轮失败后按间隔重试（只补失败者），直到保底截止（默认 23:55）
- **启动补偿**：进程重启后发现「今天该发没发」且未过截止，立即补发
- **主动巡检**：每天定时检查登录态 + 同步好友；失效当天推送提醒
- **文案系统**：随机文案池 + 变量（`{friend} {date} {time} {weekday} {streak}`）+ **一言 API**（`{hitokoto}`）+ **节日祝福**（`{festival}`，配置 MM-DD 表）+ **图片消息**（`image:路径`，实验性）
- **好友别名匹配**：备注/抖音号搜索（`per_friend.search_name`），昵称搜不到也能定位
- **发送名单白名单**：`friend_allowlist` 非空时无视勾选，供 CI/无界面部署
- **演练模式**：全流程 dry-run，不真实发送
- **漏发提醒**：昨日没发成，次日巡检时推送「火花可能已中断」
- **失败诊断**：失败自动保存页面截图（保留最近 20 张）；可选 Playwright Trace
- **Cookie 导入**：粘贴 `k=v; ...` 或 JSON 数组免扫码登录；支持导入 `storage_state.json` 换机迁移
- **统计**：连续保住天数、成功天数、发送流水
- **多账号**：独立配置/浏览器档案/定时任务，LRU 上限控制内存；删除账号归档不销毁
- **推送**：Server酱 / Bark / Telegram / 企业微信 / **钉钉（支持加签）** / 自定义 webhook；支持代理；可用 `NOTIFY_*` 环境变量配置
- **两种入口**：Web 控制台（SSE 实时日志）与 CLI（`manage.py`），功能等价
- **部署**：Windows / Linux / Docker / **GitHub Actions**（fork 即用）

## 与同类项目对比

参考了 GitHub 上主要同类项目的功能与教训（[halfwaystudent/douyin-sparkflow 478★](https://github.com/halfwaystudent/douyin-sparkflow)、[2061360308/DouYinSparkFlow 368★](https://github.com/2061360308/DouYinSparkFlow)、[unmev/douyin-auto-fire 295★](https://github.com/unmev/douyin-auto-fire)、[dr-190/ScriptCat-Douyin-Fire-Helper 119★](https://github.com/dr-190/ScriptCat-Douyin-Fire-Helper)、[bling-yshs/douyin-auto-spark 104★](https://github.com/bling-yshs/douyin-auto-spark)、[Xiaowu-0916/douyin-spark 70★](https://github.com/Xiaowu-0916/douyin-spark)、[diyiqiuye/douyin-keeper](https://github.com/diyiqiuye/douyin-keeper)、[HRuiCcc/dy-xuhuohua](https://github.com/HRuiCcc/dy-xuhuohua)）：

| 能力 | 本项目 | 同类普遍水平 |
|---|---|---|
| 准时性 | NTP 校时 + 零抖动默认，秒级触发；窗口模式按天确定性随机 | 定时任务/±30 分钟抖动 |
| 送达确认 | DOM 气泡 + IM 接口响应双层验证 | 回车即算成功 |
| 防错发 | 会话标题校验，fail-closed | 少数项目有 |
| 限流熔断 | 风控关键词即停 + 告警 | 少数项目有 |
| 失败兜底 | 截止前只补失败者 + 启动补偿 + 漏发提醒 | 一次补发或无 |
| 登录方式 | 扫码 ＋ **Cookie 导入** ＋ storage_state 迁移 | 手动抓 Cookie 或仅扫码 |
| 文案 | 模板池 + 变量 + 一言 + 节日 + 图片（实验） | 静态模板/一言 |
| 通知 | 6 种渠道 + 环境变量配置 | 无或单一 |
| 诊断 | 失败截图 + 可选 Trace | 无 |
| 工程 | 49 项单元测试、ruff 清零、原子写盘、SSE 控制台 | 多无测试 |

技术路线说明：抖音网页 IM 是 protobuf + 私有签名，纯 HTTP 直调改版即碎。本项目选择**真实浏览器内核驱动**（Playwright 持久化上下文，登录态落盘、自带指纹环境），配合轻量反检测与集中选择器注册表，把「抖音改版」的维护成本压到改一个文件（`douyin/selectors.py`）。

## 快速开始（Windows）

```bat
:: 1. 安装（需 Python 3.11+；py 是 Windows 官方启动器，比 python 命令更稳，
::    能避开商店占位符/多版本环境指向错误解释器的坑）
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium

:: 2. 配置（编辑 .env，把 AUTH_TOKEN 改成你自己的随机字符串）
copy .env.example .env

:: 3. 启动（chcp 65001 防止 cmd 里中文日志乱码，可选）
chcp 65001
.venv\Scripts\python app.py

:: 4. 打开控制台：浏览器访问 http://127.0.0.1:8020 → 输入 AUTH_TOKEN
::    「扫码登录」→ 抖音 App 扫一扫 → 「同步好友」→「好友」页勾选
::    → 「定时」页设置时间 → 先「模拟演练」，再等定时或手动「立即发送」
```

## Linux / 服务器

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
cp .env.example .env   # HOST=0.0.0.0，改 AUTH_TOKEN
systemd 示例见下方；建议国内节点部署，风控与延迟都更友好
```

`/etc/systemd/system/spark.service`：

```ini
[Unit]
Description=Spark Keeper
After=network-online.target

[Service]
WorkingDirectory=/opt/spark
ExecStart=/opt/spark/.venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Docker

```bash
cp .env.example .env   # HOST=0.0.0.0
docker compose up -d
```

## GitHub Actions（fork 即用，无需服务器）

1. 本机扫码或 Cookie 登录一次，取出 `data/accounts/<账号>/storage_state.json`；
2. 仓库 Settings → Secrets → Actions 新建 **`SPARK_STORAGE_STATE`**，粘贴文件内容；
3. 把账号配置（含 `friend_allowlist` 白名单名单）存为仓库里的 `spark.config.json`；
4. 打开 Actions 启用 workflow，或手动 `Run workflow` 先跑一次 **dry_run** 演练。

定时为北京时间 21:10（Actions 定时常迟到，已留余量，发送逻辑自带重试）。
失败提醒可在 Secrets 里配 `NOTIFY_*` 环境变量。

> ⚠️ 同类项目实测反馈：GitHub Runner 的机房 IP 可能被抖音风控识别并踢下线，可靠性不如自托管。Actions 模式适合尝鲜，长期使用建议 Docker/服务器。

## CLI（不开控制台）

```bash
python manage.py accounts            # 账号列表
python manage.py init 大号 --config spark.config.json   # 从文件初始化配置
python manage.py login 大号          # 出二维码（保存 login_qr.png）并等待扫码
python manage.py cookies 大号 --file cookie.txt    # 导入 Cookie 免扫码（k=v 文本）
python manage.py cookies 大号 --file state.json    # 导入 storage_state（换机/Actions）
python manage.py sync 大号           # 同步好友
python manage.py send 大号 --dry     # 演练
python manage.py send 大号 --select 侯,小明   # 临时只发给指定好友
python manage.py check 大号          # 登录态巡检
python manage.py notify-test         # 测试推送
python manage.py serve               # 启动服务（同 python app.py）
```

## 每天的工作原理

```
10:00  巡检：NTP 校时 → 登录态检查 → 同步好友（可关）→ 昨日漏发提醒
20:58  预热：浏览器 + 聊天页就绪
21:00  发送：逐条随机文案（一言/节日/变量动态填充）→ 防错发校验 → 双层确认 → 失败重试
       好友间随机停顿 8~20s；出现「操作频繁/安全验证」立即熔断停发并告警
       （整轮有失败 → 每 10 分钟补一轮，只发失败者，直到 23:55 截止）
23:55+ 仍未成功 → 紧急推送；次日巡检再提醒「火花可能已中断」
```

## 配置参考

全局（`.env`）：`PORT` `HOST` `AUTH_TOKEN`（必改）`TZ` `DATA_DIR` `HEADLESS` `NTP_SERVERS` `LOG_LEVEL` `MAX_CONTEXTS` `PROXY`（浏览器代理）+ 可选 `NOTIFY_*` 环境变量

每账号（控制台「定时」页可视化编辑，或 `data/accounts/<名>/config.json`）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `send_time` | `21:00` | 每日发送时刻 |
| `jitter_minutes` | `0` | 抖动分钟，0=分秒不差 |
| `send_window` | `null` | 发送窗口 `["20:00","23:00"]`：窗口内按天随机选一个确定时刻，优先于 send_time |
| `deadline_time` | `23:55` | 保底重试截止 |
| `prewarm_minutes` | `2` | 提前预热分钟 |
| `retry_interval_minutes` | `10` | 整轮重试间隔 |
| `gap_seconds` | `[8, 20]` | 好友间随机停顿 |
| `max_friends_per_run` | `30` | 单轮人数上限 |
| `messages` | 内置池 | 文案池，支持变量与 `image:` 图片条目 |
| `festivals` | `{}` | `"MM-DD": "祝福语"`，当天填充 `{festival}` |
| `per_friend` | `{}` | 好友名 → `{messages, enabled, search_name}` |
| `friend_allowlist` | `[]` | 非空时直接作为发送名单（CI/无 DB 场景） |
| `health_check_time` | `10:00` | 每日巡检时刻 |
| `auto_sync_friends` | `true` | 巡检时同步好友 |
| `notify_on_success` / `notify_on_failure` | `false` / `true` | 推送开关 |
| `risk_halt_notify` | `true` | 风控熔断时推送 |
| `debug_screenshot` / `debug_trace` | `true` / `false` | 失败诊断截图 / Playwright Trace |

## 稳定性设计

- **防错发**：会话标题校验（找到标题但不匹配 → 拦截；找不到标题 → 放行并告警），杜绝发错人
- **限流熔断**：命中风控关键词立即终止整轮、标记当日熔断、推送告警，不做无谓重试
- **原子写盘**：配置写临时文件再 `os.replace`，断电不留半截文件；损坏配置自动备份回退
- **异常隔离**：单好友失败不影响其他人；账号循环异常退避重试，不拖垮调度
- **会话自愈**：浏览器上下文崩溃自动重建；LRU 淘汰控制内存
- **时钟独立**：所有调度判断走校时时钟，NTP→HTTP Date→本机三级降级
- **选择器单点**：抖音改版只需更新 `douyin/selectors.py` 的候选列表（每目标多级回退）
- **失败可诊断**：截图 + Trace + 流水记录，出问题能复盘

## 测试

```bash
.venv/Scripts/python -m pytest tests/ -q
```

覆盖调度时间计算（准时性/窗口模式）、文案模板（变量/动态注入）、会话行解析（宁漏勿错）、Cookie 解析、钉钉加签、风控关键词、SQLite、配置校验。

## FAQ

**会封号吗？** 自动化操作可能违反抖音平台规则，存在风控风险。建议：小号先试跑、保留默认的拟人间隔、不要把间隔调到 3 秒以下、不要给不认识的人群发。数据全部本地存储。

**多久需要重新扫码？** 登录态通常几天到几周。巡检发现失效会当天推送提醒，控制台 10 秒重新扫码（浏览器档案持久化，比纯 storage_state 方案存活更久）。

**一方发送能续火花吗？** 社区共识是需要双方互动（3 天点亮）。本工具保证你这侧每天不缺席；对方是否回复取决于你们的感情 🙂

**抖音改版了？** 症状通常是「同步好友 = 0」。打开 `douyin/selectors.py` 按新版页面补一个候选选择器即可，不用动业务代码。

**想秒级压测发得更快？** 把 `gap_seconds` 调小即可，但请克制——人的手指没那么快。

## 免责声明

本项目仅供学习交流与个人自动化使用，使用本项目产生的一切后果由使用者自行承担。请遵守目标平台的服务条款与当地法律法规。

## License

MIT
