"""Redraw archived charts and stock documentation from local CSV files only."""

from __future__ import annotations

import argparse
import csv
import json
import warnings
from pathlib import Path

from .archive import Archive, ArchiveError, Client, iso_date, plot_minutes, price_limits, read_minutes


def render_date(root: Path, path: Path) -> int:
    day_info = json.loads(path.read_text(encoding="utf-8"))
    folder = path.parent
    with (folder / "signals.csv").open(encoding="utf-8-sig", newline="") as stream:
        records = list(csv.DictReader(stream))
    # The document renderer needs no credentials and never invokes this client.
    documents = Archive(root, Client("", ""), {})
    rendered = 0
    for stock in day_info["stocks"]:
        stock_folder = folder / stock["folder"]
        info = json.loads((stock_folder / "metadata.json").read_text(encoding="utf-8"))
        signal_ids = set(info["signal_ids"])
        selected = [record for record in records if record["id"] in signal_ids]
        if len(selected) != len(signal_ids):
            raise ArchiveError(f"{folder.name}/{stock['folder']} 的信号清单不一致")
        available = {}
        for entry in info.get("days", []):
            if not entry["rows"]:
                continue
            day = entry["date"]
            rows = read_minutes(stock_folder / f"{day}.csv", day)
            if len(rows) != entry["rows"]:
                raise ArchiveError(f"{folder.name}/{stock['folder']}/{day} 的行情条数不一致")
            available[day] = rows
        if available:
            limits = price_limits(available, info["buy_points"])
            for day, rows in available.items():
                plot_minutes(stock_folder / f"{day}.png", rows, day, info["code"], info["buy_points"], limits, stock_name=" / ".join(info["names"]))
                rendered += 1
        documents.stock_readme(stock_folder, selected, info)
    return rendered


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="使用已有 CSV 重画中文分时图，不访问外部接口")
    parser.add_argument("--output", type=Path, default=Path.cwd(), help="项目根目录，默认当前目录")
    parser.add_argument("--date", help="仅重画一个出票日期 YYYY-MM-DD；默认处理全部已归档日期")
    args = parser.parse_args(argv)
    try:
        root = args.output.resolve()
        if args.date:
            paths = [root / "data" / iso_date(args.date) / "metadata.json"]
        else:
            paths = sorted((root / "data").glob("????-??-??/metadata.json"))
        if not paths or any(not path.is_file() for path in paths):
            raise ArchiveError("没有找到所选日期的本地归档")
        total = 0
        with warnings.catch_warnings():
            warnings.filterwarnings("error", message=r"Glyph .* missing from font")
            for index, path in enumerate(paths, 1):
                count = render_date(root, path)
                total += count
                print(f"[{index}/{len(paths)}] {path.parent.name}：已重画 {count} 张图片", flush=True)
        print(json.dumps({"dates": len(paths), "charts": total, "source": "local_csv"}, ensure_ascii=False), flush=True)
        return 0
    except (ArchiveError, OSError, ValueError, KeyError, UserWarning) as error:
        message = str(error) if isinstance(error, ArchiveError) else "本地数据或中文字体校验失败，重画未完成"
        print(f"错误：{message}", flush=True)
        return 1
    except KeyboardInterrupt:
        print("重画已中断；重新运行可继续生成。", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
