"""Contract and metric tests for the trade journal performance report."""

import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
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


class MetricsTests(unittest.TestCase):
    """Every expected value here is computed by hand from the 8 fixture rows.

    Fixture in close_time order: W(+10), L(-5), BE(0), L(-2.5), L(-5), L(-5), W(+10).
    Balances are 500 except row 5, which is 20 to exercise the small-account case.
    """

    def setUp(self):
        self.report = load_report_module()
        self.trades, _ = self.report.load_journal(FIXTURE)

    def metrics(self, trades=None):
        return self.report.compute_metrics(self.trades if trades is None else trades)

    def test_core_metrics_match_hand_computed_values(self):
        m = self.metrics()
        self.assertEqual(m["trade_count"], 7)
        self.assertAlmostEqual(m["total_net"], 2.5, places=9)
        self.assertAlmostEqual(m["win_rate"], 2 / 7, places=9)
        self.assertAlmostEqual(m["expectancy"], 2.5 / 7, places=9)
        self.assertAlmostEqual(m["avg_win"], 10.0, places=9)
        self.assertAlmostEqual(m["avg_loss"], -4.375, places=9)
        self.assertAlmostEqual(m["largest_win"], 10.0, places=9)
        self.assertAlmostEqual(m["largest_loss"], -5.0, places=9)
        self.assertAlmostEqual(m["profit_factor"], 20.0 / 17.5, places=9)
        self.assertAlmostEqual(m["payoff_ratio"], 10.0 / 4.375, places=9)

    def test_break_even_counts_as_neither_win_nor_loss(self):
        m = self.metrics()
        # row 3 has net_profit 0.0: 2 wins and 4 losses out of 7, not 3 and 4
        self.assertAlmostEqual(m["win_rate"], 2 / 7, places=9)
        self.assertAlmostEqual(m["avg_loss"], -17.5 / 4, places=9)

    def test_cost_pct_of_gross_uses_commission_and_swap_only(self):
        m = self.metrics()
        self.assertAlmostEqual(m["total_gross"], 4.5, places=9)
        self.assertAlmostEqual(m["total_commission"], -1.7, places=9)
        self.assertAlmostEqual(m["total_swap"], -0.3, places=9)
        self.assertAlmostEqual(m["cost_pct_of_gross"], 2.0 / 4.5 * 100, places=9)
        # Spread is already inside the entry/exit prices that produced gross_profit,
        # so adding it to cost would double-count. It is reported in pips instead.
        self.assertAlmostEqual(m["total_spread_pips"], 19.5, places=9)

    def test_max_consecutive_losses_counts_run_not_total(self):
        # W, L, BE, L, L, L, W -> longest run is 3 even though there are 4 losers
        self.assertEqual(self.metrics()["max_consecutive_losses"], 3)

    def test_max_drawdown_matches_hand_computed_equity_curve(self):
        m = self.metrics()
        # equity: 500 510 505 505 502.5 497.5 492.5 502.5, peak 510
        self.assertAlmostEqual(m["max_drawdown_abs"], 17.5, places=9)
        self.assertAlmostEqual(m["max_drawdown_pct"], 17.5 / 510 * 100, places=9)

    def test_below_threshold_flags_insufficient_data(self):
        self.assertFalse(self.metrics()["sufficient_data"])

    def test_at_threshold_flags_sufficient_data(self):
        threshold = self.report.MIN_TRADES_FOR_VERDICT
        many = (self.trades * ((threshold // len(self.trades)) + 1))[:threshold]
        self.assertEqual(len(many), threshold)
        self.assertTrue(self.report.compute_metrics(many)["sufficient_data"])

    def test_empty_journal_returns_zero_metrics_without_raising(self):
        m = self.report.compute_metrics([])
        self.assertEqual(m["trade_count"], 0)
        self.assertFalse(m["sufficient_data"])
        for key in ("win_rate", "expectancy", "profit_factor", "payoff_ratio",
                    "avg_win", "avg_loss", "max_drawdown_abs", "max_drawdown_pct",
                    "cost_pct_of_gross"):
            self.assertEqual(m[key], 0.0, key)

    def test_all_wins_and_all_losses_do_not_raise(self):
        wins = [replace(t, net_profit=abs(t.net_profit) + 1.0) for t in self.trades]
        self.assertEqual(self.metrics(wins)["profit_factor"], float("inf"))
        losses = [replace(t, net_profit=-(abs(t.net_profit) + 1.0)) for t in self.trades]
        m = self.metrics(losses)
        self.assertEqual(m["profit_factor"], 0.0)
        self.assertEqual(m["payoff_ratio"], 0.0)
        self.assertEqual(m["avg_win"], 0.0)


class RiskVerdictTests(unittest.TestCase):
    def setUp(self):
        self.report = load_report_module()
        self.trades, _ = self.report.load_journal(FIXTURE)

    def test_risk_verdict_uses_losers_only(self):
        v = self.report.compute_risk_verdict(self.trades)
        self.assertEqual(v["n_losers"], 4)
        self.assertAlmostEqual(v["realized_min"], 0.5, places=9)
        self.assertAlmostEqual(v["realized_median"], 1.0, places=9)
        self.assertAlmostEqual(v["realized_max"], 25.0, places=9)
        self.assertEqual(v["exceedances"], 1)
        self.assertAlmostEqual(v["max_risk_pct"], 2.0, places=9)
        self.assertEqual(v["verdict"], "risk_exceeded")

    def test_risk_verdict_answers_even_below_sample_threshold(self):
        """Risk realization is a structural sizing question, not a statistical one.

        One trade losing 25% against an intended 1% already proves the broker's lot
        step makes the requested risk unreachable, so the verdict must not wait for
        MIN_TRADES_FOR_VERDICT the way expectancy does.
        """
        self.assertLess(len(self.trades), self.report.MIN_TRADES_FOR_VERDICT)
        self.assertFalse(self.report.compute_metrics(self.trades)["sufficient_data"])
        self.assertEqual(self.report.compute_risk_verdict(self.trades)["verdict"],
                         "risk_exceeded")

    def test_risk_verdict_without_losers_is_insufficient_data(self):
        wins = [replace(t, net_profit=abs(t.net_profit) + 1.0) for t in self.trades]
        v = self.report.compute_risk_verdict(wins)
        self.assertEqual(v["n_losers"], 0)
        self.assertEqual(v["verdict"], "insufficient_data")

    def test_contained_risk_verdict_when_within_threshold(self):
        # The $500-account rows realize 1.0% or less; drop the $20 row and the
        # default 2.0% threshold is respected.
        contained = [t for t in self.trades if t.balance_at_open > 100.0]
        v = self.report.compute_risk_verdict(contained)
        self.assertAlmostEqual(v["realized_max"], 1.0, places=9)
        self.assertEqual(v["exceedances"], 0)
        self.assertEqual(v["verdict"], "risk_contained")

    def test_threshold_is_configurable(self):
        # 25.0% is contained only by an absurd threshold, which proves the
        # parameter is actually consulted rather than hardcoded.
        v = self.report.compute_risk_verdict(self.trades, max_risk_pct=30.0)
        self.assertEqual(v["verdict"], "risk_contained")
        self.assertAlmostEqual(v["max_risk_pct"], 30.0, places=9)


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.report = load_report_module()
        self.trades, _ = self.report.load_journal(FIXTURE)

    def test_variant_groups_report_sample_size(self):
        groups = self.report.group_by(self.trades, "variant")
        self.assertEqual(set(groups), {"full", "no-trigger"})
        self.assertEqual(groups["full"]["trade_count"], 6)
        self.assertEqual(groups["no-trigger"]["trade_count"], 1)

    def test_group_values_are_full_metric_dicts(self):
        groups = self.report.group_by(self.trades, "variant")
        whole = self.report.compute_metrics(self.trades)
        self.assertEqual(set(groups["full"]), set(whole))

    def test_session_groups_match_fixture_counts(self):
        groups = self.report.group_by(self.trades, "session")
        self.assertEqual({name: g["trade_count"] for name, g in groups.items()},
                         {"London": 4, "NewYork": 2, "Asian": 1})

    def test_hour_groups_are_zero_padded_so_they_sort(self):
        groups = self.report.group_by(self.trades, "hour")
        # Row 7 opens 2026-09-18T10:00:00; row 5 opens at 02:15.
        self.assertEqual(sorted(groups), ["02", "08", "09", "10", "13", "14", "15"])

    def test_weekday_groups_are_locale_independent_english(self):
        groups = self.report.group_by(self.trades, "weekday")
        self.assertEqual({name: g["trade_count"] for name, g in groups.items()},
                         {"Monday": 2, "Tuesday": 1, "Wednesday": 1,
                          "Thursday": 2, "Friday": 1})

    def test_weekday_groups_are_ordered_chronologically_not_alphabetically(self):
        # The rendered report and its golden file depend on this order.
        groups = self.report.group_by(self.trades, "weekday")
        self.assertEqual(list(groups),
                         ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"])

    def test_every_groupable_key_partitions_all_trades(self):
        for key in self.report.GROUPABLE_KEYS:
            with self.subTest(key=key):
                groups = self.report.group_by(self.trades, key)
                self.assertEqual(sum(g["trade_count"] for g in groups.values()), 7)

    def test_unknown_key_raises_rather_than_returning_empty(self):
        with self.assertRaises(ValueError):
            self.report.group_by(self.trades, "not_a_field")

    def test_empty_input_gives_empty_groups(self):
        self.assertEqual(self.report.group_by([], "variant"), {})


class ExitQualityTests(unittest.TestCase):
    def setUp(self):
        self.report = load_report_module()
        self.trades, _ = self.report.load_journal(FIXTURE)

    def test_exit_reason_counts_cover_every_trade(self):
        q = self.report.compute_exit_quality(self.trades)
        self.assertEqual(sum(q["exit_reason_counts"].values()), 7)
        self.assertEqual(q["exit_reason_counts"]["sl"], 3)
        self.assertEqual(q["exit_reason_counts"]["tp"], 2)
        self.assertEqual(q["exit_reason_counts"]["be_stop"], 1)
        self.assertEqual(q["exit_reason_counts"]["time_stop"], 1)

    def test_exit_reason_counts_always_emit_every_reason(self):
        """A stable row set keeps the rendered report comparable across runs."""
        q = self.report.compute_exit_quality(self.trades[:1])
        self.assertEqual(list(q["exit_reason_counts"]), list(self.report.EXIT_REASONS))
        self.assertEqual(q["exit_reason_counts"]["manual"], 0)

    def test_tp_reach_count_matches_hand_computed_rows(self):
        # Only rows 1 and 7 have mfe >= |tp - entry| (10.0) and a positive net.
        q = self.report.compute_exit_quality(self.trades)
        self.assertEqual(q["tp_reached_before_sl_count"], 2)

    def test_sl_breach_count_matches_hand_computed_rows(self):
        # Rows 2, 5 and 6 have mae <= -|entry - sl| (-5.0); all three exited on sl.
        q = self.report.compute_exit_quality(self.trades)
        self.assertEqual(q["sl_exceeded_before_favourable_count"], 3)

    def test_mfe_mae_use_median_not_mean(self):
        q = self.report.compute_exit_quality(self.trades)
        # mfe sorted: 0.8 1.5 2.0 3.0 6.5 10.0 10.0 -> median 3.0 (mean would be 4.83)
        self.assertAlmostEqual(q["median_mfe"], 3.0, places=9)
        # mae sorted: -5.0 -5.0 -5.0 -2.5 -2.0 -1.2 -0.5 -> median -2.5
        self.assertAlmostEqual(q["median_mae"], -2.5, places=9)

    def test_trades_without_a_target_are_excluded_from_tp_reach(self):
        no_target = [replace(t, tp=0.0) for t in self.trades]
        q = self.report.compute_exit_quality(no_target)
        self.assertEqual(q["tp_reached_before_sl_count"], 0)

    def test_empty_input_gives_zeroed_exit_quality(self):
        q = self.report.compute_exit_quality([])
        self.assertEqual(q["tp_reached_before_sl_count"], 0)
        self.assertEqual(q["sl_exceeded_before_favourable_count"], 0)
        self.assertEqual(q["median_mfe"], 0.0)
        self.assertEqual(q["median_mae"], 0.0)


if __name__ == "__main__":
    unittest.main()
