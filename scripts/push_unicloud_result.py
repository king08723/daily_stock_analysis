#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 reports/*.md 同步到：
1) GitHub analysis-results 分支（Actions 必达，供 uniCloud/前端中转）
2) uniCloud get-stock-result（尽力而为；海外 Runner 常连不上）
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


def find_report(symbol: str, reports_dir: Path) -> Path:
    if not reports_dir.is_dir():
        raise FileNotFoundError("reports 目录不存在: %s" % reports_dir)

    files = sorted(reports_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    print("reports 目录内容:")
    for p in files:
        print("  - %s (%d bytes)" % (p.name, p.stat().st_size))
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

    seen = set()
    ordered = []
    for p in preferred:
        key = str(p.resolve())
        if key not in seen:
            seen.add(key)
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
        return 'success":true' in raw.replace(" ", "")


def http_json(method: str, url: str, payload: Optional[dict] = None, headers: Optional[dict] = None, timeout: int = 60) -> Tuple[int, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    hdrs = {"User-Agent": "daily-stock-analysis-unicloud-push/3.0", "Accept": "application/vnd.github+json"}
    if headers:
        hdrs.update(headers)
    if data is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw


def ensure_branch(repo: str, branch: str, token: str) -> None:
    # 已存在则跳过
    code, _ = http_json("GET", "https://api.github.com/repos/%s/git/ref/heads/%s" % (repo, branch), headers={"Authorization": "Bearer %s" % token})
    if code == 200:
        print("branch exists: %s" % branch)
        return
    code, main_ref = http_json("GET", "https://api.github.com/repos/%s/git/ref/heads/main" % repo, headers={"Authorization": "Bearer %s" % token})
    if code != 200:
        raise RuntimeError("无法读取 main: %s %s" % (code, main_ref))
    sha = main_ref["object"]["sha"]
    code, created = http_json(
        "POST",
        "https://api.github.com/repos/%s/git/refs" % repo,
        payload={"ref": "refs/heads/%s" % branch, "sha": sha},
        headers={"Authorization": "Bearer %s" % token},
    )
    if code not in (200, 201):
        raise RuntimeError("创建分支失败: %s %s" % (code, created))
    print("created branch: %s" % branch)


def push_github_bridge(repo: str, branch: str, symbol: str, payload: dict, token: str) -> str:
    ensure_branch(repo, branch, token)
    path = "bridge/%s.json" % symbol
    api_url = "https://api.github.com/repos/%s/contents/%s" % (repo, path)
    headers = {"Authorization": "Bearer %s" % token}
    code, existing = http_json("GET", api_url + "?ref=" + branch, headers=headers)
    body = {
        "message": "analysis result: %s" % symbol,
        "content": base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if code == 200 and isinstance(existing, dict) and existing.get("sha"):
        body["sha"] = existing["sha"]
    code2, res = http_json("PUT", api_url, payload=body, headers=headers)
    if code2 not in (200, 201):
        raise RuntimeError("GitHub bridge 写入失败: %s %s" % (code2, res))
    raw_url = "https://raw.githubusercontent.com/%s/%s/%s" % (repo, branch, path)
    cdn_url = "https://cdn.jsdelivr.net/gh/%s@%s/%s" % (repo, branch, path)
    print("✅ GitHub bridge 已写入: %s" % path)
    print("   raw: %s" % raw_url)
    print("   cdn: %s" % cdn_url)
    return cdn_url


def push_unicloud(url: str, payload: dict, attempts: int = 3) -> bool:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ctx = ssl.create_default_context()
    last_err = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "daily-stock-analysis-unicloud-push/3.0",
                "Origin": "https://nhm.net.cn",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
                raw = resp.read().decode("utf-8", "replace")
            print(raw[:1500])
            if not parse_success(raw):
                raise RuntimeError("uniCloud 返回未成功")
            print("✅ uniCloud 报告推送成功")
            return True
        except Exception as e:
            last_err = e
            print("uniCloud attempt %d/%d failed: %s: %s" % (attempt, attempts, type(e).__name__, e))
            time.sleep(min(2 ** attempt, 15))
    print("⚠️ uniCloud 推送失败（将依赖 GitHub bridge）: %s" % last_err)
    return False


def main() -> int:
    symbol = (os.environ.get("SYMBOL") or os.environ.get("STOCK_SYMBOL") or "00700.HK").strip().upper()
    url = os.environ.get("UNICLOUD_RESULT_URL") or "https://f.nhm.net.cn/get-stock-result"
    reports_dir = Path(os.environ.get("REPORTS_DIR") or "reports")
    repo = os.environ.get("GITHUB_REPOSITORY") or "king08723/daily_stock_analysis"
    branch = os.environ.get("RESULT_BRANCH") or "analysis-results"
    token = os.environ.get("GITHUB_TOKEN") or ""

    print("=" * 42)
    print("🌐 同步分析报告: %s" % symbol)
    print("=" * 42)

    result_path = Path("result.json")
    if result_path.is_file():
        print("使用已有 result.json")
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payload["symbol"] = payload.get("symbol") or symbol
    else:
        report_path = find_report(symbol, reports_dir)
        report = report_path.read_text(encoding="utf-8")
        if not report.strip():
            raise SystemExit("报告文件为空: %s" % report_path)
        print("使用报告文件: %s (len=%d)" % (report_path, len(report)))
        payload = {
            "symbol": symbol,
            "report": report,
            "candles": [],
            "cvd": [],
            "markers": [],
            "updatedAt": int(time.time() * 1000),
        }
        result_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    if "updatedAt" not in payload:
        payload["updatedAt"] = int(time.time() * 1000)

    github_ok = False
    if token:
        try:
            push_github_bridge(repo, branch, symbol, payload, token)
            github_ok = True
        except Exception as e:
            print("❌ GitHub bridge 失败: %s: %s" % (type(e).__name__, e))
    else:
        print("⚠️ 无 GITHUB_TOKEN，跳过 GitHub bridge")

    unicloud_ok = push_unicloud(url, payload)

    if github_ok or unicloud_ok:
        print("✅ 同步完成 github=%s unicloud=%s" % (github_ok, unicloud_ok))
        return 0
    raise SystemExit("❌ GitHub bridge 与 uniCloud 均失败")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        print("❌ %s: %s" % (type(e).__name__, e), file=sys.stderr)
        raise SystemExit(1)
