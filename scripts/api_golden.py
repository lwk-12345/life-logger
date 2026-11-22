#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API Golden Baseline Collector (金丝雀基线采集器)
自动从 src/life_logger/local_web.py 解析全部 79 个 API 路由分支，
对 GET 路由带 60s 超时采集并归一化计算 hash；
对 POST/PUT 等写操作路由加安全护栏（只登记，不发起真实写入）。
"""

import re
import json
import time
import hashlib
import argparse
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_WEB_PATH = REPO_ROOT / "src/life_logger/local_web.py"
SECRETS_PATH = REPO_ROOT / "output/local-web/secrets.json"
BASE_URL = "http://127.0.0.1:8765"
REQUEST_TIMEOUT = 60.0  # 单个请求 60 秒硬上限
MAX_BODY_BYTES = 200 * 1024  # 200 KB 截断阈值

# 参数化路由种子数据 (选用真实稳定的历史样本，注释来源)
SEEDS = {
    # 日期种子：选择已有完整归档且离当前最近的基准日
    "date": "2026-10-21",
    "date_alt": "2026-10-22",
    "range_from": "2026-10-21",
    "range_to": "2026-10-22",
    # 搜索种子：选经典高频菜品关键词
    "search_q": quote("烧鸭饭"),
    # 稳定 Job ID 种子：选 output/local-web/jobs 下最早且已完成的真实 Job
    "job_id": "job-20260919-191308-de38be92",
    # 稳定 Watch 设备与会话：选 output/local-web/watch-sessions 下真实存在的稳定会话
    "device_id": "watch-d449943aa8c74004bf14e4be0d4643f7",
    "session_id": "890602D599EF44F3B7F57E7490C2DB45",
    "segment_seq": "0",
}

# 归一化规则：剔除随时间、运行时长、单次调用动态变化的易变字段
NORMALIZE_RULES = {
    # 1. 时间戳字段：随请求时刻动态变化
    "generated_at": "<normalized:timestamp>",
    "server_started_at": "<normalized:timestamp>",
    "now": "<normalized:timestamp>",
    "timestamp": "<normalized:timestamp>",
    "computed_at": "<normalized:timestamp>",
    "last_attempt": "<normalized:timestamp>",
    "mtime": "<normalized:timestamp>",
    "last_heartbeat": "<normalized:timestamp>",
    "last_activity": "<normalized:timestamp>",
    "last_sync": "<normalized:timestamp>",
    "check_timestamp": "<normalized:timestamp>",
    "current_time": "<normalized:timestamp>",
    "iso_timestamp": "<normalized:timestamp>",
    "heartbeat_at": "<normalized:timestamp>",
    # 2. 耗时与指标字段：随机器负载动态波动
    "elapsed": "<normalized:duration>",
    "elapsed_seconds": "<normalized:duration>",
    "elapsed_ms": "<normalized:duration>",
    "duration_ms": "<normalized:duration>",
    "uptime": "<normalized:duration>",
    "uptime_seconds": "<normalized:duration>",
    "query_duration_ms": "<normalized:duration>",
    "pid": "<normalized:pid>",
    # 3. 运行环境与指纹动态字段
    "code_fingerprint": "<normalized:fingerprint>",
    "git_commit": "<normalized:commit>",
    "cached": "<normalized:bool>",
    "sync_time": "<normalized:time>",
    "version_hash": "<normalized:hash>",
}

def normalize_data(data, normalized_keys_set=None):
    """递归归一化 JSON 数据，抹去易变字段"""
    if normalized_keys_set is None:
        normalized_keys_set = set()

    if isinstance(data, dict):
        new_dict = {}
        for k, v in data.items():
            if k in NORMALIZE_RULES:
                new_dict[k] = NORMALIZE_RULES[k]
                normalized_keys_set.add(k)
            elif re.search(r'(_ms|_seconds|_timestamp|_at)$', k) and isinstance(v, (int, float, str)):
                new_dict[k] = "<normalized:dynamic>"
                normalized_keys_set.add(k)
            else:
                new_dict[k] = normalize_data(v, normalized_keys_set)
        return new_dict
    elif isinstance(data, list):
        return [normalize_data(item, normalized_keys_set) for item in data]
    else:
        return data

def parse_all_routes_from_code(code_path):
    """
    自动从 local_web.py 源代码中解析全部 79 个路由分支，禁止手抄硬编码。
    返回结构：[{'method': 'GET'|'POST'|'PUT', 'raw_path': str, 'test_path': str, 'write_route': bool, 'line': int}]
    """
    content = code_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    routes = []
    curr_method = "UNKNOWN"

    for idx, l in enumerate(lines, 1):
        line = l.strip()
        if line.startswith("def do_GET"):
            curr_method = "GET"
        elif line.startswith("def do_POST"):
            curr_method = "POST"
        elif line.startswith("def do_PUT"):
            curr_method = "PUT"

        # 匹配 1: if parsed.path == "/api/..."
        m_eq = re.search(r'if parsed\.path == [\"\'](/api/[^\"\']+)[\"\']', line)
        if m_eq:
            p = m_eq.group(1)
            routes.append({
                "method": curr_method,
                "raw_path": p,
                "type": "exact",
                "line": idx,
                "write_route": (curr_method != "GET")
            })

        # 匹配 2: if parsed.path.startswith("/api/...")
        m_sw = re.search(r'if parsed\.path\.startswith\([\"\'](/api/[^\"\']+)[\"\']\)', line)
        if m_sw and m_sw.group(1) != "/api/":
            p = m_sw.group(1)
            # 检查是否有 endswith
            m_ew = re.search(r'endswith\([\"\']([^\"\']+)[\"\']\)', line)
            suffix = m_ew.group(1) if m_ew else ""
            routes.append({
                "method": curr_method,
                "raw_path": p + "{sub}" + suffix,
                "type": "prefix",
                "line": idx,
                "write_route": (curr_method != "GET")
            })

        # 匹配 3: parts 路由分支 (如 /api/jobs/... 与 /api/watch-sessions/...)
        if 'and parts[3] ==' in line:
            m_p3 = re.search(r'and parts\[3\] == [\"\']([^\"\']+)[\"\']', line)
            if m_p3:
                routes.append({
                    "method": curr_method,
                    "raw_path": f"/api/jobs/{{job_id}}/{m_p3.group(1)}",
                    "type": "subpath",
                    "line": idx,
                    "write_route": (curr_method != "GET")
                })
        elif 'and parts[4] ==' in line:
            m_p4 = re.search(r'and parts\[4\] == [\"\']([^\"\']+)[\"\']', line)
            if m_p4:
                routes.append({
                    "method": curr_method,
                    "raw_path": f"/api/watch-sessions/{{device_id}}/{{session_id}}/{m_p4.group(1)}",
                    "type": "subpath",
                    "line": idx,
                    "write_route": (curr_method != "GET")
                })

    return routes

def resolve_test_query(method, raw_path):
    """为 GET 路由注入固定参数种子"""
    if method != "GET":
        return raw_path

    # 参数化映射
    if "{job_id}" in raw_path:
        p = raw_path.replace("{job_id}", SEEDS["job_id"])
        if p.endswith("/artifact"):
            p += "?path=status.json"
        elif p.endswith("/audio") or p.endswith("/audio-clip"):
            p += "?path=audio.m4a"
        return p
    if "{sub}" in raw_path:
        p = raw_path.replace("{sub}", SEEDS["job_id"])
        if p.endswith("/artifact"):
            p += "?path=status.json"
        elif p.endswith("/audio") or p.endswith("/audio-clip"):
            p += "?path=audio.m4a"
        return p

    # 特殊必须参数路由
    if raw_path == "/api/expense-range":
        return f"/api/expense-range?from={SEEDS['range_from']}&to={SEEDS['range_to']}"
    if raw_path in {"/api/events/search", "/api/search"}:
        return f"{raw_path}?q={SEEDS['search_q']}"
    if raw_path in {"/api/daily", "/api/daily-brief", "/api/daily-health", "/api/dashboard/daily", "/api/timeline-scenes", "/api/quick-captures", "/api/watch-live-events", "/api/capture-funnel"}:
        return f"{raw_path}?date={SEEDS['date']}"
    if raw_path == "/api/stats":
        return f"/api/stats?from={SEEDS['range_from']}&to={SEEDS['range_to']}"
    if raw_path == "/api/passport-sessions/audio":
        return f"{raw_path}?session={SEEDS['session_id']}"

    return raw_path

def slugify(text):
    return re.sub(r'[^a-zA-Z0-9_\-]+', '_', text).strip('_')

def main():
    parser = argparse.ArgumentParser(description="API Golden Baseline Collector")
    parser.add_argument("--out", required=True, help="Output directory (e.g. golden/before-20261022)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = REPO_ROOT / out_dir
    bodies_dir = out_dir / "bodies"
    bodies_dir.mkdir(parents=True, exist_ok=True)

    token = ""
    if SECRETS_PATH.exists():
        try:
            token = json.loads(SECRETS_PATH.read_text(encoding="utf-8")).get("mobile_access_token", "")
        except Exception:
            pass

    print("=== 开始 API 金丝雀基线采集 ===")
    print(f"目标目录: {out_dir}")
    print(f"解析源码: {LOCAL_WEB_PATH}")

    parsed_routes = parse_all_routes_from_code(LOCAL_WEB_PATH)
    print(f"共自动解析到 {len(parsed_routes)} 个路由分支。")

    manifest = {
        "label": out_dir.name,
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "total_routes": len(parsed_routes),
        "timeout_count": 0,
        "get_count": sum(1 for r in parsed_routes if r["method"] == "GET"),
        "write_count": sum(1 for r in parsed_routes if r["write_route"]),
        "routes": [],
    }

    timeouts = []

    for idx, item in enumerate(parsed_routes, 1):
        method = item["method"]
        raw_path = item["raw_path"]
        is_write = item["write_route"]
        test_url_path = resolve_test_query(method, raw_path)
        slug = f"{idx:02d}_{method}_{slugify(raw_path)}"

        route_record = {
            "index": idx,
            "method": method,
            "raw_path": raw_path,
            "request_path": test_url_path,
            "write_route": is_write,
            "status": None,
            "content_type": None,
            "bytes": 0,
            "elapsed_seconds": 0.0,
            "sha256": None,
            "truncated": False,
            "normalized_keys": [],
            "body_file": None,
            "error": None
        }

        if is_write:
            # 写操作安全护栏：绝不发起网络写入请求！
            route_record["status"] = "uncalled_write_guard"
            route_record["sha256"] = "write_guard_uncalled"
            print(f"[{idx}/{len(parsed_routes)}] 🛡️  {method:4s} {raw_path:45s} -> WRITE_GUARD (未调用)")
            manifest["routes"].append(route_record)
            continue

        # 发起真实 GET 请求，带 60s 硬超时
        req_url = BASE_URL + test_url_path
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = Request(req_url, headers=headers)

        t0 = time.time()
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                elapsed = time.time() - t0
                status_code = resp.status
                content_type = resp.headers.get("Content-Type", "")
                raw_bytes = resp.read()

                route_record["status"] = status_code
                route_record["content_type"] = content_type
                route_record["bytes"] = len(raw_bytes)
                route_record["elapsed_seconds"] = round(elapsed, 3)

                # 尝试解析 JSON 并归一化
                normalized_keys = set()
                try:
                    text_content = raw_bytes.decode("utf-8")
                    json_data = json.loads(text_content)
                    normalized_obj = normalize_data(json_data, normalized_keys)
                    norm_text = json.dumps(normalized_obj, sort_keys=True, separators=(",", ":"))
                    route_record["sha256"] = hashlib.sha256(norm_text.encode("utf-8")).hexdigest()
                    route_record["normalized_keys"] = sorted(list(normalized_keys))

                    # 保存归一化后的响应体
                    body_path = bodies_dir / f"{slug}.json"
                    if len(norm_text) > MAX_BODY_BYTES:
                        body_path.write_text(norm_text[:MAX_BODY_BYTES] + "\n...[TRUNCATED]", encoding="utf-8")
                        route_record["truncated"] = True
                    else:
                        body_path.write_text(norm_text, encoding="utf-8")
                    route_record["body_file"] = str(body_path.relative_to(out_dir))

                except (UnicodeDecodeError, json.JSONDecodeError):
                    # 非 JSON 响应（如二进制 zip、图片、纯文本）
                    route_record["sha256"] = hashlib.sha256(raw_bytes).hexdigest()
                    body_path = bodies_dir / f"{slug}.bin"
                    body_path.write_bytes(raw_bytes[:MAX_BODY_BYTES])
                    route_record["body_file"] = str(body_path.relative_to(out_dir))

                print(f"[{idx}/{len(parsed_routes)}] ✅ {method:4s} {test_url_path:45s} -> {status_code} ({elapsed:.2f}s, {len(raw_bytes)}B)")

        except HTTPError as e:
            elapsed = time.time() - t0
            raw_err = e.read()
            route_record["status"] = e.code
            route_record["bytes"] = len(raw_err)
            route_record["elapsed_seconds"] = round(elapsed, 3)
            route_record["sha256"] = hashlib.sha256(raw_err).hexdigest()
            print(f"[{idx}/{len(parsed_routes)}] ⚠️  {method:4s} {test_url_path:45s} -> HTTP {e.code} ({elapsed:.2f}s)")

        except (URLError, TimeoutError, OSError) as e:
            elapsed = time.time() - t0
            route_record["status"] = "timeout"
            route_record["elapsed_seconds"] = round(elapsed, 3)
            route_record["error"] = str(e)
            manifest["timeout_count"] += 1
            timeouts.append(test_url_path)
            print(f"[{idx}/{len(parsed_routes)}] ❌ {method:4s} {test_url_path:45s} -> TIMEOUT ({elapsed:.2f}s)")

        manifest["routes"].append(route_record)

    manifest["timeout_routes"] = timeouts
    manifest_file = out_dir / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n========================================================")
    print(f"🎉 采集完成！清单已输出至: {manifest_file}")
    print(f"总分支数: {manifest['total_routes']} | GET: {manifest['get_count']} | 写护栏: {manifest['write_count']}")
    print(f"成功/响应数: {manifest['get_count'] - manifest['timeout_count']} | 超时数: {manifest['timeout_count']}")
    if timeouts:
        print("超时路由清单:")
        for t in timeouts:
            print(f"  - {t}")
    print("========================================================")

if __name__ == "__main__":
    main()
