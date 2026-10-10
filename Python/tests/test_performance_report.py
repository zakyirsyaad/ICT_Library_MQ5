"""Contract and metric tests for the trade journal performance report."""

import importlib.util
import json
import sys
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

    Mirrors the pattern already used by test_csv_contract.py for the numbered
    training scripts.
    """
    spec = importlib.util.spec_from_file_location(REPORT_SCRIPT.stem, REPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
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


if __name__ == "__main__":
    unittest.main()
