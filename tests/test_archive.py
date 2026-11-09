import csv
import io
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from dipmethod_public_data.archive import (
    Archive, ArchiveError, CHINA, MINUTE_FIELDS, SIGNAL_FIELDS, csv_bytes,
    expected_labels, inspect_minutes, next_session, preserve_csv, validate_minutes,
    validate_signals,
)


def minute(stamp, price=8.55):
    return {"t": stamp, "o": price, "h": price + .1, "l": price - .1,
            "c": price, "v": 100, "a": 85500, "pc": price, "sf": 0}


def full_day(day):
    return [minute(stamp) for stamp in sorted(expected_labels(day) | {f"{day} 09:30:00"})]


def signal(code="002177.SZ", day="2026-01-05", identifier="sig-0001"):
    return {"id": identifier, "seq": 1, "sig_date": day, "code": code,
            "name": "御银股份", "cost": 8.555, "peak": 9.48, "peak_return": .1087}


class CalendarTests(unittest.TestCase):
    def test_next_exchange_session_skips_holiday_and_weekend(self):
        calendar = [
            {"date": "2026-01-30", "is_open": 1},
            {"date": "2026-01-31", "is_open": 0},
            {"date": "2026-02-01", "is_open": 0},
            {"date": "2026-02-02", "is_open": 0},
            {"date": "2026-02-03", "is_open": 1},
        ]
        self.assertEqual(next_session("2026-01-30", calendar), "2026-02-03")

    def test_calendar_gap_is_not_guessed(self):
        calendar = [{"date": "2026-01-05", "is_open": 1}, {"date": "2026-01-07", "is_open": 1}]
        with self.assertRaises(ArchiveError):
            next_session("2026-01-05", calendar)


class InputTests(unittest.TestCase):
    def test_null_code_and_precision_are_preserved(self):
        records = validate_signals({"signals": [signal(code=None)]})
        output = csv_bytes(records, SIGNAL_FIELDS)
        self.assertTrue(output.startswith(b"\xef\xbb\xbf"))
        row = next(csv.DictReader(io.StringIO(output.decode("utf-8-sig"))))
        self.assertEqual(row["code"], "")
        self.assertEqual(row["cost"], "8.555")
        self.assertEqual(row["name"], "御银股份")

    def test_bad_price_duplicate_or_wrong_date_are_rejected(self):
        row = minute("2026-01-05 09:31:00")
        for payload in [[row, row], [{**row, "l": 99}], [{**row, "t": "2026-01-06 09:31:00"}], [{**row, "v": -1}]]:
            with self.subTest(payload=payload), self.assertRaises(ArchiveError):
                validate_minutes(payload, "2026-01-05", "2026-01-05")

    def test_coverage_and_pending_are_distinct(self):
        rows = full_day("2026-01-05")
        closed = datetime(2026, 1, 5, 16, tzinfo=CHINA)
        self.assertEqual(inspect_minutes(rows, "2026-01-05", closed)["status"], "complete")
        self.assertEqual(inspect_minutes(rows[:-1], "2026-01-05", closed)["status"], "partial")
        self.assertEqual(inspect_minutes([], "2026-01-05", closed)["status"], "empty")
        before = datetime(2026, 1, 5, 10, tzinfo=CHINA)
        self.assertEqual(inspect_minutes(rows[:5], "2026-01-05", before)["status"], "pending")


class CorrectionTests(unittest.TestCase):
    def test_identical_run_is_idempotent_and_correction_retains_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data/2026-01-05/signals.csv"
            self.assertTrue(preserve_csv(root, path, b"old\n", "test"))
            self.assertFalse(preserve_csv(root, path, b"old\n", "test"))
            self.assertFalse((root / "corrections").exists())
            self.assertTrue(preserve_csv(root, path, b"new\n", "test"))
            changes = [json.loads(line) for line in (root / "corrections/changes.jsonl").read_text().splitlines()]
            self.assertEqual(len(changes), 1)
            self.assertEqual((root / changes[0]["previous_file"]).read_bytes(), b"old\n")
            self.assertEqual(path.read_bytes(), b"new\n")


class FakeClient:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def minutes(self, code, start, end):
        self.calls.append((code, start, end))
        return [row for row in self.rows if start <= row["t"][:10] <= end]


class ArchiveTests(unittest.TestCase):
    def calendar(self):
        return {"SZSE": [{"date": "2026-01-05", "is_open": 1}, {"date": "2026-01-06", "is_open": 1}]}

    def test_archive_keeps_exact_next_date_when_no_market_data(self):
        client = FakeClient(full_day("2026-01-05"))
        with tempfile.TemporaryDirectory() as directory, patch("dipmethod_public_data.archive.plot_minutes"):
            root = Path(directory)
            archive = Archive(root, client, self.calendar(), now=datetime(2026, 1, 7, tzinfo=CHINA))
            archive.day("2026-01-05", [signal()], "2026-01-07T09:00:00+08:00")
            archive.index()
            info = json.loads((root / "data/2026-01-05/002177.SZ/metadata.json").read_text())
            self.assertEqual(info["next_trade_date"], "2026-01-06")
            self.assertEqual(info["days"][1]["status"], "empty")
            self.assertFalse((root / "data/2026-01-05/002177.SZ/2026-01-06.csv").exists())
            self.assertEqual(archive.failures, 1)

    def test_future_next_session_is_pending_without_future_request(self):
        client = FakeClient(full_day("2026-01-05"))
        with tempfile.TemporaryDirectory() as directory, patch("dipmethod_public_data.archive.plot_minutes"):
            root = Path(directory)
            archive = Archive(root, client, self.calendar(), now=datetime(2026, 1, 5, 16, tzinfo=CHINA))
            archive.day("2026-01-05", [signal()], "2026-01-05T16:00:00+08:00")
            self.assertEqual(client.calls, [("002177.SZ", "2026-01-05", "2026-01-05")])
            info = json.loads((root / "data/2026-01-05/002177.SZ/metadata.json").read_text())
            self.assertEqual(info["status"], "pending")
            self.assertEqual(archive.failures, 0)

    def test_missing_code_is_retained_without_fetch(self):
        client = FakeClient([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = Archive(root, client, {}, now=datetime(2026, 1, 7, tzinfo=CHINA))
            archive.day("2026-01-05", [signal(code=None)], "2026-01-07T09:00:00+08:00")
            self.assertEqual(client.calls, [])
            self.assertTrue((root / "data/2026-01-05/missing-sig-0001/README.md").exists())

    def test_complete_cache_avoids_repeated_requests(self):
        client = FakeClient(full_day("2026-01-05") + full_day("2026-01-06"))
        with tempfile.TemporaryDirectory() as directory, patch("dipmethod_public_data.archive.plot_minutes"):
            root = Path(directory)
            first = Archive(root, client, self.calendar(), now=datetime(2026, 1, 7, tzinfo=CHINA))
            first.day("2026-01-05", [signal()], "2026-01-07T09:00:00+08:00")
            second = Archive(root, client, self.calendar(), now=datetime(2026, 1, 7, tzinfo=CHINA))
            second.day("2026-01-05", [signal()], "2026-01-07T09:00:00+08:00")
            self.assertEqual(len(client.calls), 1)
            self.assertFalse((root / "corrections").exists())

    def test_failed_refresh_keeps_existing_csv_and_reports_error(self):
        client = FakeClient(full_day("2026-01-05") + full_day("2026-01-06"))
        with tempfile.TemporaryDirectory() as directory, patch("dipmethod_public_data.archive.plot_minutes"):
            root = Path(directory)
            archive = Archive(root, client, self.calendar(), now=datetime(2026, 1, 7, tzinfo=CHINA))
            archive.day("2026-01-05", [signal()], "2026-01-07T09:00:00+08:00")
            csv_path = root / "data/2026-01-05/002177.SZ/2026-01-06.csv"
            original = csv_path.read_bytes()
            with patch.object(client, "minutes", side_effect=ArchiveError("上游网络请求失败或超时")):
                retry = Archive(root, client, self.calendar(), refresh=True, now=datetime(2026, 1, 7, tzinfo=CHINA))
                retry.day("2026-01-05", [signal()], "2026-01-07T10:00:00+08:00")
            self.assertEqual(csv_path.read_bytes(), original)
            info = json.loads((csv_path.parent / "metadata.json").read_text())
            self.assertEqual(info["days"][1]["status"], "error")
            self.assertEqual(info["days"][1]["rows"], 241)
            self.assertEqual(retry.failures, 2)

    def test_removed_signal_keeps_previous_stock_folder_and_snapshot(self):
        client = FakeClient(full_day("2026-01-05") + full_day("2026-01-06"))
        with tempfile.TemporaryDirectory() as directory, patch("dipmethod_public_data.archive.plot_minutes"):
            root = Path(directory)
            archive = Archive(root, client, self.calendar(), now=datetime(2026, 1, 7, tzinfo=CHINA))
            archive.day("2026-01-05", [signal()], "2026-01-07T09:00:00+08:00")
            archive.day("2026-01-05", [], "2026-01-07T10:00:00+08:00")
            info = json.loads((root / "data/2026-01-05/metadata.json").read_text())
            self.assertEqual(info["signal_count"], 0)
            self.assertEqual(info["previous_stock_folders"], ["002177.SZ"])
            self.assertTrue((root / "data/2026-01-05/002177.SZ/2026-01-05.csv").exists())
            self.assertTrue((root / "corrections/changes.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
