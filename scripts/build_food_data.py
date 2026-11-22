#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""编译四层食物查询链的本地数据文件（一次性构建，可重跑）。

输入（都在仓库外，属原始下载物，不入库）：
  ① 中国食物成分表（第6版）JSON：``Sanotsu/china-food-composition-data``
     ``json_data_v3_20260825_qwen38max_kimi_k3_fixed/merged_*.json``
     —— 默认读 ``~/.hermes/cache/scratch/cfct_json/``
  ② foodwake 中文食物表：``LuckyHookin/foodwake`` 单文件 JSONL（1643 条）
     —— 默认读 ``~/.hermes/cache/scratch/foodwake.json``
  ③ USDA 精选：``pyfooda`` 随包数据 ``data/fooddata.csv``（390381 行，64MB）
     —— 只挑非品牌（sr_legacy_food / foundation_food）的通用食物，压成小 JSON

输出（写入 ``output/local-web/food-data/``，已被 .gitignore 的 ``output/*`` 覆盖）：
  ``cfct.json`` ``foodwake.json`` ``usda-subset.json``

为什么要在构建期压缩：运行期（8765 长跑服务）不能依赖 pandas / pyfooda / 64MB CSV，
也不该在第一次查询时卡 1.2 秒读大文件。压缩后每个文件都在几百 KB，``json.load`` 毫秒级。
"""
from __future__ import annotations

import argparse
import csv
import glob
import importlib.resources
import json
import os
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "output" / "local-web" / "food-data"
DEFAULT_CFCT_DIR = Path.home() / ".hermes" / "cache" / "scratch" / "cfct_json"
DEFAULT_FOODWAKE = Path.home() / ".hermes" / "cache" / "scratch" / "foodwake.json"

# 成分表里的「无 / 微量 / 未检出」占位符：一律当缺失，不猜 0。
_MISSING_TOKENS = {"", "-", "--", "—", "－", "tr", "trace", "un", "未检出", "微量", "无", "nan", "null"}


def _num(value: Any) -> float | None:
    """把成分表里的字符串数字转成 float；占位符/非数字一律 None。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if f == f else None  # 过滤 NaN
    text = str(value).strip()
    if text.lower() in _MISSING_TOKENS:
        return None
    text = text.replace(",", "").replace("，", "")
    match = re.match(r"^-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _per_100g(
    energy: Any,
    protein: Any,
    fat: Any,
    carbs: Any,
    sodium: Any,
    *,
    energy_unit_divisor: float = 1.0,
) -> dict[str, float]:
    values = {
        "energy_kcal": _num(energy),
        "protein_g": _num(protein),
        "fat_g": _num(fat),
        "carbohydrates_g": _num(carbs),
        "sodium_mg": _num(sodium),
    }
    if energy_unit_divisor != 1.0 and values["energy_kcal"] is not None:
        values["energy_kcal"] = values["energy_kcal"] / energy_unit_divisor
    # [重要] 能量缺失的条目直接判废：查询层拿不到热量就等于没命中，
    # 留着只会在匹配时把更靠谱的同名条目挤掉。
    if values["energy_kcal"] is None:
        return {}
    return {key: round(float(value), 2) for key, value in values.items() if value is not None}


def build_cfct(src_dir: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    files = sorted(glob.glob(str(src_dir / "merged_*.json")))
    if not files:
        raise SystemExit(f"CFCT 源目录里没有 merged_*.json：{src_dir}")
    for path in files:
        category = os.path.basename(path).removeprefix("merged_").removesuffix(".json")
        for row in json.loads(Path(path).read_text(encoding="utf-8")):
            name = str(row.get("foodName") or "").strip()
            if not name:
                continue
            per_100g = _per_100g(
                row.get("energyKCal"), row.get("protein"), row.get("fat"), row.get("CHO"), row.get("Na")
            )
            if not per_100g:
                continue
            entry: dict[str, Any] = {
                "name": name,
                "per_100g": per_100g,
                "category": category,
            }
            code = str(row.get("foodCode") or "").strip()
            if code:
                entry["code"] = code
            remark = str(row.get("remark") or "").strip()
            if remark:
                entry["remark"] = remark
            entries.append(entry)
    return {"source": "cfct", "dataset": "中国食物成分表（第6版）", "entries": entries}


def build_foodwake(src: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for line in Path(src).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(row.get("name") or "").strip()
        info = row.get("info") or {}
        if not name or not isinstance(info, dict):
            continue
        # foodwake 的中文单位：能量「千卡」/ 蛋白「克」/ 钠「毫克」。
        # 任一项缺数字时字符串只剩单位（如「克」）→ _num 判 None。
        per_100g = _per_100g(
            info.get("能量"), info.get("蛋白质"), info.get("脂肪"), info.get("碳水化合物"), info.get("钠")
        )
        if not per_100g:
            continue
        entry: dict[str, Any] = {
            "name": name,
            "per_100g": per_100g,
            "category": str(row.get("type") or "").strip(),
        }
        nickname = str(row.get("nickname") or "").strip()
        if nickname and nickname != "无":
            entry["alias"] = nickname
        entries.append(entry)
    return {"source": "foodwake", "dataset": "foodwake 中文食物成分（1643 条）", "entries": entries}


def _pyfooda_csv() -> Path:
    try:
        path = Path(str(importlib.resources.files("pyfooda").joinpath("data", "fooddata.csv")))
    except Exception:  # pragma: no cover - 只在没装 pyfooda 时走到
        path = Path()
    if path.is_file():
        return path
    for candidate in Path(REPO_ROOT / ".venv" / "lib").glob("python*/site-packages/pyfooda/data/fooddata.csv"):
        return candidate
    raise SystemExit("找不到 pyfooda 的 data/fooddata.csv（先在 .venv 里 uv pip install pyfooda）")


def build_usda(
    csv_path: Path,
    *,
    keep_types: tuple[str, ...] = (
        "sr_legacy_food",
        "foundation_food",
        "survey_fndds_food",
    ),
) -> dict[str, Any]:
    """只留通用食物，品牌食品交给 OFF 那一层。

    保留 SR Legacy / Foundation（原始成分条目）+ survey FNDDS（「作为吃下去的形态」
    的调查条目，如 ``Chicken breast, sauteed, skin not eaten``）。
    **不保留 branded_food**（381600 条美国超市商品，才是 64MB CSV 的主体）——
    那类需求按架构走第 ④ 层 Open Food Facts，查中文品牌更准。
    """
    entries: list[dict[str, Any]] = []
    with Path(csv_path).open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            data_type = str(row.get("data_type") or "").strip()
            if data_type not in keep_types:
                continue
            name = str(row.get("foodName") or "").strip()
            if not name:
                continue
            per_100g = _per_100g(
                row.get("Energy"), row.get("Protein"), row.get("Total fat"), row.get("Carbohydrate"), row.get("Sodium")
            )
            if not per_100g:
                continue
            entry: dict[str, Any] = {"name": name, "per_100g": per_100g, "data_type": data_type}
            portion = _num(row.get("portion_gram_weight"))
            unit = str(row.get("portion_unit_name") or "").strip()
            if portion and portion > 0:
                entry["serving_g"] = round(portion, 1)
                if unit and unit.lower() != "undetermined":
                    entry["serving_label"] = unit
            category = str(row.get("food_category") or "").strip()
            if category:
                entry["category"] = category
            entries.append(entry)
    return {"source": "usda", "dataset": "USDA FoodData Central（SR Legacy / Foundation 精选）", "entries": entries}


def _write(path: Path, payload: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    size = path.stat().st_size
    print(f"  {path.name}: {len(payload['entries'])} 条 / {size/1024/1024:.2f} MB")
    return size


def main() -> int:
    parser = argparse.ArgumentParser(description="编译四层食物查询链的本地数据")
    parser.add_argument("--cfct-dir", default=str(DEFAULT_CFCT_DIR))
    parser.add_argument("--foodwake", default=str(DEFAULT_FOODWAKE))
    parser.add_argument("--usda-csv", default="")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--skip-usda", action="store_true")
    args = parser.parse_args()

    out = Path(args.out)
    print(f"输出目录：{out}")
    total = 0
    total += _write(out / "cfct.json", build_cfct(Path(args.cfct_dir)))
    total += _write(out / "foodwake.json", build_foodwake(Path(args.foodwake)))
    if not args.skip_usda:
        csv_path = Path(args.usda_csv) if args.usda_csv else _pyfooda_csv()
        print(f"USDA 源：{csv_path}")
        total += _write(out / "usda-subset.json", build_usda(csv_path))
    print(f"合计 {total/1024/1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
