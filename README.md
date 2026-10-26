# tobaccolifestyle.com 商品上架 / 补货监控

针对 `https://tobaccolifestyle.com/zh` 的可运行监控程序。纯标准库实现，无需 pip 安装任何依赖。

## 一、站点结论（实测）

| 项 | 实测结果 |
|---|---|
| 建站系统 | Shopify |
| 数据接口 | `https://tobaccolifestyle.com/products.json` 公开可读，`access-control-allow-origin: *` |
| 商品总量 | 826 个（250 × 3 页 + 76），分 4 页；该店在上新期，数量持续增长 |
| 变体可售状态 | 首次抽样 250 个商品：149 个变体在售、101 个缺货 → `available` 字段真实有效 |
| 上新节奏 | 实测当日 14:27 / 14:32 / 14:34 上架的 3 个 Kendal 商品均为 **0.00 元 + 缺货**，说明该店采用「先建条目、后补实价放货」的两段式上新 |
| 爬虫政策 | `robots.txt` 明确 `Allow: /`，商品页与 JSON 均可抓取 |
| 限流 | 成本型限流（单页 250 条复杂度 251），连续请求会返回 `429`，需间隔 8 秒以上；且会按客户端指纹区别对待（同 IP 同 UA 下 curl 200、urllib 429） |

**核心判断两条：**

1. **不要解析 HTML。** 每个商品的 `published_at`、`variants[].available`、`variants[].id`、`variants[].price`、`handle` 全在 JSON 里，读接口比解析页面稳定一个数量级，且不受改版影响。
2. **单靠"补货"会漏。** 这家店的新品是 0 元挂出来的，此时 `available=false`、价格 0，等到真正开卖才补价格。所以必须同时监控「新品出现」和「价格 0 → 非 0」两个跃迁，否则你会在商品其实已经可买之后才知道它存在。

## 二、怎么确认要监控哪个商品

三种方式，从粗到精：

**1. 浏览器取 handle（最直接）**
打开商品详情页，URL 形如：
```
https://tobaccolifestyle.com/zh/products/vintage-kendal-tobacco-top-black-cherry-50-gram-tin
```
`/products/` 之后那一段就是 `handle`。把它写进 `config.json` 的 `filters.handles`，即只监控这一个商品。

**2. 用脚本检索（推荐，能看清库存和上架时间）**

```bash
python3 monitor.py list --match kendal          # 按关键词找
python3 monitor.py list --available --limit 30  # 只看当前在售的最近上架
```

输出会给出：`在售/缺货` 状态、价格区间、上架时间、`handle`、`variant_id`。据此确认目标。

**3. 直接查接口（临时排查）**

```bash
curl -s "https://tobaccolifestyle.com/products.json?limit=250&page=1" | jq '.products[] | {title, handle, published_at}'
```

**监控粒度说明（三档，精度递减）**

| 方式 | 配置项 | 说明 |
|---|---|---|
| 单品 SKU（推荐） | `filters.variants` | 一个变体 ID 一个目标，最省流量、最不误报。用 `add` 命令自动写入 |
| 单品/厂牌/品类 | `filters.handles` / `vendors` / `product_types` | 商品级粒度，一个商品多规格时会一起推送 |
| 全站 | 全部留空 | 826 个商品的上新、定价开售与补货都推送 |

## 三、维护监控列表（推荐用法）

把商品链接直接粘进去即可，程序会自动识别 URL 里的 `?variant=` 参数并落盘：

```bash
python3 monitor.py add "https://tobaccolifestyle.com/zh/products/xxx?variant=49067158765854"
python3 monitor.py add "链接1" "链接2" "链接3"      # 支持一次传多个
python3 monitor.py add 47750897074462               # 也接受裸变体 ID
python3 monitor.py list --watchlist                 # 查看当前清单及实时状态
```

`add` 的实际行为：

1. 从链接里取出 `handle` 与 `variant`，用 `/{locale}/products/{handle}.js` 单次请求解析（不等全站目录，1 秒级完成）；
2. 把变体 ID 写入 `filters.variants`，同时把 `{variant, handle, title, url}` 写入 `watchlist`；
3. 打印当前缺货/在售状态与价格，重复项自动去重。

**定向模式**：只要 `filters.variants` 里的每个变体都在 `watchlist` 中有 handle，`check` 就自动切换到定向模式——只对这 6 个 SKU 各发 1 次轻量请求（约 8 秒完成），完全不碰 826 个商品的全站目录。这也是本程序能做到 60 秒一轮还不触发限流的原因。

## 四、快速开始

```bash
cd tobacco-monitor

# 1) 加入监控目标（可随时追加）
python3 monitor.py add "https://tobaccolifestyle.com/zh/products/xxx?variant=49067158765854"

# 2) 首次运行建立基线（静默，不发通知）
python3 monitor.py check

# 3) 配好通知后发一条测试消息
python3 monitor.py test-notify

# 4) 常驻轮询（定向模式下 60 秒一轮）
python3 monitor.py watch

# CI 用：在固定时长内循环检查（把 5 分钟 cron 压成 1 分钟）
python3 monitor.py run --duration 240 --interval 60

# 需要检查全站而不只是清单时
python3 monitor.py check --full          # 强制全量模式
python3 monitor.py check --dry-run       # 只打印不发通知
```

## 五、QQ 邮箱通知配置

1. 登录 QQ 邮箱 → 设置 → 账号 → 找到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务**
2. 开启 **IMAP/SMTP 服务**，按提示验证后获得 **16 位授权码**（不是 QQ 登录密码）
3. 修改 `config.json`：

```json
"qq_email": {
  "enabled": true,
  "smtp_host": "smtp.qq.com",
  "smtp_port": 465,
  "username": "12345678@qq.com",
  "auth_code": "你生成的16位授权码",
  "from_name": "商品监控",
  "to": ["12345678@qq.com"]
}
```

4. 验证：`python3 monitor.py test-notify`

注意：`smtp_port` 用 465（SSL）；若网络屏蔽 465，可改 587 并把代码里的 `SMTP_SSL` 换成 `SMTP + starttls`。

## 六、飞书通知配置

1. 在飞书里建一个群（或用一个已有的群）
2. 群设置 → 群机器人 → 添加机器人 → **自定义机器人**
3. 复制 Webhook 地址，形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`
4. 安全设置二选一（推荐"签名校验"，更安全）：
   - **签名校验**：把密钥填到 `config.json` 的 `secret`，程序会自动算 `timestamp + HMAC-SHA256` 签名
   - **自定义关键词**：填 `监控` 即可（本程序消息里固定含"商品监控"字样），此时 `secret` 留空
5. 修改 `config.json`：

```json
"feishu": {
  "enabled": true,
  "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx",
  "secret": "签名校验密钥，未开启则留空"
}
```

**消息形态**：优先发送交互卡片——标题按事件类型着色（补货红、上新蓝、定价橙、售罄灰），每条商品带一个「查看商品」按钮，点击直达该 SKU 页面；卡片发送失败会自动回退成纯文本，不会丢消息。

安全设置里若选的是「自定义关键词」，关键词填 `监控` 即可，因为卡片标题固定为「商品监控 · …」。

所有通道可同时开启，互不冲突。

## 七、微信推送配置

个人微信没有官方开放的消息接口，可行路径是「借一个服务号或企业微信把消息转进微信」。三条路都已内置在程序里，可在 `config.json` 同时开启，互不影响。

| 路径 | 到达方式 | 免费额度 | 配置难度 | 建议 |
|---|---|---|---|---|
| 企业微信群机器人 | 企业微信 App 群消息（手机通知栏可见） | 无每日总量限制，单机器人 20 条/分钟 | 需注册企业微信并建群 | **长期主力，最稳** |
| PushPlus | 微信服务号「pushplus 推送加」 | 免费版有每日额度 | 扫码约 1 分钟 | 个人微信直推，首选试用 |
| Server酱 | 微信服务号「方糖」 | 免费版每天 5 条 | 扫码约 1 分钟 | 低频场景、极简 |

> 不要用 itchat / wechaty 这类个人号协议去发消息，属于非官方接口，有封号风险。

### 方式 1：企业微信群机器人（推荐）

1. 手机装「企业微信」，用微信登录即可创建组织（一个组织、一个成员也能用，免费）；
2. 建一个群（可以只拉自己），进群 → 右上角「…」→ **群机器人** → 添加机器人 → 新建；
3. 复制 Webhook 地址，形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx`；
4. 填进 `config.json`：

```json
"wecom": {
  "enabled": true,
  "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key"
}
```

程序用 `markdown` 消息类型发送，标题加粗、链接可点，超过 4096 字节会自动拆成多条，不会撞长度上限。

### 方式 2：PushPlus（个人微信直推）

1. 打开 [pushplus.plus](https://www.pushplus.plus)，微信扫码登录；
2. 首页复制 **token**；
3. 填进 `config.json`：

```json
"pushplus": {
  "enabled": true,
  "token": "你的token",
  "topic": ""
}
```

`topic` 留空即一对一推送到你自己的微信；填了则推送到对应群组（需先在后台建群组）。

### 方式 3：Server酱（最简，额度小）

1. 打开 [sct.ftqq.com](https://sct.ftqq.com)，微信扫码登录；
2. 复制 **SendKey**（形如 `SCTxxxxxx`）；
3. 填进 `config.json`：

```json
"serverchan": {
  "enabled": true,
  "sendkey": "SCT你的SendKey"
}
```

免费版每天 5 条，够个人低频补货用；若同一天多商品同时补货，建议改用企业微信或 PushPlus。

### 验证

```bash
python3 monitor.py test-notify
```

终端会逐条打印各渠道结果，例如 `[OK] 企业微信已推送（1 条消息）`。多个渠道同时开启时会全部尝试，任一成功即算通知送达。

## 八、其他免费推送渠道

除微信系以外，程序还内置 6 条免费通道。全部可在 `config.json` 同时开启，`notify_all()` 会依次尝试，任一成功即算送达。

| 渠道 | 平台 | 免费额度 | 需要账号 | 特点 |
|---|---|---|---|---|
| Bark | iOS | 无限量 | 不需要，装 App 即得 Key | **可自建服务端**；支持分组、时效性通知、点开跳转商品页 |
| ntfy | iOS / Android / Web / 桌面 | 公共服免费（有限速） | 不需要 | **开源可自建**；topic 即频道；支持附件与点击跳转 |
| Telegram Bot | 全平台 | 无限量 | 需建 Bot 取 token | 稳定性最好、几乎不限量；国内需网络代理 |
| Discord Webhook | 全平台 | 无限量 | 需群 Webhook | 消息样式好，单条 2000 字符自动分片；国内需代理 |
| 钉钉群机器人 | 全平台 | 免费，20 条/分钟 | 需钉钉群 | 国内企业场景标准，支持 HMAC 加签 |
| 本地桌面通知 | macOS / Linux | 免费 | 不需要 | **零配置**，即时弹出；只在本机有效 |

### Bark（iPhone 首选）

App Store 安装 Bark → 首页看到形如 `https://api.day.app/xxxxxxxx` 的地址，末尾那串就是 Key：

```json
"bark": {
  "enabled": true,
  "device_key": "你的Key",
  "server": "https://api.day.app",
  "group": "商品监控",
  "level": "timeSensitive"
}
```

`level` 设为 `timeSensitive` 可穿透专注模式。若自建了 Bark 服务端，把 `server` 改成自己的域名即可，免费额度问题一并消失。

### ntfy（跨平台，免注册）

手机装 ntfy App → 订阅一个 topic（建议带随机串，如 `tobacco-8f3a1c`）：

```json
"ntfy": {
  "enabled": true,
  "topic": "tobacco-8f3a1c",
  "server": "https://ntfy.sh",
  "priority": 4,
  "tags": ["bell"]
}
```

`priority` 4 为高优先级，5 为最高（可绕过静音）。自建时把 `server` 换成 `http://你的服务器:8080`。

> 公共 ntfy.sh 的 topic 是公开的，任何人猜到名字都能订阅，所以务必用随机串，或改为自建。

### Telegram

在 Telegram 里找 `@BotFather` 建 Bot 拿到 token；给 Bot 发一条消息后访问 `https://api.telegram.org/bot<token>/getUpdates` 取 `chat_id`：

```json
"telegram": {
  "enabled": true,
  "bot_token": "123456:ABC-DEF...",
  "chat_id": "123456789",
  "api_base": "https://api.telegram.org"
}
```

`api_base` 可指向自建反代，用于网络受限环境。

### Discord

群设置 → 整合 → Webhook → 新建 → 复制 URL：

```json
"discord": { "enabled": true, "webhook": "https://discord.com/api/webhooks/xxx/yyy" }
```

### 钉钉

群设置 → 智能群助手 → 添加机器人 → 自定义 → 安全设置可选「加签」：

```json
"dingtalk": {
  "enabled": true,
  "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",
  "secret": "SEC开头的加签密钥，未开启则留空"
}
```

### 本地桌面通知（零配置）

```json
"local": { "enabled": true }
```

macOS 走 `osascript`，Linux 走 `notify-send`。适合人在电脑前时立刻察觉，不依赖任何外部账号，也不会被网络限流。

### 选型建议

- **只用 iPhone**：Bark（自建则彻底免费无限制）＋ 本地桌面通知。
- **Android 或想要开源可控**：ntfy 自建。
- **有 Telegram 使用条件**：Telegram 最稳，且完全不需要担心额度。
- **不折腾、能接受装企业微信**：企业微信群机器人（见第七章）。
- **不建议**：个人微信协议号（封号风险）、短信（要钱且成本与收益不匹配）。

## 九、常驻运行

**方式 A：cron 定时（推荐，最简单）**

```bash
crontab -e
# 每分钟检查一次（定向模式负载极低），日志追加到 monitor.log
* * * * * /usr/bin/cd /Users/zhangjian/WorkBuddy/2026-10-26-14-21-09/tobacco-monitor && /Users/zhangjian/.workbuddy/binaries/python/versions/3.13.12/bin/python3 monitor.py check >> monitor.log 2>&1
```

**方式 B：常驻进程**

```bash
nohup python3 monitor.py watch > monitor.log 2>&1 &
```

**方式 C：launchd（macOS 开机自启，推荐长期使用）**

本目录已附带 `com.tobaccolifestyle.restock-monitor.plist`（每 60 秒执行一次 `check`，`RunAtLoad` 开机即跑）。安装：

```bash
cp com.tobaccolifestyle.restock-monitor.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tobaccolifestyle.restock-monitor.plist
```

查看状态与日志：

```bash
launchctl list | grep tobaccolifestyle     # 第二列是上次退出码，0 为正常
tail -f monitor.log
```

卸载：

```bash
launchctl unload ~/Library/LaunchAgents/com.tobaccolifestyle.restock-monitor.plist
rm ~/Library/LaunchAgents/com.tobaccolifestyle.restock-monitor.plist
```

> plist 里的 Python 路径写死为当前使用的解释器。若升级或更换 Python，改 `ProgramArguments` 第一项即可。

## 十、判定逻辑

所有事件都受 `filters.variants` 白名单约束：不在清单里的变体，即使发生跃迁也不推送。

| 事件 | 触发条件 | 默认 |
|---|---|---|
| `new_product` 新品上架 | 商品 ID 从未出现在 `state.json` 中 | 推送 |
| `price_set` 定价开售 | 某变体价格由 `0.00` → 非 0（该站两段式上新的第二段） | 推送 |
| `restock` 补货 | 某变体 `available` 由 `false` → `true` | 推送 |
| `sold_out` 售罄 | 某变体 `available` 由 `true` → `false` | 不推送 |

关键设计：

- **首次运行静默建基线**。否则一启动就会把 826 个存量商品当成"新品"全推一遍。
- **比对对象是结构化状态，不是 diff 文本**。`state.json` 只存 `product_id → {variant_id: available}` 与 `variant_id → price`，页面改版、广告位、推荐位变化不会造成误报。
- **按商品聚合**。一次上新多规格只发一条消息，不会每个 SKU 一条。
- **价格跃迁只在 0 → 非 0 时触发**，不做任意调价通知，避免被日常改价刷屏。
- **只读公开数据**。不登录、不绕过验证码、不自动下单。

## 十一、限流与调参

实测该站 `products.json` 有成本型限流，连续请求会 `429`。相关参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `interval_seconds` | 60 | 轮询间隔。定向模式下每轮仅 6 次轻量请求，60 秒安全；全量模式（`--full`）建议回到 300 |
| `page_delay_seconds` | 8 | 翻页间隔。想更保守可调到 12–15 |
| `jitter_seconds` | 30 | 每轮随机抖动，避免固定节奏被识别 |

程序内建处理：

- 传输层优先用 `curl`（HTTP/2 + 浏览器化请求头）。实测同 IP、同 UA、间隔 3 秒：curl 返回 200，Python 原生 urllib 返回 429——Shopify 会按客户端指纹区别对待，走 curl 可绕开这一误判；机器上没有 curl 时自动退回 urllib。
- 遇到 `429` 按 `25s × 重试次数` 退避（上限 120s），最多 4 次，不会硬撞。
- 每轮结束按 `interval_seconds + 随机抖动` 休眠，节奏不固定。

如果只想监控少数几个商品，可以把 `filters.handles` 填上、同时把 `interval_seconds` 降到 60——但注意程序仍是整目录抓取（Shopify 没有按商品查询库存的公开接口），降低间隔并不会减少请求数。真正的提速方式是缩短 `page_delay_seconds` 承担 429 风险，或改用官方 Storefront API（需要 access token，可精确查询指定商品）。

## 十二、24 小时运行方案（免费 / 极低成本）

监控程序本身是单文件脚本，任何能跑 Python 3.9+ 且能访问该站的环境都能跑。按「免费程度 × 及时性 × 折腾成本」分四档，数据核实于 2026-10。

| 方案 | 成本 | 检查粒度 | 需要什么 | 主要代价 |
|---|---|---|---|---|
| **GitHub Actions（公开仓库）** | 0 元 | cron 5 分钟 / job 内循环可压到 **约 1 分钟** | 一个 GitHub 账号 | 平台 cron 仍可能延迟；需 keepalive 保活 |
| **Oracle Cloud Always Free** | 0 元 | 1 分钟 | 信用卡验证 | 免费 ARM 已从 4C24G 砍到 2C12G；热门区域常无库存；长期低负载可能被回收 |
| **低价 VPS（轻量服务器）** | 约 2–8 元/月 | 1 分钟 | 支付宝 | 需自己维护系统；新用户促销价，续费会涨 |
| **云函数（SCF / FC）** | 0 元起 | 1 分钟 | 实名云账号 | 免费额度政策已多次调整，长期免费不确定 |

**选择逻辑**：先问「错过一次的成本有多高」。限量烟草补货往往几分钟售空——Actions 用「job 内循环」可以把有效频率压到约 1 分钟（见方案 A），成本仍然为零，代价是偶发的平台延迟与一个 60 天保活机制。

### 方案 A：GitHub Actions（推荐先试，零成本）

本目录已备好全部文件：

```
.github/workflows/monitor.yml   # 主工作流：每 5 分钟启动一个 job，job 内每 60 秒检查
.github/workflows/keepalive.yml # 每月提交一次，防止定时任务因 60 天无活动被停用
ci_bootstrap.py                 # 由模板 + Secrets 生成 config.json
config.example.json             # 配置模板（不含真实凭据，含监控清单）
.gitignore                      # 排除 config.json / state.json / monitor.log
```

**如何突破 5 分钟限制、做到 1 分钟级**

GitHub 定时任务的 cron 最小粒度硬性为 5 分钟，无法直接改。但一个 job 最长可跑 6 小时，因此采用「job 内循环」：

```
00:00  job 启动 → 检查、检查、检查、检查（每 60 秒一次）→ 约 04:25 退出
05:00  下一个 job 启动 → 首尾相接，中间只有约 35 秒空隙
```

由 `monitor.py run --duration 240 --interval 60` 实现。代价是单个 job 占用约 4.5 分钟，全天约 **1296 分钟**（月约 3.9 万分钟）——公开仓库不计费，私有仓库则约需 3.9 万分钟、远超 2000 分钟免费额度，**私有仓库不可行**。

部署步骤：

```bash
cd tobacco-monitor
git init && git add . && git commit -m "restock monitor"
git branch -M main
git remote add origin https://github.com/你的用户名/restock-monitor.git
git push -u origin main
```

然后到仓库 **Settings → Secrets and variables → Actions → New repository secret** 添加：

- `FEISHU_WEBHOOK`：你的飞书机器人 Webhook 地址（必填）
- `FEISHU_SECRET`：若启用了签名校验则填，否则不用加

到 **Actions** 页签启用工作流，点 **Run workflow** 手动跑一次验证。

设计要点：

- **凭据只存 Secrets**。`config.json` 与 `state.json` 在 `.gitignore` 里，公开仓库不会泄露 Webhook；运行时由 `ci_bootstrap.py` 拼出真实配置，缺 Secret 会以退出码 2 明确报错，不会静默空跑。
- **状态用 Actions 缓存跨 job 传递**。每个 job 结束把 `state.json` 存成 `state-<run_id>`，下一个 job 用 `restore-keys` 取最新一份。缓存若被清理，程序会自动静默重建基线，不会误报。
- **必须保留 keepalive**。公开仓库的定时工作流若连续 60 天没有仓库活动，会被 GitHub 自动停用——这是最容易踩的坑，表现为「跑了一个月突然不推了」。keepalive 工作流每月提交一次时间戳来维持活跃。
- **单次检查约 20 秒**（6 个 SKU 各 1 次请求），4 分钟一轮共 4 次，远低于 job 上限。
- **cron 延迟是平台特性**，GitHub 高峰期可能把 job 推迟几分钟，此方案只能压缩「计划间隔」，无法消除平台延迟。若要分钟级确定性，仍需 VPS 或云函数。

### 方案 B：VPS 常驻（最省心，几元/月）

以 1 核 1G 的轻量服务器为例：

```bash
# 1) 上传项目（或 git clone）
scp -r tobacco-monitor root@服务器IP:/opt/

# 2) 写入凭据
cd /opt/tobacco-monitor
FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxx" python3 ci_bootstrap.py

# 3) 装成系统服务（deploy/tobacco-monitor.service 已备好）
cp deploy/tobacco-monitor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tobacco-monitor

# 4) 查看状态与日志
systemctl status tobacco-monitor
journalctl -u tobacco-monitor -f
```

内存占用约 20 MB，1 核 1G 足够跑很多个这样的监控。若服务器在境外（日本/新加坡），访问该站最快；国内机器访问 Shopify 可能更慢甚至不稳定。

### 方案 C：Oracle Cloud Always Free（免费但要折腾）

- 免费额度：ARM 2 OCPU / 12 GB（2026-08-18 起从 4C24G 减半并开始执行）、2 台 1/8 OCPU 的 AMD 微型机、200 GB 存储、10 TB/月出网流量。
- 需要信用卡验证（不扣费），热门区域（如美东）常报 “Out of host capacity”。
- **注意回收策略**：若 7 天内 CPU / 网络 / 内存的 95 分位都低于 20%，实例可能被回收。本监控几乎不耗资源，单跑这一件事有被判定闲置的风险；建议顺便跑点别的（如 nginx、探针）来维持活跃度。
- 装法与方案 B 相同，`systemd` 一把梭。

### 方案 D：国内云函数横向对比（1 分钟粒度）

各家定时触发器最小粒度均为 1 分钟，差异全在**免费额度是否长期有效**。口径核实于 2026-10：

| 厂商 | 产品 | 免费额度 | 是否长期有效 | 依据 |
|---|---|---|---|---|
| **阿里云** | 函数计算 FC | 每月 100 万次调用 + 40 万 CU·秒 | **是**，帮助文档写「所有用户」 | 官方帮助中心 |
| **华为云** | FunctionGraph | 每月 100 万次请求 + 40 万 GB·秒 | **是**，成本规划页标价 0 元 | 官方帮助中心 |
| **腾讯云** | 云函数 SCF | 官方文档称 2022-06 起仅新用户前 3 个月，之后取消；代理商页面仍写「每月 100 万次 + 40 万 GB·s」 | **存疑**，两处口径冲突 | 以控制台账单为准 |
| **百度智能云** | 函数计算 CFC | 2023-03-10 起取消长期额度，新用户仅 3 个月（100 万次 + 40 万 GBs + 1 GB 流量） | 否 | 官方计费概述 |
| **火山引擎** | veFaaS | 文档仅有按量计费与资源包，未见公开长期免费额度 | 否（新用户有代金券） | 官方计费文档 |

**本监控的真实用量**（定向模式，6 个 SKU、每分钟一轮）：

- 调用次数：43,200 次/月 → 占 100 万免费额度的 **4.3%**
- 资源消耗：约 2,700–5,600 GB·秒/月 → 占 40 万免费额度的 **0.7%–1.4%**
- 出网流量：约 0.5 GB/月（6 个商品 JSON、每次约 12 KB）

结论：**这套监控在任何一家都跑不出免费额度**。即使完全按量付费，月成本也只有 **0.6–0.8 元**（腾讯云 0.00011108 元/GB·s × 5000 + 0.0133 元/万次 × 4.32 万次 ≈ 0.6 元）。

因此选型的真正权重不在免费额度，而在两点：

1. **出口 IP 质量**。云函数用共享 NAT 出口，IP 段容易被 Shopify 判定为机房流量并返回 429。本程序已用 curl 作为传输层来规避这类误判，但部署后建议先手动跑 10 轮观察是否稳定。
2. **地域选择**。站点 CDN 在境外，国内地域跨境访问延迟高且抖动；优先选**香港 / 新加坡 / 日本**地域，阿里云与华为云的境外地域同样适用免费额度。

### 轻量服务器价格参考（新用户首年，2026 促销，波动大）

| 厂商 | 入门配置 | 首年价 |
|---|---|---|
| 京东云 | 2 核 2G | 约 50 元 |
| 百度云 | 2 核 2G | 约 59 元 |
| 阿里云 | 2 核 2G | 约 68–99 元 |
| 腾讯云 | 2 核 2G / 4 核 4G | 约 68–118 元（秒杀款可低至 38 元/年） |

重要提醒：以上均为**首年新用户价**，续费通常回到 300–800 元/年。这个监控只需 1 核 1G、20 MB 内存，买最低配即可；更划算的做法是用已有的机器或家里常年开着的设备。

### 不建议的方案

- **Vercel Cron（Hobby）**：免费计划定时任务每天只能触发一次，粒度不够。
- **Render / Railway 免费层**：免费 Web 服务会休眠，Cron Job 属付费功能。
- **PythonAnywhere 免费版**：每天只能跑 1 个定时任务。
- **云函数改写成本**：为了省几元/月把 Python 逻辑重写成 JS（Cloudflare Workers）并不划算，除非你本来就要用它。

## 十三、文件结构

```
tobacco-monitor/
├── monitor.py                    # 主程序（仅标准库）
├── ci_bootstrap.py               # CI/服务器：由模板 + 环境变量生成 config.json
├── config.json                   # 本地真实配置（含凭据，不入库）
├── config.example.json           # 配置模板（无凭据，入库）
├── state.json                    # 状态快照，自动生成（不入库）
├── monitor.log                   # 运行日志（不入库）
├── .gitignore
├── com.tobaccolifestyle.restock-monitor.plist   # macOS 开机自启
├── deploy/
│   └── tobacco-monitor.service   # Linux systemd 服务单元
├── .github/
│   └── workflows/
│       ├── monitor.yml           # GitHub Actions：每 5 分钟启动 job，job 内每 60 秒检查
│       └── keepalive.yml         # 每月提交，防止 60 天无活动导致定时任务被停用
└── README.md
```
