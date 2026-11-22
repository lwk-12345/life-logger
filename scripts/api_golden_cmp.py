#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API Golden Baseline Comparator (金丝雀基线比对器)
比对两份基线快照的 manifest.json 与 bodies/*.json
输出三态表：一致 (MATCH) / 内容变了 (CONTENT_DIFF) / 协议变了 (SCHEMA_DIFF)
退出码：0 全一致 / 1 有内容变 / 2 有协议变
"""

import sys
import json
import argparse
from pathlib import Path

def find_diff_paths(obj1, obj2, prefix=""):
    """递归找出两个对象之间不同的字段路径，最多返回 3 个"""
    diffs = []
    if isinstance(obj1, dict) and isinstance(obj2, dict):
        all_keys = sorted(set(obj1.keys()) | set(obj2.keys()))
        for k in all_keys:
            p = f"{prefix}.{k}" if prefix else k
            if k not in obj1:
                diffs.append(f"+{p}")
            elif k not in obj2:
                diffs.append(f"-{p}")
            elif obj1[k] != obj2[k]:
                if isinstance(obj1[k], (dict, list)) and isinstance(obj2[k], (dict, list)):
                    sub = find_diff_paths(obj1[k], obj2[k], p)
                    diffs.extend(sub)
                else:
                    diffs.append(f"{p} ({obj1[k]!r} != {obj2[k]!r})")
            if len(diffs) >= 3:
                return diffs[:3]
    elif isinstance(obj1, list) and isinstance(obj2, list):
        if len(obj1) != len(obj2):
            diffs.append(f"{prefix}.len ({len(obj1)} != {len(obj2)})")
        for i in range(min(len(obj1), len(obj2))):
            p = f"{prefix}[{i}]"
            if obj1[i] != obj2[i]:
                sub = find_diff_paths(obj1[i], obj2[i], p)
                diffs.extend(sub)
            if len(diffs) >= 3:
                return diffs[:3]
    else:
        diffs.append(f"{prefix} ({obj1!r} != {obj2!r})")
    return diffs[:3]

def main():
    parser = argparse.ArgumentParser(description="API Golden Comparator")
    parser.add_argument("--base", required=True, help="Base baseline directory")
    parser.add_argument("--target", required=True, help="Target baseline directory")
    parser.add_argument("--ignore-route", action="append", default=[], help="Routes to ignore")
    args = parser.parse_args()

    base_dir = Path(args.base)
    target_dir = Path(args.target)
    ignore_set = set(args.ignore_route)

    base_manifest_file = base_dir / "manifest.json"
    target_manifest_file = target_dir / "manifest.json"

    if not base_manifest_file.exists():
        print(f"❌ Base manifest 不存在: {base_manifest_file}", file=sys.stderr)
        sys.exit(2)
    if not target_manifest_file.exists():
        print(f"❌ Target manifest 不存在: {target_manifest_file}", file=sys.stderr)
        sys.exit(2)

    base_man = json.loads(base_manifest_file.read_text(encoding="utf-8"))
    target_man = json.loads(target_manifest_file.read_text(encoding="utf-8"))

    base_routes = { (r["method"], r["raw_path"]): r for r in base_man.get("routes", []) }
    target_routes = { (r["method"], r["raw_path"]): r for r in target_man.get("routes", []) }

    all_keys = sorted(set(base_routes.keys()) | set(target_routes.keys()))

    results = []
    has_schema_diff = False
    has_content_diff = False

    for method, path in all_keys:
        r_base = base_routes.get((method, path))
        r_target = target_routes.get((method, path))

        if path in ignore_set or any(path.startswith(ig) for ig in ignore_set):
            results.append({
                "method": method, "path": path, "status_base": r_base.get("status") if r_base else None,
                "status_target": r_target.get("status") if r_target else None,
                "verdict": "IGNORED", "diff": "命令行已配置忽略"
            })
            continue

        if not r_base:
            has_schema_diff = True
            results.append({
                "method": method, "path": path, "status_base": None,
                "status_target": r_target.get("status"), "verdict": "SCHEMA_DIFF", "diff": "Base 中缺失此路由"
            })
            continue

        if not r_target:
            has_schema_diff = True
            results.append({
                "method": method, "path": path, "status_base": r_base.get("status"),
                "status_target": None, "verdict": "SCHEMA_DIFF", "diff": "Target 中缺失此路由"
            })
            continue

        s_base = r_base.get("status")
        s_target = r_target.get("status")

        # 状态码不一致 -> 协议变了
        if s_base != s_target:
            has_schema_diff = True
            results.append({
                "method": method, "path": path, "status_base": s_base,
                "status_target": s_target, "verdict": "SCHEMA_DIFF", "diff": f"状态码改变: {s_base} -> {s_target}"
            })
            continue

        # 写路由护栏
        if r_base.get("write_route") and r_target.get("write_route"):
            results.append({
                "method": method, "path": path, "status_base": s_base,
                "status_target": s_target, "verdict": "MATCH", "diff": "写路由护栏(未调用)"
            })
            continue

        # 两边都是超时
        if s_base == "timeout" and s_target == "timeout":
            results.append({
                "method": method, "path": path, "status_base": "timeout",
                "status_target": "timeout", "verdict": "MATCH", "diff": "两边均超时(同源挂起)"
            })
            continue

        # 对比 SHA256
        sha_base = r_base.get("sha256")
        sha_target = r_target.get("sha256")

        if sha_base == sha_target:
            results.append({
                "method": method, "path": path, "status_base": s_base,
                "status_target": s_target, "verdict": "MATCH", "diff": "完全一致"
            })
        else:
            has_content_diff = True
            # 读取具体 body 文件找出前 3 个 diff 字段
            diff_detail = "SHA256不一致"
            bf_base = r_base.get("body_file")
            bf_target = r_target.get("body_file")
            if bf_base and bf_target:
                p_b = base_dir / bf_base
                p_t = target_dir / bf_target
                if p_b.exists() and p_t.exists() and p_b.suffix == ".json" and p_t.suffix == ".json":
                    try:
                        d1 = json.loads(p_b.read_text(encoding="utf-8"))
                        d2 = json.loads(p_t.read_text(encoding="utf-8"))
                        paths = find_diff_paths(d1, d2)
                        if paths:
                            diff_detail = " | ".join(paths[:3])
                    except Exception:
                        pass
            results.append({
                "method": method, "path": path, "status_base": s_base,
                "status_target": s_target, "verdict": "CONTENT_DIFF", "diff": diff_detail
            })

    print("==========================================================================================================")
    print("📊 API 金丝雀快照比对结果")
    print(f"Base:   {base_dir.name} ({base_man.get('collected_at')})")
    print(f"Target: {target_dir.name} ({target_man.get('collected_at')})")
    print("==========================================================================================================")
    print(f"{'序号':4s} | {'方法':4s} | {'路由':42s} | {'Base':6s} | {'Target':6s} | {'比对结论':12s} | {'差异说明'}")
    print("-----|------|--------------------------------------------|--------|--------|--------------|----------------")

    for i, r in enumerate(results, 1):
        v = r['verdict']
        v_str = "✅ MATCH" if v == "MATCH" else ("⚠️ CONTENT_DIFF" if v == "CONTENT_DIFF" else f"❌ {v}")
        print(f"{i:4d} | {r['method']:4s} | {r['path'][:42]:42s} | {str(r['status_base'])[:6]:6s} | {str(r['status_target'])[:6]:6s} | {v_str:14s} | {r['diff']}")

    matches_count = sum(1 for r in results if r["verdict"] == "MATCH")
    content_count = sum(1 for r in results if r["verdict"] == "CONTENT_DIFF")
    schema_count = sum(1 for r in results if r["verdict"] == "SCHEMA_DIFF")
    ignored_count = sum(1 for r in results if r["verdict"] == "IGNORED")

    print("==========================================================================================================")
    print(f"汇总: 总计 {len(results)} 条 | 一致: {matches_count} | 内容变动: {content_count} | 协议变动: {schema_count} | 忽略: {ignored_count}")
    print("==========================================================================================================")

    if has_schema_diff:
        print("❌ 存在协议变动 (SCHEMA_DIFF)，退出码 2")
        sys.exit(2)
    elif has_content_diff:
        print("⚠️  存在内容变动 (CONTENT_DIFF)，退出码 1")
        sys.exit(1)
    else:
        print("🎉 全部一致 (ALL MATCH)，退出码 0")
        sys.exit(0)

if __name__ == "__main__":
    main()
