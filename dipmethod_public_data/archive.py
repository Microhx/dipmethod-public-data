"""Fetch public records and market evidence; never infer trades or returns."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CHINA = ZoneInfo("Asia/Shanghai")
SIGNALS_URL = "https://dipmethod.com/api/signals"
SIGNAL_FIELDS = ["id", "seq", "sig_date", "code", "name", "cost", "peak", "peak_return"]
MINUTE_FIELDS = ["t", "o", "h", "l", "c", "v", "a", "pc", "sf"]
EXCHANGES = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
CODE = re.compile(r"\d{6}\.(SH|SZ|BJ)\Z")
STATUS = {
    "complete": "分钟标签齐全", "partial": "行情不完整", "empty": "暂无行情",
    "pending": "待补充", "error": "获取失败", "missing_code": "缺少股票代码",
    "calendar_error": "交易日历不完整",
}


class ArchiveError(Exception):
    """A safe message that never contains credentials or a request URL."""


def now_iso() -> str:
    return datetime.now(CHINA).isoformat(timespec="seconds")


def iso_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ArchiveError("日期必须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ArchiveError("日期无效") from None


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def write_json(path: Path, value) -> None:
    atomic_write(path, json_bytes(value))


def csv_bytes(rows: list[dict], fields: list[str]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: "" if row.get(field) is None else row.get(field) for field in fields})
    return buffer.getvalue().encode("utf-8-sig")


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def preserve_csv(root: Path, path: Path, content: bytes, reason: str) -> bool:
    """Preserve the replaced CSV, and a content-addressed correction entry."""
    if path.exists():
        old = path.read_bytes()
        if old == content:
            return False
        relative = path.relative_to(root)
        old_hash = digest(old)
        previous = root / "corrections" / "previous" / relative.parent / f"{path.stem}-{old_hash[:16]}.csv"
        if not previous.exists():
            atomic_write(previous, old)
        entry = {
            "recorded_at": now_iso(), "file": relative.as_posix(), "reason": reason,
            "previous_file": previous.relative_to(root).as_posix(),
            "previous_sha256": old_hash, "current_sha256": digest(content),
        }
        log = root / "corrections" / "changes.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    atomic_write(path, content)
    return True


def load_env(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.removeprefix("export ").split("=", 1)
            if key.strip() in {"ZHITU_TOKEN", "TUSHARE_TOKEN"}:
                values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ["ZHITU_TOKEN", "TUSHARE_TOKEN"]:
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def postman_token(path: Path) -> str:
    collection = json.loads(path.read_text(encoding="utf-8-sig"))

    def walk(items):
        for item in items:
            if "item" in item:
                yield from walk(item["item"])
            elif item.get("name") == "历史分时交易（Pro版）":
                url = item.get("request", {}).get("url", "")
                raw = url if isinstance(url, str) else url.get("raw", "")
                parsed = urllib.parse.urlsplit(raw)
                if parsed.hostname in {"api.zhituapi.com", "p.zhituapi.com"} and parsed.path.startswith("/hs/pro/history/"):
                    yield dict(urllib.parse.parse_qsl(parsed.query)).get("token", "")

    token = next((token for token in walk(collection.get("item", [])) if token), None)
    if not token:
        raise ArchiveError("Postman 文件中找不到沪深 Pro 历史行情凭证")
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, zhitu_token: str, tushare_token: str, interval: float = 1.1):
        self.zhitu_token = zhitu_token
        self.tushare_token = tushare_token
        self.interval = max(1.0, interval)
        self.last_market_request = 0.0
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url: str, body=None):
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": "dipmethod-public-data/0.1 (+https://dipmethod.com)"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            try:
                with self.opener.open(urllib.request.Request(url, data=encoded, headers=headers), timeout=30) as response:
                    raw = response.read(12_000_001)
                if len(raw) > 12_000_000:
                    raise ArchiveError("上游响应超过大小限制")
                try:
                    return json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ArchiveError("上游响应不是有效 JSON") from None
            except urllib.error.HTTPError as error:
                body_text = error.read(4000).decode("utf-8", errors="replace")
                if "108:" in body_text:
                    raise ArchiveError("智兔返回 108：API Pro 扩能包未开通或已过期") from None
                if (error.code == 429 or error.code >= 500) and attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ArchiveError(f"上游请求失败（HTTP {error.code}）") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ArchiveError("上游网络请求失败或超时") from None
        raise ArchiveError("上游请求失败")

    def signals(self) -> list[dict]:
        return validate_signals(self.request(SIGNALS_URL))

    def calendar(self, exchange: str, start: str, end: str) -> list[dict]:
        if not self.tushare_token:
            raise ArchiveError("请在本地配置 TUSHARE_TOKEN，用于确定下一交易日")
        data = self.request("https://api.tushare.pro", {
            "api_name": "trade_cal", "token": self.tushare_token,
            "params": {"exchange": exchange, "start_date": start.replace("-", ""), "end_date": end.replace("-", "")},
            "fields": "cal_date,is_open",
        })
        if not isinstance(data, dict) or data.get("code") != 0 or not isinstance(data.get("data"), dict):
            raise ArchiveError("交易日历接口拒绝请求，请检查权限或调用频率")
        payload = data["data"]
        fields = payload.get("fields", [])
        if "cal_date" not in fields or "is_open" not in fields or not isinstance(payload.get("items"), list):
            raise ArchiveError("交易日历缺少必要字段")
        rows = []
        for item in payload["items"]:
            raw_day = str(item[fields.index("cal_date")])
            day = iso_date(f"{raw_day[:4]}-{raw_day[4:6]}-{raw_day[6:8]}")
            if not start <= day <= end:
                raise ArchiveError("交易日历返回日期超出请求范围")
            opened = item[fields.index("is_open")]
            if opened not in {0, 1, "0", "1"}:
                raise ArchiveError("交易日历的开市状态无效")
            rows.append({"date": day, "is_open": int(opened)})
        if len({row["date"] for row in rows}) != len(rows):
            raise ArchiveError("交易日历包含重复日期")
        return sorted(rows, key=lambda row: row["date"])

    def minutes(self, code: str, start: str, end: str) -> list[dict]:
        if not self.zhitu_token:
            raise ArchiveError("请在本地配置 ZHITU_TOKEN（需要 API Pro 扩能包权限）")
        if not CODE.fullmatch(code):
            raise ArchiveError("股票代码格式无效")
        delay = self.interval - (time.monotonic() - self.last_market_request)
        if delay > 0:
            time.sleep(delay)
        market = "bj" if code.endswith(".BJ") else "hs"
        query = urllib.parse.urlencode({"st": start.replace("-", "") + "000000", "et": end.replace("-", "") + "235959", "token": self.zhitu_token})
        self.last_market_request = time.monotonic()
        payload = self.request(f"https://api.zhituapi.com/{market}/pro/history/{code}/1/n?{query}")
        return validate_minutes(payload, start, end)


def validate_signals(payload) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("signals"), list):
        raise ArchiveError("出票接口缺少 signals 数组")
    rows = []
    seen = set()
    for item in payload["signals"]:
        if not isinstance(item, dict) or any(field not in item for field in SIGNAL_FIELDS):
            raise ArchiveError("出票记录缺少必要字段")
        row = {field: item[field] for field in SIGNAL_FIELDS}
        row["sig_date"] = iso_date(row["sig_date"])
        if not isinstance(row["id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", row["id"]) or row["id"] in seen:
            raise ArchiveError("信号 ID 无效或重复")
        seen.add(row["id"])
        if row["code"] is not None and (not isinstance(row["code"], str) or (row["code"] and not CODE.fullmatch(row["code"]))):
            raise ArchiveError("出票记录中的股票代码无效")
        if not isinstance(row["name"], str) or not row["name"]:
            raise ArchiveError("出票记录缺少股票名称")
        if not isinstance(row["seq"], int) or isinstance(row["seq"], bool):
            raise ArchiveError("出票序号无效")
        for field in ["cost", "peak", "peak_return"]:
            if isinstance(row[field], bool) or not isinstance(row[field], (int, float)) or not math.isfinite(row[field]):
                raise ArchiveError("出票记录包含无效数值")
        if row["cost"] <= 0 or row["peak"] <= 0:
            raise ArchiveError("出票价格必须大于零")
        rows.append(row)
    return sorted(rows, key=lambda row: (row["sig_date"], row["seq"], row["id"]))


def validate_minutes(payload, start: str, end: str) -> list[dict]:
    if not isinstance(payload, list):
        raise ArchiveError("行情接口未返回分钟数组，请检查授权及请求频率")
    if len(payload) > 20000:
        raise ArchiveError("分钟行情超出请求范围")
    seen, rows = set(), []
    for item in payload:
        if not isinstance(item, dict) or any(field not in item for field in MINUTE_FIELDS[:7]):
            raise ArchiveError("分钟行情缺少必要字段")
        stamp = item["t"]
        try:
            parsed = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            raise ArchiveError("分钟行情时间格式无效") from None
        if parsed.strftime("%Y-%m-%d %H:%M:%S") != stamp or not start <= stamp[:10] <= end or parsed.second != 0:
            raise ArchiveError("分钟行情时间超出范围或不是分钟标签")
        if stamp in seen:
            raise ArchiveError("分钟行情包含重复时间标签")
        seen.add(stamp)
        row = {field: item.get(field) for field in MINUTE_FIELDS}
        for field in ["o", "h", "l", "c", "v", "a"]:
            if isinstance(row[field], bool) or not isinstance(row[field], (int, float)) or not math.isfinite(row[field]):
                raise ArchiveError("分钟行情包含无效价格或成交量")
            if row[field] < 0 or (field in {"o", "h", "l", "c"} and row[field] == 0):
                raise ArchiveError("分钟行情包含非正价格或负成交量")
        if row["h"] < max(row["o"], row["l"], row["c"]) or row["l"] > min(row["o"], row["h"], row["c"]):
            raise ArchiveError("分钟行情的最高最低价关系异常")
        for field in ["pc", "sf"]:
            if row[field] is not None and (isinstance(row[field], bool) or not isinstance(row[field], (int, float)) or not math.isfinite(row[field])):
                raise ArchiveError("分钟行情附带字段无效")
        rows.append(row)
    return sorted(rows, key=lambda row: row["t"])


def read_minutes(path: Path, day: str) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for field in MINUTE_FIELDS[1:]:
            row[field] = float(row[field]) if row.get(field) not in {None, ""} else None
    return validate_minutes(rows, day, day)


def expected_labels(day: str) -> set[str]:
    result = set()
    for start, end in [("09:31", "11:30"), ("13:01", "15:00")]:
        current = datetime.fromisoformat(f"{day} {start}")
        stop = datetime.fromisoformat(f"{day} {end}")
        while current <= stop:
            result.add(current.strftime("%Y-%m-%d %H:%M:%S"))
            current += timedelta(minutes=1)
    return result


def inspect_minutes(rows: list[dict], day: str, now: datetime) -> dict:
    labels = {row["t"] for row in rows}
    expected = expected_labels(day)
    missing = sorted(expected - labels)
    extra = sorted(labels - expected - {f"{day} 09:30:00"})
    closed = now >= datetime.combine(date.fromisoformat(day), day_time(15, 15), CHINA)
    status = "pending" if not closed else "empty" if not rows else "complete" if not missing and not extra else "partial"
    return {"date": day, "status": status, "rows": len(rows), "missing_labels": missing, "unexpected_labels": extra}


def next_session(day: str, calendar: list[dict]) -> str:
    by_day = {row["date"]: row["is_open"] for row in calendar}
    if by_day.get(day) != 1:
        raise ArchiveError("出票日期缺少开市日历，不能推断下一交易日")
    current = date.fromisoformat(day) + timedelta(days=1)
    for _ in range(45):
        stamp = current.isoformat()
        if stamp not in by_day:
            raise ArchiveError("交易日历存在缺口，不能推断下一交易日")
        if by_day[stamp] == 1:
            return stamp
        current += timedelta(days=1)
    raise ArchiveError("交易日历中没有找到下一交易日")


def md(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\r", " ").replace("\n", " ")


def plot_minutes(path: Path, rows: list[dict], day: str, code: str, costs: list[float], limits: tuple[float, float], stock_name: str = "") -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    font_path = None
    for family in ["PingFang SC", "PingFang HK", "Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC", "Heiti TC", "Arial Unicode MS", "SimHei"]:
        candidate = next((entry.fname for entry in font_manager.fontManager.ttflist if entry.name == family and Path(entry.fname).is_file()), None)
        if candidate:
            font_path = candidate
            break
    if font_path is None:
        raise ArchiveError("缺少中文字体，请安装思源黑体或 Noto Sans CJK SC 后重新绘图")
    font = font_manager.FontProperties(fname=font_path)
    legend_font = font_manager.FontProperties(fname=font_path, size=9)

    def position(stamp: str) -> float:
        hour, minute = map(int, stamp[11:16].split(":"))
        at = hour * 60 + minute
        return at - 570 if at <= 690 else 120 + at - 780

    fig, ax = plt.subplots(figsize=(12, 4.8), dpi=140)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#ffffff")
    # Separate morning and afternoon; do not interpolate the lunch break.
    sessions = [[row for row in rows if row["t"][11:16] <= "11:30"], [row for row in rows if row["t"][11:16] >= "13:00"]]
    segments = []
    for session in sessions:
        segment = []
        for row in session:
            if segment and position(row["t"]) - position(segment[-1]["t"]) > 1:
                segments.append(segment)
                segment = []
            segment.append(row)
        if segment:
            segments.append(segment)
    for i, segment in enumerate(segments):
        if not segment:
            continue
        # NaN gaps prevent an apparently continuous price line across missing minutes.
        xs, ys, previous = [], [], None
        for row in segment:
            at = position(row["t"])
            if previous is not None and at - previous > 1:
                xs.append(float("nan")); ys.append(float("nan"))
            xs.append(at); ys.append(row["c"]); previous = at
        line, = ax.plot(xs, ys, color="#c43d32", linewidth=1.3, label="分钟收盘价" if i == 0 else None)
        line.get_path().should_simplify = False
    for cost in sorted(set(costs)):
        ax.axhline(cost, color="#20834b", linestyle="--", linewidth=1.1, label=f"记录买点：{cost} 元")
    ax.axvline(120, color="#c9ccce", linestyle=":", linewidth=.8)
    ax.set(xlim=(0, 240), ylim=limits)
    title_name = f"{stock_name}（{code}）" if stock_name else code
    ax.set_title(f"{title_name}  |  {day}  |  一分钟分时 · 不复权", fontproperties=font, fontsize=14, pad=13)
    ax.set_ylabel("价格（元）", fontproperties=font, fontsize=11)
    ax.set_xticks([0, 60, 120, 180, 240], ["09:30", "10:30", "11:30 / 13:00", "14:00", "15:00"])
    ax.grid(axis="y", color="#e4e5e2", linewidth=.7)
    for spine in ["top", "right"]:
        ax.spines[spine].set_visible(False)
    ax.legend(loc="best", prop=legend_font, facecolor="white", edgecolor="#d8d8d8", framealpha=1)
    fig.text(.08, .025, "数据来源：智兔数服  |  北京时间  |  记录买点仅作价格参照，不代表实际成交", fontproperties=font, fontsize=9, color="#626b70")
    fig.tight_layout(rect=(0, .05, 1, 1))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.tmp.png")
    try:
        fig.savefig(temporary, metadata={"Software": "dipmethod-public-data"})
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
        plt.close(fig)


def price_limits(data: dict[str, list[dict]], costs: list[float]) -> tuple[float, float]:
    values = [float(value) for value in costs]
    values.extend(row[field] for rows in data.values() for row in rows for field in ["l", "h"])
    low, high = min(values), max(values)
    padding = max((high - low) * .08, high * .005)
    return max(0, low - padding), high + padding


class Archive:
    def __init__(self, root: Path, client: Client, calendars: dict[str, list[dict]], refresh: bool = False, now: datetime | None = None):
        self.root, self.client, self.calendars, self.refresh = root, client, calendars, refresh
        self.now = now or datetime.now(CHINA)
        self.failures = 0
        self.statuses: dict[str, int] = defaultdict(int)

    def cached(self, code: str, day: str) -> tuple[list[dict], dict] | None:
        folder = self.root / ".cache" / "minutes" / code
        source = folder / f"{day}.json"
        if self.refresh or not source.exists():
            return None
        try:
            cached = json.loads(source.read_text())
            rows = validate_minutes(cached["rows"], day, day)
            info = inspect_minutes(rows, day, self.now)
            if info["status"] != "complete":
                return None
            return rows, cached["source"]
        except (ValueError, KeyError, ArchiveError, OSError):
            return None

    def stock(self, day: str, records: list[dict], folder: Path) -> dict:
        code = records[0]["code"]
        costs = [row["cost"] for row in records]
        info = {"code": code, "names": sorted({row["name"] for row in records}), "signal_ids": [row["id"] for row in records], "buy_points": costs, "signal_date": day, "days": []}
        old_metadata = {}
        if (folder / "metadata.json").exists():
            old_metadata = json.loads((folder / "metadata.json").read_text())
        if not code:
            info["status"] = "missing_code"
            self.failures += 1
        else:
            try:
                following = next_session(day, self.calendars[EXCHANGES[code[-2:]]])
            except (ArchiveError, KeyError):
                following = None
                info["calendar_note"] = "无法可靠确定下一交易日，未使用工作日估算"
                self.failures += 1
            info["next_trade_date"] = following
            days = [day] + ([following] if following else [])
            rows_by_day, sources, errors = {}, {}, {}
            needed = []
            for target in days:
                cached = self.cached(code, target)
                if cached:
                    rows_by_day[target], sources[target] = cached
                elif target <= self.now.date().isoformat():
                    needed.append(target)
                else:
                    rows_by_day[target] = []
            if needed:
                try:
                    rows = self.client.minutes(code, min(needed), max(needed))
                    fetched_at = now_iso()
                    for target in needed:
                        rows_by_day[target] = [row for row in rows if row["t"].startswith(target)]
                        sources[target] = {"provider": "智兔 API Pro", "fetched_at": fetched_at, "period": "1m", "adjustment": "n", "timezone": "Asia/Shanghai"}
                        write_json(self.root / ".cache" / "minutes" / code / f"{target}.json", {"rows": rows_by_day[target], "source": sources[target]})
                except ArchiveError as error:
                    for target in needed:
                        errors[target] = str(error)
                        path = folder / f"{target}.csv"
                        if path.exists():
                            rows_by_day[target] = read_minutes(path, target)
                            previous = next((entry for entry in old_metadata.get("days", []) if entry.get("date") == target), {})
                            sources[target] = previous.get("source", {})
                        else:
                            rows_by_day[target] = []
            limits = price_limits(rows_by_day, costs)
            for target in days:
                rows = rows_by_day.get(target, [])
                checked = inspect_minutes(rows, target, self.now)
                if target in errors:
                    checked.update(status="error", error=errors[target], attempted_at=now_iso())
                checked["source"] = sources.get(target)
                if rows:
                    csv_path = folder / f"{target}.csv"
                    if target in errors and csv_path.exists():
                        content = csv_path.read_bytes()
                    else:
                        content = csv_bytes(rows, MINUTE_FIELDS)
                        preserve_csv(self.root, csv_path, content, "行情源返回数据更新")
                    checked["sha256"] = digest(content)
                    plot_minutes(folder / f"{target}.png", rows, target, code, costs, limits, stock_name=" / ".join(info["names"]))
                elif (folder / f"{target}.csv").exists() and target not in errors:
                    # An upstream retraction is a correction, not a silently stale success.
                    content = csv_bytes([], MINUTE_FIELDS)
                    preserve_csv(self.root, folder / f"{target}.csv", content, "行情源原有记录变为空数据")
                    (folder / f"{target}.png").unlink(missing_ok=True)
                if checked["status"] in {"error", "empty", "partial"}:
                    self.failures += 1
                self.statuses[checked["status"]] += 1
                info["days"].append(checked)
            info["status"] = "calendar_error" if not following else "complete" if all(entry["status"] == "complete" for entry in info["days"]) else "pending" if all(entry["status"] in {"complete", "pending"} for entry in info["days"]) else "partial"
        info["first_archived_at"] = old_metadata.get("first_archived_at", now_iso())
        write_json(folder / "metadata.json", info)
        self.stock_readme(folder, records, info)
        return info

    def stock_readme(self, folder: Path, records: list[dict], info: dict) -> None:
        code = info["code"] or "代码缺失"
        lines = [f"# {md(' / '.join(info['names']))} · {code}", "", f"出票日期：{info['signal_date']} · [返回当天清单](../README.md)", "", "| 信号 ID | 网站记录买点（元） |", "| --- | ---: |"]
        lines += [f"| {row['id']} | {row['cost']} |" for row in records]
        lines += ["", "记录买点为网站原值，不代表实际成交。图中绿色虚线只作为价格参照。", ""]
        if not info["code"]:
            lines += ["原始记录缺少股票代码，保留原始名称和买点，未猜测代码或请求行情。", ""]
        if info.get("calendar_note"):
            lines += [info["calendar_note"], ""]
        for i, entry in enumerate(info["days"]):
            target = entry["date"]
            label = "出票当日" if i == 0 else "下一交易日"
            lines += [f"## {label} · {target}", "", f"状态：**{STATUS[entry['status']]}**；{entry['rows']} 条分钟记录。", ""]
            if entry.get("error"):
                lines += [f"{entry['error']}。已有数据如存在则保留，未将请求失败解释为停牌。", ""]
            if entry["rows"]:
                lines += [f"![{target} 分时图]({target}.png)", "", f"[下载一分钟 CSV]({target}.csv)", ""]
            if entry["missing_labels"] and entry["status"] != "pending":
                lines += [f"缺少 {len(entry['missing_labels'])} 个常规分钟标签；具体列表见 [metadata.json](metadata.json)。未补值或插值。", ""]
            if entry["unexpected_labels"]:
                lines += ["包含常规时间范围外的标签，见 metadata.json，需进一步核对。", ""]
            if entry.get("source"):
                lines += [f"数据来源：智兔 API Pro；采集时间：{entry['source'].get('fetched_at', '未知')}；北京时间、不复权。", ""]
        lines += ["## 说明", "", "分时图的红线表示分钟收盘价，绿色虚线表示记录买点；两张图使用相同价格坐标范围。缺失分钟处断开，价格数据不做平滑处理。每分钟最高价和最低价保留在 CSV 中。零成交量的价格标签不代表该分钟存在可成交量。", "", "该档案仅支持核对价格及走势，不提供收益回测。历史整理不证明信号在出票当天已公开。网站区间最高价可能来自更长的持有区间。", ""]
        atomic_write(folder / "README.md", "\n".join(lines).encode("utf-8"))

    def day(self, day: str, records: list[dict], signals_fetched_at: str) -> None:
        folder = self.root / "data" / day
        preserve_csv(self.root, folder / "signals.csv", csv_bytes(records, SIGNAL_FIELDS), "网站出票记录变更（保留前一份快照）")
        metadata_path = folder / "metadata.json"
        previous = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        info = {
            "signal_date": day, "source_url": SIGNALS_URL, "signals_fetched_at": signals_fetched_at,
            "first_archived_at": previous.get("first_archived_at", now_iso()),
            "archive_type": previous.get("archive_type", "historical_import" if day < self.now.date().isoformat() else "collected_on_signal_date"),
            "timezone": "Asia/Shanghai", "signal_count": len(records), "stocks": [],
        }
        groups = defaultdict(list)
        for row in records:
            groups[row["code"] or f"missing-{row['id']}"].append(row)
        for key, stock_records in groups.items():
            stock_info = self.stock(day, stock_records, folder / key)
            info["stocks"].append({"folder": key, "code": stock_info["code"], "names": stock_info["names"], "signal_ids": stock_info["signal_ids"], "status": stock_info["status"]})
            print(f"  {key}: {STATUS[stock_info['status']]}", flush=True)
        current_folders = set(groups)
        # Keep removed source records discoverable, with their previous metadata intact.
        info["previous_stock_folders"] = sorted({path.name for path in folder.iterdir() if path.is_dir()} - current_folders)
        write_json(metadata_path, info)
        label = "历史整理" if info["archive_type"] == "historical_import" else "当日采集（不证明首次公开时间）"
        lines = [f"# {day} 出票记录", "", f"归档性质：{label}。共 {len(records)} 条出票记录。", "", f"出票来源：[网站日历](https://dipmethod.com/calendar/?date={day}) · [原始 CSV](signals.csv) · [采集元数据](metadata.json)", "", "| 股票代码 | 股票名称 | 记录买点（元） | 行情状态 |", "| --- | --- | ---: | --- |"]
        statuses = {stock["folder"]: stock["status"] for stock in info["stocks"]}
        for row in records:
            key = row["code"] or f"missing-{row['id']}"
            lines.append(f"| [{row['code'] or '代码缺失'}]({key}/README.md) | {md(row['name'])} | {row['cost']} | {STATUS[statuses[key]]} |")
        if not records:
            lines += ["", "该日期的当前出票接口返回零条记录。"]
        if info["previous_stock_folders"]:
            lines += ["", "## 先前归档", "", "以下条目已不在本次网站清单中，保留原有文件供核对：", ""]
            lines += [f"- [{key}]({key}/README.md)" for key in info["previous_stock_folders"]]
        lines += ["", f"本次读取出票接口的时间：{signals_fetched_at}。买点和区间指标沿用原始记录，不推断实际成交。", ""]
        atomic_write(folder / "README.md", "\n".join(lines).encode("utf-8"))

    def index(self) -> None:
        folder = self.root / "data"
        entries = []
        for path in folder.glob("????-??-??/metadata.json"):
            info = json.loads(path.read_text())
            counts = defaultdict(int)
            for stock in info["stocks"]:
                counts[STATUS[stock["status"]]] += 1
            summary = "、".join(f"{key} {value}" for key, value in counts.items()) or "零出票"
            entries.append((info["signal_date"], info["signal_count"], summary))
        lines = ["# 按出票日期查看", "", "每个目录包含当天全部出票记录及个股两天分钟行情。状态描述的是行情覆盖情况，不代表成交或收益验证。", "", "| 出票日期 | 记录数 | 行情状态（按股票） |", "| --- | ---: | --- |"]
        lines += [f"| [{day}]({day}/README.md) | {count} | {summary} |" for day, count, summary in sorted(entries, reverse=True)]
        lines += ["", "[返回项目说明](../README.md)", ""]
        atomic_write(folder / "README.md", "\n".join(lines).encode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="归档网站出票记录和两天一分钟行情")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", help="一个出票日期 YYYY-MM-DD")
    group.add_argument("--all", action="store_true", help="归档全部已有出票日期")
    parser.add_argument("--start", help="出票日期范围起点（包含）")
    parser.add_argument("--end", help="出票日期范围终点（包含）")
    parser.add_argument("--output", type=Path, default=Path.cwd(), help="项目根目录，默认当前目录")
    parser.add_argument("--env-file", type=Path, action="append", default=[], help="额外本地环境文件，可重复传入")
    parser.add_argument("--postman-file", type=Path, help="只读取本地 Postman 文件里的凭证，不复制该文件")
    parser.add_argument("--signals-file", type=Path, help="读取已保存的接口 JSON，代替实时接口")
    parser.add_argument("--refresh", action="store_true", help="重新获取已有行情并保留更正快照")
    args = parser.parse_args(argv)
    try:
        if (args.date or args.all) and (args.start or args.end):
            raise ArchiveError("--date/--all 与日期范围不能同时使用")
        root = args.output.resolve()
        values = load_env([root / ".env"] + args.env_file)
        if args.postman_file:
            values["ZHITU_TOKEN"] = postman_token(args.postman_file)
        if not values.get("ZHITU_TOKEN"):
            raise ArchiveError("缺少 ZHITU_TOKEN，请配置本地 .env 或 --postman-file")
        client = Client(values["ZHITU_TOKEN"], values.get("TUSHARE_TOKEN", ""))
        records = validate_signals(json.loads(args.signals_file.read_text())) if args.signals_file else client.signals()
        fetched_at = now_iso()
        write_json(root / ".cache" / "signals-latest.json", {"fetched_at": fetched_at, "signals": records, "source": "local_file" if args.signals_file else SIGNALS_URL})
        if not records and not args.date:
            raise ArchiveError("出票接口没有记录，无法选择归档日期")
        dates = sorted({row["sig_date"] for row in records})
        if args.date:
            dates = [iso_date(args.date)]
        elif args.all:
            # Notice deletions even when a date disappears completely from the source.
            archived_dates = [path.name for path in (root / "data").glob("????-??-??") if path.is_dir()]
            dates = sorted(set(dates) | set(archived_dates))
        elif args.start or args.end:
            start = iso_date(args.start) if args.start else dates[0]
            end = iso_date(args.end) if args.end else dates[-1]
            if start > end:
                raise ArchiveError("开始日期不能晚于结束日期")
            dates = [day for day in dates if start <= day <= end]
        elif not args.all:
            dates = dates[-1:]
        if not dates:
            raise ArchiveError("所选范围没有出票日期")
        selected = [row for row in records if row["sig_date"] in set(dates)]
        exchanges = sorted({EXCHANGES[row["code"][-2:]] for row in selected if row["code"]})
        end = (date.fromisoformat(dates[-1]) + timedelta(days=45)).isoformat()
        calendars = {}
        for exchange in exchanges:
            print(f"读取 {exchange} 交易日历 {dates[0]} 至 {end}", flush=True)
            calendars[exchange] = client.calendar(exchange, dates[0], end)
            write_json(root / ".cache" / f"calendar-{exchange}.json", {"fetched_at": fetched_at, "days": calendars[exchange]})
        archive = Archive(root, client, calendars, args.refresh)
        selected_by_date = defaultdict(list)
        for row in selected:
            selected_by_date[row["sig_date"]].append(row)
        for index, day in enumerate(dates, 1):
            print(f"[{index}/{len(dates)}] {day} · {len(selected_by_date[day])} 条出票", flush=True)
            archive.day(day, selected_by_date[day], fetched_at)
            archive.index()
        print(json.dumps({"dates": len(dates), "signals": len(selected), "minute_days": dict(archive.statuses), "issues": archive.failures}, ensure_ascii=False), flush=True)
        return 2 if archive.failures else 0
    except (ArchiveError, ValueError, OSError) as error:
        # Only explicitly sanitized operational messages are surfaced.
        message = str(error) if isinstance(error, ArchiveError) else "本地文件或配置读取失败，请检查文件格式和路径"
        print(f"错误：{message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断；已生成文件保留，重跑可继续补齐。", file=sys.stderr)
        return 130
