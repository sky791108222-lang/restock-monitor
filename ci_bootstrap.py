#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在 CI / 服务器环境中，由 config.example.json + 环境变量生成 config.json。

这样真实的飞书 Webhook 只存在于 GitHub Secrets 或服务器环境变量里，
公开仓库中不会留下可被滥用的凭据。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

SRC = pathlib.Path("config.example.json")
DST = pathlib.Path("config.json")


def main() -> int:
    if not SRC.exists():
        print(f"缺少模板：{SRC}", file=sys.stderr)
        return 1

    cfg = json.loads(SRC.read_text(encoding="utf-8"))
    notify = cfg.setdefault("notify", {})

    hook = (os.environ.get("FEISHU_WEBHOOK") or "").strip()
    if hook:
        feishu = notify.setdefault("feishu", {})
        feishu["enabled"] = True
        feishu["webhook"] = hook
        feishu["secret"] = (os.environ.get("FEISHU_SECRET") or "").strip()
        print("飞书通道：已由环境变量注入")
    else:
        print("飞书通道：未提供 FEISHU_WEBHOOK，保持模板默认（关闭）")

    if os.environ.get("MONITOR_INTERVAL"):
        try:
            cfg["interval_seconds"] = int(os.environ["MONITOR_INTERVAL"])
        except ValueError:
            print("MONITOR_INTERVAL 不是整数，已忽略", file=sys.stderr)

    DST.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    enabled = [k for k, v in notify.items() if isinstance(v, dict) and v.get("enabled")]
    print(f"config.json 已生成，启用通道：{enabled or '无'}")
    print(f"监控 SKU 数：{len(cfg.get('filters', {}).get('variants') or [])}")

    if not enabled:
        print(
            "错误：没有任何通知通道被启用，本次运行无法推送。\n"
            "请设置仓库 Secret：FEISHU_WEBHOOK（飞书机器人 Webhook 地址）。",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
