#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 reports/*.md 推送到 uniCloud get-stock-result。

GitHub Actions runner 上 curl 访问 f.nhm.net.cn 常 TLS 失败（exit 35），
因此必须用 urllib，并带重试。
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def find_report(symbol: str, reports_dir: Path) -> Path:
    if not reports_dir.is_dir():
        raise FileNotFoundError(f"reports 目录不存在: {reports_dir}")

    files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    print("reports 目录内容:")
    for p in files:
        print(f"  - {p.name} ({p.stat().st_size} bytes)")
    if not files:
        raise FileNotFoundError("reports 下没有 .md 文件")

    code_key = symbol.replace(".", "").replace("-", "")
    preferred = []
    for p in files:
        name = p.name.upper()
        if symbol.upper() in name or code_key.upper() in name:
            preferred.append(p)
    for p in files:
        if p.name.startswith("report_"):
            preferred.append(p)
    preferred.extend(files)

    # 去重保序
    seen = set()
    ordered = []
    for p in preferred:
        if p.resolve() not in seen:
            seen.add(p.resolve())
            ordered.append(p)
    return ordered[0]


def parse_success(raw: str) -> bool:
    try:
        outer = json.loads(raw)
        body = outer.get("body", outer)
        if isinstance(body, str):
            body = json.loads(body)
        return isinstance(body, dict) and body.get("success") is True
    except Exception:
        compact = raw.replace(" ", "")
        return 'success":true' in compact or "success':true" in compact


def push(url: str, payload: dict, attempts: int = 5) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ctx = ssl.create_default_context()
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "daily-stock-analysis-unicloud-push/2.0",
                "Origin": "https://nhm.net.cn",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
            print(raw[:2000])
            if not parse_success(raw):
                raise RuntimeError("uniCloud 返回未成功")
            print("✅ uniCloud 报告推送成功")
            return
        except Exception as e:  # noqa: BLE001 - 需要汇总重试
            last_err = e
            print(f"attempt {attempt}/{attempts} failed: {type(e).__name__}: {e}")
            time.sleep(min(2 ** attempt, 20))
    raise SystemExit(f"❌ uniCloud 推送失败: {last_err}")


def main() -> int:
    symbol = (os.environ.get("SYMBOL") or os.environ.get("STOCK_SYMBOL") or "00700.HK").strip().upper()
    url = os.environ.get("UNICLOUD_RESULT_URL") or "https://f.nhm.net.cn/get-stock-result"
    reports_dir = Path(os.environ.get("REPORTS_DIR") or "reports")

    print("=" * 42)
    print(f"🌐 推送分析报告至 uniCloud: {symbol}")
    print("=" * 42)

    result_path = Path("result.json")
    if result_path.is_file():
        print("使用已有 result.json 上报")
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not payload.get("symbol"):
            payload["symbol"] = symbol
    else:
        report_path = find_report(symbol, reports_dir)
        report = report_path.read_text(encoding="utf-8")
        if not report.strip():
            raise SystemExit(f"报告文件为空: {report_path}")
        print(f"使用报告文件: {report_path} (len={len(report)})")
        payload = {
            "symbol": symbol,
            "report": report,
            "candles": [],
            "cvd": [],
            "markers": [],
        }
        result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    push(url, payload)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"❌ {type(e).__name__}: {e}", file=sys.stderr)
        raise SystemExit(1)
