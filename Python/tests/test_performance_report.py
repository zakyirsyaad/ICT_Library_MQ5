"""Contract and metric tests for the trade journal performance report."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

TESTS_DIR = Path(__file__).resolve().parent
PYTHON_DIR = TESTS_DIR.parent
ROOT = PYTHON_DIR.parent

sys.path.insert(0, str(PYTHON_DIR))

SCHEMA_PATH = ROOT / "schemas/trade-journal.schema.json"
FIXTURE = ROOT / "tests/fixtures/trade-journal-v1.jsonl"
EXPECTED = ROOT / "tests/fixtures/trade-journal-v1.expected.txt"
REPORT_SCRIPT = PYTHON_DIR / "16_performance_report.py"


def load_report_module():
    """Load 16_performance_report.py; a digit-prefixed name cannot be imported.

    Follows the spec_from_file_location pattern used by test_csv_contract.py,
    with one required addition: the module must be registered in sys.modules
    before exec_module. dataclasses resolves string annotations (produced by
    `from __future__ import annotations`) through sys.modules[cls.__module__],
    and returns None for an unregistered module. The numbered training scripts
    that test_csv_contract.py loads define no dataclasses, so they never hit this.
    """
    spec = importlib.util.spec_from_file_location(REPORT_SCRIPT.stem, REPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fixture_lines():
    """Return the fixture's raw non-empty lines, in file order."""
    return [line for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def valid_fixture_rows():
    """Parse every fixture line that is well-formed JSON.

    Test-only helper: the truncated trailing line is a deliberate fixture case,
    so it is skipped here rather than raising.
    """
    rows = []
    for line in fixture_lines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


class JournalSchemaTests(unittest.TestCase):
    def test_fixture_valid_rows_conform_to_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        rows = valid_fixture_rows()
        self.assertEqual(len(rows), 7)
        for row in rows:
            jsonschema.validate(row, schema)

    def test_trade_fields_match_schema_required(self):
        """Guard against drift between the published schema and the parser.

        The analyzer validates rows in Python rather than calling jsonschema per
        row, so nothing else would catch the two lists diverging.
        """
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        expected = set(schema["required"]) - {"journal_version"}
        self.assertEqual(set(load_report_module().TRADE_FIELDS), expected)


class JournalParserTests(unittest.TestCase):
    """Boundary rules for reading a journal. Every rule here is a way real
    MT5 output can be malformed; none of them may raise out of load_journal."""

    def setUp(self):
        self.report = load_report_module()
        self.rows = valid_fixture_rows()

    def write_journal(self, directory, rows):
        path = Path(directory) / "journal.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_loads_valid_rows_into_trade_objects(self):
        trades, warnings = self.report.load_journal(FIXTURE)
        self.assertEqual(len(trades), 7)
        self.assertEqual(trades[0].exit_reason, "tp")
        self.assertEqual(trades[0].symbol, "XAUUSD")

    def test_trades_preserve_file_order_without_sorting(self):
        trades, _ = self.report.load_journal(FIXTURE)
        self.assertEqual([trade.ticket for trade in trades],
                         [100001, 100002, 100003, 100004, 100005, 100006, 100007])

    def test_trade_is_immutable(self):
        trades, _ = self.report.load_journal(FIXTURE)
        with self.assertRaises(AttributeError):
            trades[0].net_profit = 999.0

    def test_truncated_last_line_is_discarded_with_warning(self):
        trades, warnings = self.report.load_journal(FIXTURE)
        self.assertEqual(len(trades), 7)
        truncated = [w for w in warnings if w.code == "truncated_line"]
        self.assertEqual(len(truncated), 1)
        self.assertEqual(truncated[0].line_number, 8)

    def test_unknown_field_warns_but_does_not_reject(self):
        trades, warnings = self.report.load_journal(FIXTURE)
        self.assertEqual(len(trades), 7)
        unknown = [w for w in warnings if w.code == "unknown_field"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0].line_number, 7)

    def test_missing_required_field_rejects_row(self):
        rows = [dict(row) for row in self.rows]
        del rows[0]["net_profit"]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_journal(tmp, rows)
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(len(trades), 6)
        rejected = [w for w in warnings if w.code == "rejected_row"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].line_number, 1)
        self.assertIn("net_profit", rejected[0].message)

    def test_non_finite_number_rejects_row(self):
        rows = [dict(row) for row in self.rows]
        rows[1]["net_profit"] = float("nan")
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_journal(tmp, rows)
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(len(trades), 6)
        self.assertIn("rejected_row", [w.code for w in warnings])

    def test_close_before_open_rejects_row(self):
        rows = [dict(row) for row in self.rows]
        rows[0]["close_time"] = "2026-09-13T08:00:00"
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_journal(tmp, rows)
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(len(trades), 6)
        self.assertIn("rejected_row", [w.code for w in warnings])

    def test_utf16_bom_gives_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            path.write_bytes(FIXTURE.read_text(encoding="utf-8").encode("utf-16"))
            with self.assertRaises(self.report.JournalFormatError) as ctx:
                self.report.load_journal(path)
        message = str(ctx.exception)
        self.assertTrue("UTF-16" in message or "FILE_UNICODE" in message, message)

    def test_duplicate_position_id_warns_but_keeps_both_rows(self):
        rows = [dict(row) for row in self.rows]
        rows[1]["position_id"] = rows[0]["position_id"]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_journal(tmp, rows)
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(len(trades), 7)
        duplicates = [w for w in warnings if w.code == "duplicate_position"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0].line_number, 2)


if __name__ == "__main__":
    unittest.main()
