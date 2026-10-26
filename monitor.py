#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tobaccolifestyle.com 商品上架 / 补货监控
--------------------------------------
数据源：Shopify Storefront 公开接口 /products.json（结构化，无需解析 HTML）

用法:
    python3 monitor.py list                  # 列出全部商品（确认监控目标用）
    python3 monitor.py list --match kendal   # 关键词检索，定位具体商品
    python3 monitor.py check                 # 单次检查（首次运行自动静默建立基线）
    python3 monitor.py check --dry-run       # 只打印，不发通知
    python3 monitor.py watch                 # 常驻轮询
    python3 monitor.py test-notify           # 发一条测试通知，验证邮箱/飞书配置

依赖：仅 Python 3.9+ 标准库，无需 pip install
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import random
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"

TZ_CN = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------
def now_cn() -> datetime:
    return datetime.now(TZ_CN)


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    tmp.replace(path)


CURL_BIN = shutil.which("curl")


def _curl_get(url: str, timeout: int) -> tuple[int, str]:
    """用 curl 作为传输层（HTTP/2 + 浏览器化请求头）。

    实测同一 IP、同一 UA、间隔 3 秒：curl 返回 200，urllib 返回 429。
    Shopify 的流量治理会区分客户端指纹，因此优先走 curl，必要时再退回 urllib。
    """
    cmd = [
        CURL_BIN,
        "-sS",
        "--compressed",
        "--http2",
        "--max-time",
        str(timeout),
        "-A",
        UA,
        "-H",
        "Accept: application/json, text/plain, */*",
        "-H",
        "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        "-H",
        "Referer: https://tobaccolifestyle.com/",
        "-w",
        "\n__STATUS__%{http_code}",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 20)
    out = proc.stdout or ""
    idx = out.rfind("__STATUS__")
    if idx < 0:
        return 0, ""
    body = out[:idx]
    try:
        status = int(out[idx + len("__STATUS__"):].strip() or 0)
    except ValueError:
        status = 0
    return status, body


def _request_once(url: str, timeout: int) -> tuple[int, str]:
    if CURL_BIN:
        return _curl_get(url, timeout)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def http_get_json(url: str, timeout: int = 30, retries: int = 4):
    """带退避重试的 GET。429/403/5xx 重试。"""
    last_err = None
    for attempt in range(retries):
        try:
            status, body = _request_once(url, timeout)
        except (subprocess.SubprocessError, OSError, TimeoutError) as e:
            last_err = f"{type(e).__name__}: {e} ({url})"
            time.sleep(3 * (attempt + 1) + random.random())
            continue

        if status == 200:
            try:
                return json.loads(body)
            except json.JSONDecodeError as e:
                last_err = f"JSON 解析失败: {e} ({url})"
                time.sleep(3 * (attempt + 1) + random.random())
                continue

        last_err = f"HTTP {status} {url}"
        if status in (403, 429) or 500 <= status < 600:
            wait = min(120, (25 if status == 429 else 5) * (attempt + 1)) + random.uniform(0, 3)
            print(
                f"  [限流] HTTP {status}，{wait:.0f}s 后重试（第 {attempt + 1}/{retries} 次）",
                file=sys.stderr,
            )
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {status} {url}")

    raise RuntimeError(f"请求失败，已重试 {retries} 次：{last_err}")


# ----------------------------------------------------------------------------
# 商品目录抓取
# ----------------------------------------------------------------------------
def fetch_catalog(
    site: str,
    page_size: int = 250,
    max_pages: int = 40,
    quiet: bool = False,
    page_delay: float = 8.0,
):
    """分页拉取全部商品。返回 {product_id: product_dict}。"""
    site = site.rstrip("/")
    catalog: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        url = f"{site}/products.json?limit={page_size}&page={page}"
        data = http_get_json(url)
        products = data.get("products") or []
        if not products:
            break
        for p in products:
            catalog[str(p["id"])] = p
        if not quiet:
            print(f"  已拉取第 {page} 页：累计 {len(catalog)} 个商品", file=sys.stderr)
        if len(products) < page_size:
            break
        # Shopify 对 products.json 采用成本型限流（单页 250 条复杂度很高），翻页必须放慢
        time.sleep(page_delay + random.random() * 2.0)
    return catalog


def variant_availability(product: dict) -> dict[str, bool]:
    return {str(v["id"]): bool(v.get("available")) for v in product.get("variants", [])}


def variant_prices(product: dict) -> dict[str, str]:
    return {str(v["id"]): str(v.get("price") or "0") for v in product.get("variants", [])}


def _to_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def clean_text(s: str) -> str:
    """去掉 Shopify 多语言翻译标签 <tc>…</tc>，用于通知正文。"""
    s = re.sub(r"</?tc>", "", s or "")
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def product_price_range(product: dict) -> str:
    prices = []
    for v in product.get("variants", []):
        try:
            prices.append(float(v.get("price")))
        except (TypeError, ValueError):
            pass
    if not prices:
        return "-"
    lo, hi = min(prices), max(prices)
    return f"{lo:.2f}" if abs(lo - hi) < 1e-9 else f"{lo:.2f}~{hi:.2f}"


def product_url(site: str, handle: str, locale: str = "") -> str:
    site = site.rstrip("/")
    prefix = f"/{locale.strip('/')}" if locale else ""
    return f"{site}{prefix}/products/{handle}"


# ----------------------------------------------------------------------------
# 过滤规则
# ----------------------------------------------------------------------------
def _haystack(product: dict) -> str:
    parts = [
        product.get("title") or "",
        product.get("vendor") or "",
        product.get("product_type") or "",
        " ".join(product.get("tags") or []),
        product.get("handle") or "",
    ]
    return " ".join(parts).lower()


def watched_variants(f: dict) -> set[str] | None:
    """变体白名单。返回 None 表示不限制。"""
    vs = [str(v).strip() for v in (f.get("variants") or []) if str(v).strip()]
    return set(vs) if vs else None


def matches(product: dict, f: dict) -> bool:
    handle = (product.get("handle") or "").lower()
    handles = [h.lower() for h in (f.get("handles") or [])]
    includes = [k.lower() for k in (f.get("include_keywords") or [])]
    excludes = [k.lower() for k in (f.get("exclude_keywords") or [])]
    vendors = [v.lower() for v in (f.get("vendors") or [])]
    types = [t.lower() for t in (f.get("product_types") or [])]

    # 0) 变体白名单优先级最高：URL 里带 ?variant= 的那一个 SKU
    wv = watched_variants(f)
    if wv is not None:
        if not (wv & set(variant_availability(product).keys())):
            return False
        # 命中即通过，不再受 handles / 关键词约束
        return not any(k in _haystack(product) for k in excludes)

    # 1) 白名单优先：给了 handles 就只看这些
    if handles:
        if handle not in handles:
            return False
    elif includes:
        hay = _haystack(product)
        if not any(k in hay for k in includes):
            return False

    # 2) 厂商 / 品类
    if vendors and (product.get("vendor") or "").lower() not in vendors:
        return False
    if types and (product.get("product_type") or "").lower() not in types:
        return False

    # 3) 排除词
    if excludes:
        hay = _haystack(product)
        if any(k in hay for k in excludes):
            return False

    # 4) 价格区间
    prices = []
    for v in product.get("variants", []):
        try:
            prices.append(float(v.get("price")))
        except (TypeError, ValueError):
            pass
    if prices:
        lo = min(prices)
        if f.get("min_price") is not None and lo < float(f["min_price"]):
            return False
        if f.get("max_price") is not None and lo > float(f["max_price"]):
            return False

    return True


# ----------------------------------------------------------------------------
# 差异计算
# ----------------------------------------------------------------------------
def diff(catalog: dict, state: dict, cfg: dict) -> list[dict]:
    """返回事件列表。事件类型: new_product / price_set / restock / sold_out"""
    old = state.get("products", {})
    filters = cfg.get("filters", {})
    notify_on = cfg.get("notify_on", {})
    wv = watched_variants(filters)
    events: list[dict] = []

    for pid, prod in catalog.items():
        va_new = variant_availability(prod)
        pr_new = variant_prices(prod)
        old_entry = old.get(pid) or {}
        va_old = old_entry.get("variants", {})
        pr_old = old_entry.get("prices", {})

        # 新品：state 里从未出现过
        if pid not in old:
            if notify_on.get("new_product", True) and matches(prod, filters):
                events.append(
                    {
                        "type": "new_product",
                        "product": prod,
                        "available_count": sum(1 for x in va_new.values() if x),
                        "variant_count": len(va_new),
                    }
                )
            continue

        # 开售定价：0.00 -> 非 0（该站常见「先建 0 元条目，后补实价」的上新节奏）
        priced: list[dict] = []
        if notify_on.get("price_set", True) and pr_old:
            for vid, new_price in pr_new.items():
                if wv is not None and vid not in wv:
                    continue
                old_price = pr_old.get(vid)
                fo, fn = _to_float(old_price), _to_float(new_price)
                if fo is not None and fn is not None and fo == 0 and fn > 0:
                    title = "Default Title"
                    for v in prod.get("variants", []):
                        if str(v["id"]) == vid:
                            title = v.get("title") or title
                    priced.append(
                        {"vid": vid, "title": title, "old": old_price or "0.00", "new": new_price}
                    )
        if priced and matches(prod, filters):
            events.append({"type": "price_set", "product": prod, "items": priced})

        # 补货：某变体 false -> true
        restocked = [
            vid
            for vid, av in va_new.items()
            if av and not va_old.get(vid, False) and (wv is None or vid in wv)
        ]
        if restocked and notify_on.get("restock", True) and matches(prod, filters):
            details = []
            for vid in restocked:
                title, price = "Default Title", "-"
                for v in prod.get("variants", []):
                    if str(v["id"]) == vid:
                        title = v.get("title") or title
                        price = str(v.get("price") or "-")
                details.append({"vid": vid, "title": title, "price": price})
            events.append(
                {
                    "type": "restock",
                    "product": prod,
                    "variants": [d["title"] for d in details],
                    "items": details,
                    "count": len(restocked),
                }
            )

        # 售罄：true -> false（默认关闭）
        sold_out = [
            vid
            for vid, av in va_new.items()
            if not av and va_old.get(vid, False) and (wv is None or vid in wv)
        ]
        if sold_out and notify_on.get("sold_out", False) and matches(prod, filters):
            events.append({"type": "sold_out", "product": prod, "count": len(sold_out)})

    return events


def channel_cfg(cfg: dict, name: str) -> dict:
    """渠道配置读取：优先 config["notify"][name]，兼容早期写在顶层的写法。"""
    nested = (cfg.get("notify") or {}).get(name)
    if isinstance(nested, dict):
        return nested
    top = cfg.get(name)
    return top if isinstance(top, dict) else {}


# ----------------------------------------------------------------------------
# 通知：QQ 邮箱
# ----------------------------------------------------------------------------
def send_qq_email(cfg: dict, subject: str, body: str) -> bool:
    c = channel_cfg(cfg, "qq_email")
    if not c.get("enabled"):
        return False
    user = c.get("username") or ""
    auth = c.get("auth_code") or ""
    to_list = c.get("to") or [user]
    if not user or not auth:
        print("  [跳过] QQ 邮箱未配置 username / auth_code", file=sys.stderr)
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header(c.get("from_name") or "商品监控", "utf-8")), user))
    msg["To"] = ", ".join(to_list)

    host = c.get("smtp_host") or "smtp.qq.com"
    port = int(c.get("smtp_port") or 465)
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, auth)
            s.sendmail(user, to_list, msg.as_string())
        print(f"  [OK] 邮件已发送至 {', '.join(to_list)}", file=sys.stderr)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] QQ 邮箱发送异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


# ----------------------------------------------------------------------------
# 通知：飞书自定义机器人
# ----------------------------------------------------------------------------
def send_feishu(cfg: dict, text: str, card: dict | None = None) -> bool:
    c = channel_cfg(cfg, "feishu")
    if not c.get("enabled"):
        return False
    hook = c.get("webhook") or ""
    if not hook:
        print("  [跳过] 飞书 webhook 未配置", file=sys.stderr)
        return False

    secret = c.get("secret") or ""

    def _post(payload: dict) -> tuple[int, str, dict]:
        if secret:
            ts = str(int(time.time()))
            string_to_sign = f"{ts}\n{secret}"
            sign = base64.b64encode(
                hmac.new(string_to_sign.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
            ).decode("utf-8")
            payload = dict(payload, timestamp=ts, sign=sign)
        status, raw = http_post(hook, payload)
        try:
            data = json.loads(raw) if raw.strip().startswith("{") else {}
        except json.JSONDecodeError:
            data = {}
        return status, raw, data

    try:
        # 优先发交互卡片：标题加粗、按钮可直接跳商品页
        if card is not None:
            status, raw, data = _post({"msg_type": "interactive", "card": card})
            code = data.get("code", data.get("StatusCode", 0))
            if code in (0, None) and 200 <= status < 300:
                print("  [OK] 飞书卡片已推送", file=sys.stderr)
                return True
            print(f"  [警告] 飞书卡片发送失败（{raw[:120]}），回退纯文本", file=sys.stderr)

        status, raw, data = _post({"msg_type": "text", "content": {"text": text}})
        code = data.get("code", data.get("StatusCode", 0))
        if code in (0, None):
            print("  [OK] 飞书消息已推送", file=sys.stderr)
            return True
        print(f"  [失败] 飞书返回：{raw}", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] 飞书推送异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


# ----------------------------------------------------------------------------
# 通知：微信（三条路径）
#   1) 企业微信群机器人  —— 最稳，无每日配额焦虑，需企业微信群
#   2) PushPlus          —— 个人微信直推，免费额度较宽
#   3) Server酱          —— 个人微信直推，免费版每天 5 条
# ----------------------------------------------------------------------------
def http_post(
    url: str, payload: dict, headers: dict | None = None, timeout: int = 20
) -> tuple[int, str]:
    """POST JSON，返回 (HTTP 状态码, 响应正文)。优先 curl，退回 urllib。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v})
    if CURL_BIN:
        cmd = [CURL_BIN, "-sS", "--compressed", "--max-time", str(timeout), "-X", "POST"]
        for k, v in hdrs.items():
            cmd += ["-H", f"{k}: {v}"]
        cmd += ["-A", UA, "--data-binary", "@-", "-w", "\n__STATUS__%{http_code}", url]
        proc = subprocess.run(cmd, input=body, capture_output=True, timeout=timeout + 15)
        out = (proc.stdout or b"").decode("utf-8", "replace")
        idx = out.rfind("__STATUS__")
        if idx < 0:
            return 0, out
        try:
            status = int(out[idx + len("__STATUS__"):].strip() or 0)
        except ValueError:
            status = 0
        return status, out[:idx]
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def http_post_json(url: str, payload: dict, timeout: int = 20) -> str:
    """只要响应正文的旧接口。"""
    return http_post(url, payload, timeout=timeout)[1]


def chunk_markdown(md: str, limit: int = 3800) -> list[str]:
    """按空行切块，超长消息拆成多条，规避渠道长度上限（企业微信 4096 字节）。"""
    if len(md.encode("utf-8")) <= limit:
        return [md]
    chunks: list[str] = []
    cur = ""
    for block in md.split("\n\n"):
        cand = f"{cur}\n\n{block}" if cur else block
        if len(cand.encode("utf-8")) > limit and cur:
            chunks.append(cur)
            cur = block
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return chunks


def send_wecom(cfg: dict, md: str) -> bool:
    c = channel_cfg(cfg, "wecom")
    if not c.get("enabled"):
        return False
    hook = c.get("webhook") or ""
    if not hook:
        print("  [跳过] 企业微信 webhook 未配置", file=sys.stderr)
        return False
    ok = True
    parts = chunk_markdown(md)
    for i, part in enumerate(parts, 1):
        try:
            raw = http_post_json(hook, {"msgtype": "markdown", "markdown": {"content": part}})
            data = json.loads(raw) if raw.strip().startswith("{") else {}
            if data.get("errcode") not in (0, None):
                print(f"  [失败] 企业微信返回：{raw}", file=sys.stderr)
                ok = False
        except Exception as e:  # noqa: BLE001
            print(f"  [失败] 企业微信推送异常：{type(e).__name__}: {e}", file=sys.stderr)
            ok = False
        if i < len(parts):
            time.sleep(0.5)
    if ok:
        print(f"  [OK] 企业微信已推送（{len(parts)} 条消息）", file=sys.stderr)
    return ok


def send_pushplus(cfg: dict, subject: str, md: str) -> bool:
    c = channel_cfg(cfg, "pushplus")
    if not c.get("enabled"):
        return False
    token = c.get("token") or ""
    if not token:
        print("  [跳过] PushPlus token 未配置", file=sys.stderr)
        return False
    payload = {
        "token": token,
        "title": subject,
        "content": md,
        "template": "markdown",
    }
    if c.get("topic"):
        payload["topic"] = c["topic"]
    try:
        raw = http_post_json("https://www.pushplus.plus/send", payload)
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        if data.get("code") == 200:
            print("  [OK] PushPlus 已推送", file=sys.stderr)
            return True
        print(f"  [失败] PushPlus 返回：{raw}", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] PushPlus 推送异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


def send_serverchan(cfg: dict, subject: str, md: str) -> bool:
    c = channel_cfg(cfg, "serverchan")
    if not c.get("enabled"):
        return False
    key = c.get("sendkey") or ""
    if not key:
        print("  [跳过] Server酱 SendKey 未配置", file=sys.stderr)
        return False
    url = f"https://sctapi.ftqq.com/{key}.send"
    try:
        raw = http_post_json(url, {"title": subject, "desp": md})
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        if data.get("code") == 0 or data.get("errno") == 0:
            print("  [OK] Server酱已推送", file=sys.stderr)
            return True
        print(f"  [失败] Server酱返回：{raw}", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] Server酱推送异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


# ----------------------------------------------------------------------------
# 通知：其他免费渠道
#   bark / ntfy / telegram / discord / dingtalk / 本地桌面通知
# ----------------------------------------------------------------------------
def chunk_text(text: str, limit: int) -> list[str]:
    """按行贪心切分纯文本，用于 Discord 之类有字符上限的渠道。"""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        cand = f"{cur}\n{line}" if cur else line
        if len(cand) > limit and cur:
            out.append(cur)
            cur = line
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def send_bark(cfg: dict, subject: str, body: str) -> bool:
    """Bark：iOS 免费推送，无需账号，可自建服务端。"""
    c = channel_cfg(cfg, "bark")
    if not c.get("enabled"):
        return False
    key = c.get("device_key") or ""
    if not key:
        print("  [跳过] Bark device_key 未配置", file=sys.stderr)
        return False
    server = (c.get("server") or "https://api.day.app").rstrip("/")
    payload = {
        "title": subject,
        "body": body[:1800],
        "group": c.get("group") or "商品监控",
        "level": c.get("level") or "timeSensitive",
        "icon": c.get("icon"),
        "sound": c.get("sound"),
        "url": c.get("url"),
    }
    payload = {k: v for k, v in payload.items() if v}
    try:
        status, raw = http_post(f"{server}/{key}", payload)
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        if data.get("code") == 200 or status == 200:
            print("  [OK] Bark 已推送", file=sys.stderr)
            return True
        print(f"  [失败] Bark 返回：{status} {raw}", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] Bark 推送异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


def send_ntfy(cfg: dict, subject: str, body: str) -> bool:
    """ntfy：开源、跨平台、免注册，可自建。手机装 ntfy App 订阅同名 topic 即可。"""
    c = channel_cfg(cfg, "ntfy")
    if not c.get("enabled"):
        return False
    topic = c.get("topic") or ""
    if not topic:
        print("  [跳过] ntfy topic 未配置", file=sys.stderr)
        return False
    server = (c.get("server") or "https://ntfy.sh").rstrip("/")
    payload = {
        "topic": topic,
        "title": subject,
        "message": body[:3500],
        "priority": int(c.get("priority") or 4),
        "tags": c.get("tags") or ["bell"],
    }
    if c.get("click"):
        payload["click"] = c["click"]
    try:
        status, raw = http_post(f"{server}/", payload)
        if 200 <= status < 300:
            print("  [OK] ntfy 已推送", file=sys.stderr)
            return True
        print(f"  [失败] ntfy 返回：{status} {raw}", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] ntfy 推送异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


def send_telegram(cfg: dict, subject: str, body: str) -> bool:
    """Telegram Bot：完全免费、无限量，稳定性最好的一条。"""
    c = channel_cfg(cfg, "telegram")
    if not c.get("enabled"):
        return False
    token = c.get("bot_token") or ""
    chat_id = c.get("chat_id") or ""
    if not token or not chat_id:
        print("  [跳过] Telegram bot_token / chat_id 未配置", file=sys.stderr)
        return False
    base = (c.get("api_base") or "https://api.telegram.org").rstrip("/")
    text = f"{subject}\n\n{body}"
    try:
        status, raw = http_post(
            f"{base}/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True},
        )
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        if data.get("ok") or status == 200:
            print("  [OK] Telegram 已推送", file=sys.stderr)
            return True
        print(f"  [失败] Telegram 返回：{status} {raw}", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] Telegram 推送异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


def send_discord(cfg: dict, subject: str, body: str) -> bool:
    """Discord Webhook：免费无限量，单条上限 2000 字符，自动分片。"""
    c = channel_cfg(cfg, "discord")
    if not c.get("enabled"):
        return False
    hook = c.get("webhook") or ""
    if not hook:
        print("  [跳过] Discord webhook 未配置", file=sys.stderr)
        return False
    text = f"**{subject}**\n{body}"
    ok = True
    parts = chunk_text(text, 1900)
    for i, part in enumerate(parts, 1):
        try:
            status, raw = http_post(hook, {"content": part})
            if not (200 <= status < 300):
                print(f"  [失败] Discord 返回：{status} {raw}", file=sys.stderr)
                ok = False
        except Exception as e:  # noqa: BLE001
            print(f"  [失败] Discord 推送异常：{type(e).__name__}: {e}", file=sys.stderr)
            ok = False
        if i < len(parts):
            time.sleep(0.5)
    if ok:
        print(f"  [OK] Discord 已推送（{len(parts)} 条消息）", file=sys.stderr)
    return ok


def send_dingtalk(cfg: dict, subject: str, body: str) -> bool:
    """钉钉群机器人：免费，支持加签。"""
    c = channel_cfg(cfg, "dingtalk")
    if not c.get("enabled"):
        return False
    hook = c.get("webhook") or ""
    if not hook:
        print("  [跳过] 钉钉 webhook 未配置", file=sys.stderr)
        return False
    secret = c.get("secret") or ""
    if secret:
        ts = str(int(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        sign = base64.b64encode(
            hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        sep = "&" if "?" in hook else "?"
        hook = f"{hook}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"
    try:
        status, raw = http_post(
            hook, {"msgtype": "text", "text": {"content": body[:18000]}}
        )
        data = json.loads(raw) if raw.strip().startswith("{") else {}
        if data.get("errcode") in (0, None) and 200 <= status < 300:
            print("  [OK] 钉钉已推送", file=sys.stderr)
            return True
        print(f"  [失败] 钉钉返回：{status} {raw}", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] 钉钉推送异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


def send_local(cfg: dict, subject: str, body: str) -> bool:
    """本地桌面通知：macOS 用 osascript，Linux 用 notify-send，零配置。"""
    c = channel_cfg(cfg, "local")
    if not c.get("enabled"):
        return False
    text = body.replace("\n", " ").replace('"', "'").strip()[:180]
    try:
        if sys.platform == "darwin":
            script = f'display notification "{text}" with title "商品监控" sound name "Glass"'
            proc = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
            if proc.returncode == 0:
                print("  [OK] 已弹出系统通知", file=sys.stderr)
                return True
            print(f"  [失败] osascript：{proc.stderr.decode('utf-8', 'replace')}", file=sys.stderr)
            return False
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            proc = subprocess.run(
                ["notify-send", "商品监控", text], capture_output=True, timeout=15
            )
            if proc.returncode == 0:
                print("  [OK] 已弹出系统通知", file=sys.stderr)
                return True
        print("  [跳过] 当前系统不支持本地通知", file=sys.stderr)
        return False
    except Exception as e:  # noqa: BLE001
        print(f"  [失败] 本地通知异常：{type(e).__name__}: {e}", file=sys.stderr)
        return False


def notify_all(cfg: dict, subject: str, body: str, md: str, card: dict | None = None) -> int:
    """向所有已启用渠道推送，返回成功渠道数。"""
    sent = 0
    if send_local(cfg, subject, body):
        sent += 1
    if send_bark(cfg, subject, body):
        sent += 1
    if send_ntfy(cfg, subject, body):
        sent += 1
    if send_telegram(cfg, subject, body):
        sent += 1
    if send_discord(cfg, subject, body):
        sent += 1
    if send_dingtalk(cfg, subject, body):
        sent += 1
    if send_qq_email(cfg, subject, body):
        sent += 1
    if send_feishu(cfg, body, card):
        sent += 1
    if send_wecom(cfg, md):
        sent += 1
    if send_pushplus(cfg, subject, md):
        sent += 1
    if send_serverchan(cfg, subject, md):
        sent += 1
    return sent


# ----------------------------------------------------------------------------
# 消息组装
# ----------------------------------------------------------------------------
EVENT_LABEL = {
    "new_product": "新品上架",
    "price_set": "定价开售",
    "restock": "补货",
    "sold_out": "售罄",
}
EVENT_ICON = {"new_product": "🆕", "price_set": "💰", "restock": "🔁", "sold_out": "⛔"}


def summarize(events: list[dict]) -> str:
    counts: dict[str, int] = {}
    for e in events:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    return " / ".join(f"{EVENT_LABEL[k]}{v}" for k, v in counts.items())


def event_view(e: dict, cfg: dict) -> dict:
    """把事件整理成渠道无关的展示结构，供纯文本与 Markdown 两种渲染共用。"""
    site = cfg["site"].rstrip("/")
    locale = cfg.get("locale") or ""
    p = e["product"]
    items = e.get("items") or []
    vurl = product_url(site, p.get("handle", ""), locale)
    meta = [f"厂商 {p.get('vendor') or '-'}", f"品类 {p.get('product_type') or '-'}"]

    if e["type"] == "new_product":
        meta.append(f"价格 {product_price_range(p)}")
        meta.append(f"可售 {e['available_count']}/{e['variant_count']}")
        if p.get("published_at"):
            meta.append(f"上架 {p['published_at'][:16].replace('T', ' ')}")
    elif e["type"] == "price_set":
        meta.append("；".join(f"{d['title']} {d['old']} → {d['new']}" for d in items[:4]))
        if items:
            vurl += f"?variant={items[0]['vid']}"
    elif e["type"] == "restock":
        meta.append("；".join(f"{d['title']} {d['price']}" for d in items[:6]))
        meta.append("可售 是")
        if items:
            vurl += f"?variant={items[0]['vid']}"

    return {
        "type": e["type"],
        "label": EVENT_LABEL[e["type"]],
        "icon": EVENT_ICON[e["type"]],
        "title": clean_text(p.get("title")),
        "meta": meta,
        "url": vurl,
    }


def build_message(events: list[dict], cfg: dict) -> tuple[str, str]:
    """纯文本版本：邮件用。"""
    site = cfg["site"].rstrip("/")
    summary = summarize(events)
    lines = [
        f"【商品监控】{site.replace('https://', '')}",
        f"时间：{now_cn():%Y-%m-%d %H:%M}　共 {len(events)} 条（{summary}）",
        "",
    ]
    for e in events:
        v = event_view(e, cfg)
        lines.append(f"{v['icon']} {v['label']}｜{v['title']}")
        lines.append("   " + "　|　".join(v["meta"]))
        lines.append("   " + v["url"])
        lines.append("")
    return f"[商品监控] {summary}｜{now_cn():%m-%d %H:%M}", "\n".join(lines).rstrip()


def build_markdown(events: list[dict], cfg: dict) -> str:
    """Markdown 版本：企业微信 / PushPlus / Server酱 用。"""
    site = cfg["site"].rstrip("/").replace("https://", "")
    summary = summarize(events)
    parts = [
        f"**商品监控** · {site}",
        f'<font color="comment">{now_cn():%Y-%m-%d %H:%M} · 共 {len(events)} 条（{summary}）</font>',
    ]
    for e in events:
        v = event_view(e, cfg)
        parts.append(
            f"{v['icon']} **{v['label']}｜{v['title']}**\n"
            + "　".join(v["meta"])
            + f"\n[查看商品]({v['url']})"
        )
    return "\n\n".join(parts)


CARD_COLOR = {"restock": "red", "new_product": "blue", "price_set": "orange", "sold_out": "grey"}


def build_feishu_card(events: list[dict], cfg: dict, title_prefix: str = "商品监控") -> dict:
    """飞书交互卡片：标题按事件类型着色，每条带「去下单」按钮直达该 SKU。"""
    site = cfg["site"].rstrip("/").replace("https://", "")
    summary = summarize(events)

    seen: list[str] = []
    for e in events:
        if e["type"] not in seen:
            seen.append(e["type"])
    color = CARD_COLOR.get(seen[0], "blue") if seen else "blue"

    elements: list[dict] = []
    show = events[:8]
    for idx, e in enumerate(show):
        v = event_view(e, cfg)
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{v['icon']} {v['label']}｜{v['title']}**\n"
                    + " · ".join(v["meta"]),
                },
            }
        )
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看商品"},
                        "url": v["url"],
                        "type": "primary",
                    }
                ],
            }
        )
        if idx < len(show) - 1:
            elements.append({"tag": "hr"})

    if len(events) > len(show):
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"另有 {len(events) - len(show)} 条未在卡片中展示。",
                },
            }
        )

    elements.append(
        {
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": f"{now_cn():%Y-%m-%d %H:%M} · {site}"}
            ],
        }
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": f"{title_prefix} · {summary}"},
        },
        "elements": elements,
    }


# ----------------------------------------------------------------------------
# 命令实现
# ----------------------------------------------------------------------------
def cmd_list(args, cfg: dict) -> int:
    site = cfg["site"]
    locale = cfg.get("locale") or ""

    catalog = None
    if args.watchlist:
        mode, entries = plan_check(cfg, force_full=False)
        if mode == "targeted":
            print(f"定向读取监控列表中的 {len(entries)} 个 SKU：{site}")
            catalog = fetch_watchlist_catalog(cfg, entries)
    if catalog is None:
        print(f"拉取全站目录：{site}")
        catalog = fetch_catalog(site, page_delay=cfg.get("page_delay_seconds", 8))
    rows = list(catalog.values())

    if args.match:
        kw = args.match.lower()
        rows = [p for p in rows if kw in _haystack(p)]
    if args.variant:
        vid = str(args.variant).strip()
        rows = [p for p in rows if vid in variant_availability(p)]
    if args.watchlist:
        wv = watched_variants(cfg.get("filters", {}))
        if not wv:
            print("当前监控列表为空。用 `monitor.py add <商品链接>` 添加。")
            return 0
        rows = [p for p in rows if wv & set(variant_availability(p).keys())]
    if args.available:
        rows = [
            p
            for p in rows
            if any(v.get("available") for v in p.get("variants", []))
        ]

    rows.sort(key=lambda p: p.get("published_at") or "", reverse=True)
    if args.limit:
        rows = rows[: args.limit]

    print(f"\n共 {len(catalog)} 个商品，命中 {len(rows)} 个\n")
    for p in rows:
        va = variant_availability(p)
        pr = variant_prices(p)
        state = "在售" if any(va.values()) else "缺货"
        pub = (p.get("published_at") or "")[:16].replace("T", " ")
        print(f"[{state}] {p.get('title')}")
        print(f"  handle   {p.get('handle')}")
        print(f"  品类 {p.get('vendor') or '-'} / {p.get('product_type') or '-'}　上架 {pub}")
        for vid, av in va.items():
            mark = "在售" if av else "缺货"
            print(f"  变体 {vid}  {mark}  价格 {pr.get(vid, '-')}")
        print(f"  链接 {product_url(site, p.get('handle',''), locale)}")
        print()
    if rows:
        first = rows[0]
        vid = next(iter(variant_availability(first)))
        print(f"提示：把变体 ID 写入 config.json 的 filters.variants 即可精确监控，")
        print(f"      或直接执行：monitor.py add {product_url(site, first.get('handle',''), locale)}?variant={vid}")
    return 0


VARIANT_RE = re.compile(r"[?&]variant=(\d+)")


def _build_watch_entry(site: str, locale: str, data: dict, vid: str) -> dict:
    """从 /products/<handle>.js 的结果构造监控条目。该接口的 price 单位是「分」。"""
    info = {"p": 0.0, "a": False, "t": "Default Title"}
    for v in data.get("variants") or []:
        if str(v.get("id")) == vid:
            info = {
                "p": float(v.get("price") or 0) / 100.0,
                "a": bool(v.get("available")),
                "t": v.get("title") or "Default Title",
            }
            break
    handle = data.get("handle") or ""
    return {
        "vid": vid,
        "title": clean_text(data.get("title")) or handle,
        "handle": handle,
        "available": info["a"],
        "price": f"{info['p']:.2f}",
        "url": product_url(site, handle, locale) + f"?variant={vid}",
    }


def cmd_add(args, cfg: dict) -> int:
    """把商品链接（含 ?variant=）或裸变体 ID 加入监控列表。"""
    site = cfg["site"].rstrip("/")
    locale = cfg.get("locale") or ""
    filters = cfg.setdefault("filters", {})
    current = [str(v) for v in (filters.get("variants") or [])]

    added: list[dict] = []
    resolved: list[dict] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    bare_ids: list[str] = []

    for raw in args.targets:
        raw = raw.strip().strip('"').strip("'")
        if not raw:
            continue
        if raw.isdigit():
            bare_ids.append(raw)
            continue
        path = urllib.parse.unquote(urllib.parse.urlparse(raw).path)
        if "/products/" not in path:
            failed.append((raw, "链接里没有 /products/"))
            continue
        handle = path.split("/products/", 1)[1].split("/")[0].strip("/")
        m = VARIANT_RE.search(raw)
        vid = m.group(1) if m else ""
        try:
            url = f"{site}/{locale}/products/{urllib.parse.quote(handle)}.js"
            data = http_get_json(url, retries=2)
        except Exception as e:  # noqa: BLE001
            failed.append((raw, f"解析失败：{e}"))
            continue

        variants = data.get("variants") or []
        if vid:
            if not any(str(v.get("id")) == vid for v in variants):
                failed.append((raw, f"该商品下没有变体 {vid}"))
                continue
        else:
            pick = next((v for v in variants if v.get("available")), variants[0] if variants else None)
            if pick is None:
                failed.append((raw, "商品无变体"))
                continue
            vid = str(pick["id"])

        entry = _build_watch_entry(site, locale, data, vid)
        resolved.append(entry)
        if vid in current:
            skipped.append(f"{entry['title']}（{vid}）已在列表中")
        else:
            current.append(vid)
            added.append(entry)
        time.sleep(1.0)

    # 裸变体 ID 需要完整目录才能反查
    if bare_ids:
        print("按裸变体 ID 查找，需要拉取完整目录…", file=sys.stderr)
        catalog = fetch_catalog(site, page_delay=cfg.get("page_delay_seconds", 8))
        for vid in bare_ids:
            hit = None
            for p in catalog.values():
                if vid in variant_availability(p):
                    hit = p
                    break
            if hit is None:
                failed.append((vid, "目录中没有该变体"))
                continue
            if vid in current:
                skipped.append(f"{hit.get('title')}（{vid}）已在列表中")
                continue
            current.append(vid)
            entry = {
                "vid": vid,
                "title": hit.get("title"),
                "handle": hit.get("handle"),
                "available": variant_availability(hit)[vid],
                "price": variant_prices(hit).get(vid),
                "url": product_url(site, hit.get("handle", ""), locale) + f"?variant={vid}",
            }
            resolved.append(entry)
            added.append(entry)

    if resolved:
        wl = {str(e.get("vid")): e for e in (cfg.get("watchlist") or []) if e.get("vid")}
        for e in resolved:
            wl[str(e["vid"])] = {
                "vid": e["vid"],
                "handle": e["handle"],
                "title": e["title"],
                "url": e["url"],
            }
        cfg["watchlist"] = [wl[v] for v in current if v in wl]
        filters["variants"] = current
        save_json(CONFIG_PATH, cfg)

    if added:
        print(f"已加入监控（{len(added)} 个）：")
        for e in added:
            mark = "在售" if e["available"] else "缺货"
            print(f"  [{mark}] {e['title']}　价格 {e['price']}")
            print(f"         变体 {e['vid']}  {e['url']}")
    if skipped:
        print("已存在，未重复添加：")
        for s in skipped:
            print(f"  - {s}")
    if failed:
        print("以下条目未能加入：")
        for raw, why in failed:
            print(f"  ✗ {raw}\n      {why}")

    print(f"\n当前监控 {len(current)} 个变体：{', '.join(current)}")
    return 0 if not failed else 1


def normalize_js_product(data: dict) -> dict:
    """把 /products/<handle>.js 的结果转成与 products.json 一致的结构（price 由分转元）。"""
    variants = []
    for v in data.get("variants") or []:
        try:
            price = f"{float(v.get('price') or 0) / 100.0:.2f}"
        except (TypeError, ValueError):
            price = "0.00"
        variants.append(
            {
                "id": v.get("id"),
                "title": v.get("title") or "Default Title",
                "available": bool(v.get("available")),
                "price": price,
            }
        )
    return {
        "id": data.get("id"),
        "title": clean_text(data.get("title")),
        "handle": data.get("handle"),
        "vendor": data.get("vendor"),
        "product_type": data.get("type"),
        "tags": data.get("tags") or [],
        "published_at": data.get("published_at"),
        "variants": variants,
    }


def fetch_watchlist_catalog(cfg: dict, entries: list[dict]) -> dict:
    """定向模式：逐个拉取监控列表里的商品，1 个 SKU 1 次轻量请求，不触碰全站目录。"""
    site = cfg["site"].rstrip("/")
    locale = cfg.get("locale") or ""
    catalog: dict[str, dict] = {}
    for e in entries:
        handle = e.get("handle")
        if not handle:
            continue
        url = f"{site}/{locale}/products/{urllib.parse.quote(handle)}.js"
        try:
            data = http_get_json(url, retries=3)
        except Exception as ex:  # noqa: BLE001
            print(f"  [警告] 拉取失败：{e.get('title') or handle} —— {ex}", file=sys.stderr)
            continue
        p = normalize_js_product(data)
        catalog[str(p["id"])] = p
        time.sleep(1.2 + random.random())
    if not catalog:
        raise RuntimeError("监控列表全部拉取失败")
    return catalog


def update_state(state: dict | None, catalog: dict) -> dict:
    """合并写入状态：只更新本轮抓到的商品，其余保留（定向模式必需）。"""
    products = dict((state or {}).get("products") or {})
    for pid, p in catalog.items():
        products[pid] = {
            "handle": p.get("handle"),
            "title": p.get("title"),
            "published_at": p.get("published_at"),
            "variants": variant_availability(p),
            "prices": variant_prices(p),
        }
    return {
        "version": 2,
        "updated_at": now_cn().isoformat(timespec="seconds"),
        "products": products,
    }


def plan_check(cfg: dict, force_full: bool) -> tuple[str, list[dict] | None]:
    """决定用定向模式还是全量模式。"""
    wv = watched_variants(cfg.get("filters", {}))
    wl = {
        str(e.get("vid")): e
        for e in (cfg.get("watchlist") or [])
        if e.get("vid") and e.get("handle")
    }
    if not force_full and wv and wv.issubset(wl.keys()):
        return "targeted", [wl[v] for v in wv]
    return "full", None


def cmd_check(args, cfg: dict) -> int:
    site = cfg["site"]
    force_full = bool(getattr(args, "full", False))
    mode, entries = plan_check(cfg, force_full)
    print(f"[{now_cn():%Y-%m-%d %H:%M:%S}] 检查 {site}")
    if mode == "targeted":
        print(f"  定向模式：只检查监控列表中的 {len(entries)} 个 SKU（不拉全站目录）")
        catalog = fetch_watchlist_catalog(cfg, entries)
    else:
        print("  全量模式：拉取全站目录")
        catalog = fetch_catalog(site, page_delay=cfg.get("page_delay_seconds", 8))
    print(f"  本轮检查 {len(catalog)} 个商品")

    state = load_json(STATE_PATH, default=None)
    first_run = state is None or not state.get("products")

    if first_run:
        save_json(STATE_PATH, update_state(state, catalog))
        avail = sum(
            1 for p in catalog.values() if any(v.get("available") for v in p.get("variants", []))
        )
        print(
            f"  首次运行：已建立基线（{len(catalog)} 个商品，其中 {avail} 个有在售规格）。"
            f"\n  基线已写入 {STATE_PATH.name}，本轮不发送任何通知，避免历史商品刷屏。"
            f"\n  之后每次运行只推送「新品上架 / 定价开售 / 缺货→在售」。"
        )
        return 0

    events = diff(catalog, state, cfg)
    save_json(STATE_PATH, update_state(state, catalog))

    if not events:
        print("  无变化。")
        return 0

    subject, body = build_message(events, cfg)
    md = build_markdown(events, cfg)
    card = build_feishu_card(events, cfg)
    print("\n" + body + "\n")

    if args.dry_run:
        print("  [dry-run] 已跳过通知发送。Markdown 预览：\n")
        print(md)
        return 0

    sent = notify_all(cfg, subject, body, md, card)
    if sent == 0:
        print("  [警告] 未成功发出任何通知，请检查 config.json 的通知配置。", file=sys.stderr)
    return 0


def cmd_watch(args, cfg: dict) -> int:
    interval = int(args.interval or cfg.get("interval_seconds", 120))
    jitter = int(cfg.get("jitter_seconds", 20))
    print(f"常驻轮询启动：基础间隔 {interval}s + 随机抖动 {jitter}s，Ctrl+C 退出。")
    failures = 0
    while True:
        try:
            cmd_check(argparse.Namespace(dry_run=False, full=args.full), cfg)
            failures = 0
        except KeyboardInterrupt:
            print("\n已停止。")
            return 0
        except Exception as e:  # noqa: BLE001
            failures += 1
            backoff = min(600, 30 * failures)
            print(f"  [异常] {type(e).__name__}: {e}；{backoff}s 后重试", file=sys.stderr)
            time.sleep(backoff)
            continue
        time.sleep(interval + random.uniform(0, jitter))


def cmd_run(args, cfg: dict) -> int:
    """在固定时长内按固定间隔重复检查。

    用于 GitHub Actions：cron 最小粒度是 5 分钟，但一个 job 可以活 5 分钟，
    在里面每 60 秒查一次，等效频率就压到了 1 分钟。
    """
    duration = int(args.duration or 240)
    interval = int(args.interval or 60)
    started = time.monotonic()
    rounds = ok_rounds = 0
    last_err = None

    print(f"批量模式：时长 {duration}s，间隔 {interval}s（预计 {duration // max(interval, 1) + 1} 轮）")
    while True:
        rounds += 1
        loop_start = time.monotonic()
        try:
            cmd_check(argparse.Namespace(dry_run=False, full=getattr(args, "full", False)), cfg)
            ok_rounds += 1
            last_err = None
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            print(f"  [异常] 第 {rounds} 轮失败：{last_err}", file=sys.stderr)

        elapsed = time.monotonic() - started
        # 下一轮若会超出总时长就收尾，避免被 Actions 的 timeout 硬杀
        if elapsed + interval > duration:
            break
        sleep_s = interval - (time.monotonic() - loop_start)
        if sleep_s > 0:
            time.sleep(sleep_s)

    print(f"批量模式结束：共 {rounds} 轮，成功 {ok_rounds} 轮，用时 {time.monotonic() - started:.0f}s")
    if ok_rounds == 0:
        print(f"全部失败，最后错误：{last_err}", file=sys.stderr)
        return 1
    return 0


def cmd_test_notify(args, cfg: dict) -> int:
    subject = "[商品监控] 测试通知"
    body = (
        "这是一条测试通知。\n"
        f"时间：{now_cn():%Y-%m-%d %H:%M:%S}\n"
        f"监控站点：{cfg['site']}\n"
        "若你收到这条消息，说明通知通道已打通。"
    )
    md = (
        f"**商品监控 · 测试通知**\n\n"
        f'<font color="comment">{now_cn():%Y-%m-%d %H:%M:%S}</font>\n\n'
        f"站点：{cfg['site']}\n\n若你收到这条消息，说明该通道已打通。"
    )
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": "商品监控 · 连通性测试"},
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**通知通道已打通**\n收到这张卡片说明飞书 webhook 配置正确，"
                    "有商品补货时会以同样格式推送。",
                },
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"{now_cn():%Y-%m-%d %H:%M} · {cfg['site']}"}
                ],
            },
        ],
    }
    if notify_all(cfg, subject, body, md, card) == 0:
        print("没有任何通道发送成功，请检查 config.json。", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Shopify 商品上架/补货监控")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出/检索商品，用于确认监控目标")
    p_list.add_argument("--match", help="关键词过滤（匹配标题/厂商/品类/标签/handle）")
    p_list.add_argument("--variant", help="按变体 ID 精确查找")
    p_list.add_argument("--watchlist", action="store_true", help="只显示当前监控列表中的商品")
    p_list.add_argument("--available", action="store_true", help="只看当前在售")
    p_list.add_argument("--limit", type=int, default=0, help="最多显示条数")

    p_add = sub.add_parser("add", help="把商品链接（含 ?variant=）或变体 ID 加入监控列表")
    p_add.add_argument("targets", nargs="+", help="商品链接或变体 ID，可一次传多个")

    p_check = sub.add_parser("check", help="单次检查")
    p_check.add_argument("--dry-run", action="store_true", help="只打印不发通知")
    p_check.add_argument("--full", action="store_true", help="强制全量模式（默认按监控列表定向检查）")

    p_watch = sub.add_parser("watch", help="常驻轮询")
    p_watch.add_argument("--interval", type=int, help="轮询间隔秒数（覆盖配置）")
    p_watch.add_argument("--full", action="store_true", help="强制全量模式")

    p_run = sub.add_parser("run", help="在固定时长内重复检查（CI 用：把 5 分钟 cron 压成 1 分钟）")
    p_run.add_argument("--duration", type=int, default=240, help="总时长秒数，默认 240")
    p_run.add_argument("--interval", type=int, default=60, help="检查间隔秒数，默认 60")
    p_run.add_argument("--full", action="store_true", help="强制全量模式")

    sub.add_parser("test-notify", help="发送测试通知")

    args = parser.parse_args()
    cfg = load_json(CONFIG_PATH)
    if cfg is None:
        print(f"缺少配置文件：{CONFIG_PATH}", file=sys.stderr)
        return 1

    return {
        "list": cmd_list,
        "add": cmd_add,
        "check": cmd_check,
        "watch": cmd_watch,
        "run": cmd_run,
        "test-notify": cmd_test_notify,
    }[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
