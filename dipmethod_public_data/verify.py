"""Check exported records, market files, hashes and documentation links offline."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

from .archive import ArchiveError, MINUTE_FIELDS, SIGNAL_FIELDS, digest, read_minutes


def verify(root: Path, require_all: bool = False) -> dict:
    errors = []
    counts = Counter()
    exported = {}
    source_path = root / ".cache" / "signals-latest.json"
    source = None
    if source_path.exists():
        source = {row["id"]: row for row in json.loads(source_path.read_text())["signals"]}
    for path in sorted((root / "data").glob("????-??-??/signals.csv")):
        if not (path.parent / "metadata.json").exists():
            counts["unfinished_dates"] += 1
            continue
        counts["dates"] += 1
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != SIGNAL_FIELDS:
                errors.append(f"{path.relative_to(root)}: 出票字段不匹配")
            rows = list(reader)
        for row in rows:
            identifier = row["id"]
            if identifier in exported or row["sig_date"] != path.parent.name:
                errors.append(f"{identifier}: 重复信号或日期不匹配")
            exported[identifier] = row
            if source is not None:
                expected = source.get(identifier)
                if expected is None:
                    errors.append(f"{identifier}: 不在最近读取的源数据中")
                else:
                    for field in SIGNAL_FIELDS:
                        value = row[field]
                        same = float(value) == float(expected[field]) if field in {"seq", "cost", "peak", "peak_return"} else value == ("" if expected[field] is None else str(expected[field]))
                        if not same:
                            errors.append(f"{identifier}: {field} 与最近读取的源数据不一致")
        info = json.loads((path.parent / "metadata.json").read_text())
        if info["signal_count"] != len(rows):
            errors.append(f"{path.parent.name}: 元数据出票数量不一致")
        stock_ids = {identifier for stock in info["stocks"] for identifier in stock["signal_ids"]}
        if stock_ids != {row["id"] for row in rows}:
            errors.append(f"{path.parent.name}: 个股文档和当天清单的信号不一致")
        for stock in info["stocks"]:
            folder = path.parent / stock["folder"]
            metadata = json.loads((folder / "metadata.json").read_text())
            counts[f"stock_{metadata['status']}"] += 1
            for entry in metadata.get("days", []):
                counts[f"day_{entry['status']}"] += 1
                if not entry["rows"]:
                    continue
                csv_path = folder / f"{entry['date']}.csv"
                png_path = folder / f"{entry['date']}.png"
                if not csv_path.exists() or not png_path.exists():
                    errors.append(f"{folder.relative_to(root)}: 缺少 CSV 或分时图")
                    continue
                try:
                    market = read_minutes(csv_path, entry["date"])
                    if len(market) != entry["rows"]:
                        errors.append(f"{csv_path.relative_to(root)}: 行情条数不一致")
                    if entry.get("sha256") != digest(csv_path.read_bytes()):
                        errors.append(f"{csv_path.relative_to(root)}: SHA256 不一致")
                    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
                        if next(csv.reader(stream)) != MINUTE_FIELDS:
                            errors.append(f"{csv_path.relative_to(root)}: 行情字段不匹配")
                    counts["minute_rows"] += len(market)
                    counts["minute_csv_files"] += 1
                    counts["chart_files"] += 1
                except (ArchiveError, ValueError):
                    errors.append(f"{csv_path.relative_to(root)}: 行情格式无效")
    if require_all:
        if source is None:
            errors.append("缺少本地源数据快照，不能检查全量覆盖")
        elif set(exported) != set(source):
            errors.append(f"全量覆盖不一致：未导出 {len(set(source) - set(exported))} 条，多出 {len(set(exported) - set(source))} 条")
    for path in [root / "README.md", *(root / "data").rglob("README.md")]:
        if not path.exists():
            errors.append(f"缺少 {path.relative_to(root)}")
            continue
        for link in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", path.read_text()):
            if link.startswith(("https://", "http://", "#")):
                continue
            if not (path.parent / link.split("#", 1)[0]).exists():
                errors.append(f"{path.relative_to(root)}: 链接不存在 {link}")
        counts["readme_files"] += 1
    counts["signals"] = len(exported)
    return {"counts": dict(counts), "source_reconciled": source is not None, "errors": errors}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="离线核对归档文件、字段、校验值和文档链接")
    parser.add_argument("--output", type=Path, default=Path.cwd())
    parser.add_argument("--require-all", action="store_true", help="检查是否完整导出本地最新源快照中的全部信号")
    args = parser.parse_args(argv)
    try:
        result = verify(args.output.resolve(), args.require_all)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result["errors"] else 0
    except (ValueError, KeyError, OSError):
        print("归档文件缺失或格式无效，核对未完成。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
