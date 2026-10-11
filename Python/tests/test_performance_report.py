"""Contract and metric tests for the trade journal performance report."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path

import jsonschema

TESTS_DIR = Path(__file__).resolve().parent
PYTHON_DIR = TESTS_DIR.parent
ROOT = PYTHON_DIR.parent

sys.path.insert(0, str(PYTHON_DIR))

SCHEMA_PATH = ROOT / "schemas/trade-journal.schema.json"
FIXTURE = ROOT / "tests/fixtures/trade-journal-v1.jsonl"
NULLS_FIXTURE = ROOT / "tests/fixtures/trade-journal-v1-nulls.jsonl"
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


NO_FILTERS = {"variant": None, "max_risk_pct": 2.0}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.report = load_report_module()
        self.trades, self.warnings = self.report.load_journal(FIXTURE)

    def build(self, trades=None, filters=NO_FILTERS):
        return self.report.build_report(
            self.trades if trades is None else trades, self.warnings, filters)

    def render(self, trades=None, filters=NO_FILTERS):
        return self.report.render_report(self.build(trades, filters))

    def test_report_carries_every_group_key(self):
        built = self.build()
        self.assertEqual(set(built), {"metrics", "filters", "risk_verdict",
                                      "exit_quality", "groups", "warnings"})
        self.assertEqual(set(built["groups"]), set(self.report.GROUP_KEYS))
        self.assertEqual(built["groups"]["variant"]["full"]["trade_count"], 6)

    def test_report_is_json_serializable_without_nan(self):
        """Infinity is not valid JSON, and small groups produce it routinely.

        group_by computes metrics per subset, so a group holding one winning
        trade has no losing side and profit_factor is infinite. json.dumps would
        emit the non-standard literal Infinity, which jq and browsers reject.
        """
        json.dumps(self.build(), allow_nan=False)

    def test_infinite_ratio_becomes_json_safe_in_the_report(self):
        wins = [replace(t, net_profit=abs(t.net_profit) + 1.0) for t in self.trades]
        self.assertEqual(self.report.compute_metrics(wins)["profit_factor"], float("inf"))
        self.assertEqual(self.build(wins)["metrics"]["profit_factor"], "inf")

    def test_filters_are_carried_into_the_risk_threshold(self):
        strict = {"variant": None, "max_risk_pct": 0.75}
        self.assertEqual(self.build(filters=strict)["risk_verdict"]["verdict"],
                         "risk_exceeded")
        loose = {"variant": None, "max_risk_pct": 30.0}
        self.assertEqual(self.build(filters=loose)["risk_verdict"]["verdict"],
                         "risk_contained")

    def test_rendered_report_matches_expected_fixture(self):
        expected = EXPECTED.read_text(encoding="utf-8")
        self.assertEqual(self.render(), expected)

    def test_rendered_report_ends_with_newline(self):
        self.assertTrue(self.render().endswith("\n"))
        self.assertFalse(self.render().endswith("\n\n"))

    def test_report_includes_insufficient_data_notice(self):
        self.assertIn("insufficient", self.render().lower())

    def test_warnings_are_surfaced_in_report(self):
        text = self.render()
        self.assertIn("truncated_line", text)
        self.assertIn("unknown_field", text)
        self.assertIn("broker_tag", text)

    def test_data_quality_precedes_every_other_section(self):
        lines = [line for line in self.render().splitlines() if line.strip()]
        self.assertEqual(lines[1], "DATA QUALITY")

    def test_rendered_report_shows_ablation_table(self):
        text = self.render()
        self.assertIn("no-trigger", text)
        self.assertIn("variant", text)

    def test_rendered_report_shows_every_group_section(self):
        text = self.render()
        for key in self.report.GROUP_KEYS:
            with self.subTest(key=key):
                # "BY <key>" not just "<key>": bare "variant" also occurs in the
                # FILTERS block and "trigger" inside the value "no-trigger", so
                # the weaker assertion passed even with the section deleted.
                self.assertIn(f"BY {key}", text)

    def test_money_and_percentages_use_two_decimals(self):
        text = self.render()
        self.assertIn("25.00%", text)     # realized risk max
        self.assertIn("44.44%", text)     # cost as % of gross
        self.assertIn("-4.38", text)      # avg loss, rounded from -4.375


class CliTests(unittest.TestCase):
    def setUp(self):
        self.report = load_report_module()

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.report.main(argv)
        return code, out.getvalue(), err.getvalue()

    def payload(self, argv):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.json"
            code, _, _ = self.run_main([*argv, "--json-out", str(path)])
            self.assertEqual(code, 0)
            return json.loads(path.read_text(encoding="utf-8"))

    def test_main_prints_report_and_returns_zero(self):
        code, out, _ = self.run_main(["--journal", str(FIXTURE)])
        self.assertEqual(code, 0)
        self.assertEqual(out, EXPECTED.read_text(encoding="utf-8"))

    def test_main_filters_by_variant(self):
        payload = self.payload(["--journal", str(FIXTURE), "--variant", "no-trigger"])
        self.assertEqual(payload["filters"]["variant"], "no-trigger")
        self.assertEqual(payload["metrics"]["trade_count"], 1)

    def test_main_writes_json_out_with_six_keys(self):
        payload = self.payload(["--journal", str(FIXTURE)])
        self.assertEqual(set(payload), {"metrics", "filters", "risk_verdict",
                                        "exit_quality", "groups", "warnings"})
        self.assertEqual(payload["metrics"]["trade_count"], 7)

    def test_json_out_is_strict_json_without_nan_literals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "metrics.json"
            self.run_main(["--journal", str(FIXTURE), "--json-out", str(path)])
            raw = path.read_text(encoding="utf-8")
        self.assertNotIn("Infinity", raw)
        self.assertNotIn("NaN", raw)
        json.loads(raw, parse_constant=lambda name: self.fail(f"non-finite {name}"))

    def test_max_risk_pct_flows_into_verdict(self):
        contained = self.payload(["--journal", str(FIXTURE), "--max-risk-pct", "30"])
        self.assertEqual(contained["risk_verdict"]["verdict"], "risk_contained")
        exceeded = self.payload(["--journal", str(FIXTURE), "--max-risk-pct", "2"])
        self.assertEqual(exceeded["risk_verdict"]["verdict"], "risk_exceeded")

    def test_max_risk_pct_defaults_to_two(self):
        payload = self.payload(["--journal", str(FIXTURE)])
        self.assertAlmostEqual(payload["filters"]["max_risk_pct"], 2.0, places=9)

    def test_variant_with_no_matches_reports_zero_trades_not_an_error(self):
        payload = self.payload(["--journal", str(FIXTURE), "--variant", "nonexistent"])
        self.assertEqual(payload["metrics"]["trade_count"], 0)
        self.assertFalse(payload["metrics"]["sufficient_data"])

    def test_missing_journal_returns_one(self):
        code, _, err = self.run_main(["--journal", str(FIXTURE.parent / "absent.jsonl")])
        self.assertEqual(code, 1)
        self.assertIn("absent.jsonl", err)

    def test_utf16_journal_returns_one_with_actionable_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            path.write_bytes(FIXTURE.read_text(encoding="utf-8").encode("utf-16"))
            code, _, err = self.run_main(["--journal", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("FILE_UNICODE", err)

    def test_invalid_arguments_exit_with_code_two(self):
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(io.StringIO()):
                self.report.main([])
        self.assertEqual(ctx.exception.code, 2)


def write_journal(directory, rows):
    """Write rows as a JSONL journal and return its path."""
    path = Path(directory) / "journal.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


class ReviewFixTests(unittest.TestCase):
    """Regression tests for the findings of the whole-branch review.

    Each test reproduces one finding against the code as it stood before the
    fix, so a revert turns the suite red rather than silently reintroducing a
    misleading number.
    """

    def setUp(self):
        self.report = load_report_module()
        self.trades, self.warnings = self.report.load_journal(FIXTURE)
        self.rows = valid_fixture_rows()

    def rows_with(self, index, **changes):
        rows = [dict(row) for row in self.rows]
        rows[index].update(changes)
        return rows

    # --- Finding 1: cost share reported a false 0.00% on a losing run ---

    def test_cost_share_is_undefined_when_gross_is_not_positive(self):
        losing = [replace(t, gross_profit=-100.0, commission=-30.0, swap=-20.0)
                  for t in self.trades]
        metrics = self.report.compute_metrics(losing)
        self.assertLess(metrics["total_gross"], 0)
        self.assertIsNone(metrics["cost_pct_of_gross"])
        text = self.report.render_report(
            self.report.build_report(losing, [], NO_FILTERS))
        self.assertIn("cost as % of gross", text)
        self.assertIn("n/a", text)

    def test_cost_share_still_zero_for_an_empty_journal(self):
        self.assertEqual(self.report.compute_metrics([])["cost_pct_of_gross"], 0.0)

    # --- Finding 2: drawdown abs and pct were independent maxima ---

    def test_drawdown_percentage_is_paired_with_the_absolute_maximum(self):
        """A growing account must not report a pairing that never happened.

        Equity 100 -> 90 (gap 10 at peak 100) -> 2090 -> 2040 (gap 50 at peak
        2090). The largest absolute drawdown is 50, which was 2.39% of its own
        peak. Reporting 50 alongside the 10% from the earlier, smaller drawdown
        describes an event that never occurred and overstates risk on exactly
        the compounding accounts this tool is used on.
        """
        grown = [
            replace(self.trades[0], balance_at_open=100.0, net_profit=-10.0),
            replace(self.trades[1], net_profit=2000.0),
            replace(self.trades[2], net_profit=-50.0),
            replace(self.trades[3], net_profit=0.0),
        ]
        metrics = self.report.compute_metrics(grown)
        self.assertAlmostEqual(metrics["max_drawdown_abs"], 50.0, places=9)
        self.assertAlmostEqual(metrics["max_drawdown_pct"], 50.0 / 2090 * 100, places=9)

    def test_fixture_drawdown_pairing_is_unchanged(self):
        metrics = self.report.compute_metrics(self.trades)
        self.assertAlmostEqual(metrics["max_drawdown_abs"], 17.5, places=9)
        self.assertAlmostEqual(metrics["max_drawdown_pct"], 17.5 / 510 * 100, places=9)

    # --- Finding 3: a target hit but eaten by costs read as never reached ---

    def test_target_reached_counts_a_hit_eaten_by_costs(self):
        """The metric answers 'did price travel to the target', not 'did it pay'.

        Commission decides net_profit, price travel decides whether the target
        was reached. Coupling them understates target reach precisely on small
        accounts, where commission is large relative to gross, and pushes a
        reader to move the target closer for the wrong reason.
        """
        eaten = replace(self.trades[1], exit_reason="tp", exit_price=2662.0,
                        mfe=10.0, gross_profit=0.20, commission=-0.35,
                        swap=0.0, net_profit=-0.15)
        quality = self.report.compute_exit_quality([eaten])
        self.assertEqual(quality["tp_reached_before_sl_count"], 1)

    # --- Finding 4: a UTF-8 BOM silently discarded the first trade ---

    def test_utf8_bom_does_not_discard_the_first_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            path.write_bytes(b"\xef\xbb\xbf" + FIXTURE.read_bytes())
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(len(trades), 7)
        self.assertEqual(trades[0].ticket, 100001)
        # The fixture's own truncated line 8 still warns; what must not happen
        # is the BOM turning line 1 into an unparsable record.
        truncated = [w.line_number for w in warnings if w.code == "truncated_line"]
        self.assertEqual(truncated, [8])

    def test_malformed_middle_line_is_not_blamed_on_an_interrupted_write(self):
        """Only a trailing partial line is plausibly an interrupted write.

        A malformed line in the middle of a journal means something else — a
        corrupt producer, a bad edit — and naming the wrong cause sends the
        reader looking for a crash that never happened.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            lines = [json.dumps(row) for row in self.rows]
            lines.insert(1, '{"journal_version": "1.0", "ticket": ')
            lines.append('{"journal_version": "1.0", "ticket": 999')
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(len(trades), 7)
        messages = {w.line_number: w.message
                    for w in warnings if w.code == "truncated_line"}
        self.assertEqual(sorted(messages), [2, 9])
        self.assertNotIn("interrupted write", messages[2])
        self.assertIn("interrupted write", messages[9])

    # --- Finding 5: uncapped warnings and a zero report that exits 0 ---

    def test_rendered_warning_list_is_capped_with_a_rollup(self):
        many = [self.rows_with(0, journal_version="9.9")[0] for _ in range(25)]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_journal(tmp, many)
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(trades, [])
        self.assertEqual(len(warnings), 25)
        text = self.report.render_report(
            self.report.build_report(trades, warnings, NO_FILTERS))
        detail = [line for line in text.splitlines() if line.startswith("    ")]
        self.assertLessEqual(len(detail), self.report.MAX_RENDERED_WARNINGS + 1)
        self.assertIn("more", text)
        self.assertIn("rejected_row", text)

    def test_all_rows_rejected_exits_one_with_a_diagnosis(self):
        rows = [self.rows_with(0, journal_version="9.9")[0] for _ in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_journal(tmp, rows)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.report.main(["--journal", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("rejected", err.getvalue())

    def test_empty_journal_file_still_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.jsonl"
            path.write_text("", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.report.main(["--journal", str(path)])
        self.assertEqual(code, 0)

    # --- Finding 6: the documented P&L identity was never checked ---

    def test_pnl_identity_violation_warns(self):
        rows = self.rows_with(0, net_profit=9999.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_journal(tmp, rows)
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(len(trades), 7)
        inconsistent = [w for w in warnings if w.code == "inconsistent_pnl"]
        self.assertEqual(len(inconsistent), 1)
        self.assertEqual(inconsistent[0].line_number, 1)

    def test_pnl_identity_tolerates_rounding(self):
        rows = self.rows_with(0, net_profit=10.004)
        with tempfile.TemporaryDirectory() as tmp:
            path = write_journal(tmp, rows)
            _, warnings = self.report.load_journal(path)
        self.assertNotIn("inconsistent_pnl", [w.code for w in warnings])

    # --- Finding 7: reconstructed equity versus recorded balances ---

    def test_balance_divergence_warns_once_for_the_whole_journal(self):
        """The fixture mixes a $500 and a $20 account, so divergence is real.

        One aggregated warning rather than one per row: six near-identical lines
        would bury the two warnings the fixture exists to demonstrate.
        """
        diverging = [w for w in self.warnings if w.code == "balance_divergence"]
        self.assertEqual(len(diverging), 1)
        self.assertIsNone(diverging[0].line_number)
        self.assertIn("cash movement", diverging[0].message)

    def test_consistent_balances_produce_no_divergence_warning(self):
        rows = [dict(row) for row in self.rows]
        balance = 500.0
        for row in rows:
            row["balance_at_open"] = balance
            balance += row["net_profit"]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_journal(tmp, rows)
            _, warnings = self.report.load_journal(path)
        self.assertNotIn("balance_divergence", [w.code for w in warnings])

    # --- Finding 8: only field names were guarded against schema drift ---

    def test_enums_match_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for field, allowed in self.report._ENUMS.items():
            with self.subTest(field=field):
                self.assertEqual(list(allowed), schema["properties"][field]["enum"])

    def test_sign_bounds_match_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        properties = schema["properties"]
        for field in self.report._POSITIVE:
            with self.subTest(field=field):
                self.assertEqual(properties[field].get("exclusiveMinimum"), 0)
        for field in self.report._NON_NEGATIVE:
            with self.subTest(field=field):
                self.assertEqual(properties[field].get("minimum"), 0)
        for field in self.report._NON_POSITIVE:
            with self.subTest(field=field):
                self.assertEqual(properties[field].get("maximum"), 0)

    # --- Finding 12: unreadable paths raised tracebacks ---

    def test_directory_as_journal_returns_one_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.report.main(["--journal", tmp])
        self.assertEqual(code, 1)
        self.assertIn("error:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_unwritable_json_out_returns_one_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "absent-dir" / "metrics.json"
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.report.main(
                    ["--journal", str(FIXTURE), "--json-out", str(target)])
        self.assertEqual(code, 1)
        self.assertIn("error:", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    # --- Finding 14: a zero-trade report rendered plausible-looking zeros ---

    def test_zero_trade_report_renders_rates_as_unavailable(self):
        """Nothing measured must not render as a measured zero.

        `0.00` reads as "breaks even"; `n/a` reads as "no data". Sums are the
        exception: the total of nothing really is zero.
        """
        text = self.report.render_report(
            self.report.build_report([], [], NO_FILTERS))
        core = text.split("CORE", 1)[1].split("COST", 1)[0]

        def line_for(label):
            return next(line for line in core.splitlines()
                        if line.strip().startswith(label))

        for label in ("expectancy / trade", "win rate", "profit factor",
                      "payoff ratio", "avg win", "avg loss", "largest win",
                      "largest loss", "max drawdown"):
            self.assertIn("n/a", line_for(label), label)
        self.assertIn("0.00", line_for("total net"))
        self.assertEqual(line_for("max consecutive losses").strip().split()[-1], "0")

    # --- Findings 17 and 18: group values and warnings pinned independently ---

    def test_variant_full_group_metrics_match_hand_computation(self):
        full = self.report.group_by(self.trades, "variant")["full"]
        self.assertEqual(full["trade_count"], 6)
        self.assertAlmostEqual(full["total_net"], 7.5, places=9)
        self.assertAlmostEqual(full["expectancy"], 1.25, places=9)
        self.assertAlmostEqual(full["profit_factor"], 20.0 / 12.5, places=9)
        self.assertAlmostEqual(full["win_rate"], 2 / 6, places=9)
        self.assertAlmostEqual(full["max_drawdown_abs"], 12.5, places=9)
        self.assertAlmostEqual(full["max_drawdown_pct"], 12.5 / 510 * 100, places=9)

    def test_thursday_group_metrics_match_hand_computation(self):
        thursday = self.report.group_by(self.trades, "weekday")["Thursday"]
        self.assertEqual(thursday["trade_count"], 2)
        self.assertAlmostEqual(thursday["total_net"], -10.0, places=9)
        # Seeds from row 5's balance of 20.0: equity 20 -> 15 -> 10, peak 20.
        self.assertAlmostEqual(thursday["max_drawdown_abs"], 10.0, places=9)
        self.assertAlmostEqual(thursday["max_drawdown_pct"], 50.0, places=9)

    def test_duplicate_position_warning_reaches_the_rendered_report(self):
        rows = [dict(row) for row in self.rows]
        rows[1]["position_id"] = rows[0]["position_id"]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_journal(tmp, rows)
            trades, warnings = self.report.load_journal(path)
        text = self.report.render_report(
            self.report.build_report(trades, warnings, NO_FILTERS))
        self.assertIn("duplicate_position", text)


class NullableFieldsTests(unittest.TestCase):
    """Fields a producer cannot compute must be null, never a plausible zero.

    zone_percent 0.0 means "at the swing low", in_ote false means "evaluated and
    outside the zone", and mfe 0.0 means "price never moved favourably". An EA
    that does not evaluate premium/discount at all would otherwise be forced to
    assert all three, and the analyzer would report those assertions as findings.
    """

    NULLABLE = ("zone_percent", "in_ote", "mfe", "mae")

    def setUp(self):
        self.report = load_report_module()
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_nulls_fixture_conforms_to_schema(self):
        rows = [json.loads(line) for line in
                NULLS_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 3)
        for row in rows:
            jsonschema.validate(row, self.schema)

    def test_schema_declares_exactly_these_fields_nullable(self):
        properties = self.schema["properties"]
        nullable = {
            name for name, spec in properties.items()
            if isinstance(spec.get("type"), list) and "null" in spec["type"]
        }
        self.assertEqual(nullable, set(self.NULLABLE))

    def test_parser_accepts_null_in_nullable_fields(self):
        trades, warnings = self.report.load_journal(NULLS_FIXTURE)
        self.assertEqual(len(trades), 3)
        self.assertEqual([w.code for w in warnings], [])
        first = trades[0]
        for name in self.NULLABLE:
            self.assertIsNone(getattr(first, name), name)

    def test_parser_keeps_non_null_values_alongside_nulls(self):
        trades, _ = self.report.load_journal(NULLS_FIXTURE)
        self.assertAlmostEqual(trades[1].mfe, 2.0, places=9)
        self.assertAlmostEqual(trades[1].mae, -5.0, places=9)
        self.assertAlmostEqual(trades[2].zone_percent, 31.0, places=9)
        self.assertIs(trades[2].in_ote, True)
        self.assertIsNone(trades[1].zone_percent)

    def test_parser_still_rejects_null_in_a_required_number(self):
        rows = [json.loads(line) for line in
                NULLS_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows[0]["net_profit"] = None
        with tempfile.TemporaryDirectory() as tmp:
            path = write_journal(tmp, rows)
            trades, warnings = self.report.load_journal(path)
        self.assertEqual(len(trades), 2)
        self.assertIn("rejected_row", [w.code for w in warnings])

    def test_nulls_fixture_balances_are_internally_consistent(self):
        """Guards the fixture itself: 500 +10 -> 510 -5 -> 505 +8, no divergence.

        Asserts the row count first. Without it this test passes vacuously
        whenever the parser rejects every row, which is exactly the state it is
        meant to detect the absence of.
        """
        trades, warnings = self.report.load_journal(NULLS_FIXTURE)
        self.assertEqual(len(trades), 3)
        self.assertNotIn("balance_divergence", [w.code for w in warnings])
        self.assertNotIn("inconsistent_pnl", [w.code for w in warnings])

    def test_exit_quality_skips_null_excursions(self):
        trades, _ = self.report.load_journal(NULLS_FIXTURE)
        quality = self.report.compute_exit_quality(trades)
        # mfe non-null: 2.0, 8.0 -> median 5.0. Row 1 is skipped, not read as 0.
        self.assertAlmostEqual(quality["median_mfe"], 5.0, places=9)
        # mae non-null: -5.0, -1.0 -> median -3.0
        self.assertAlmostEqual(quality["median_mae"], -3.0, places=9)
        # Row 3 only: mfe 8.0 >= |2656-2648|. Row 1 has no mfe, so it cannot count.
        self.assertEqual(quality["tp_reached_before_sl_count"], 1)
        # Row 2 only: mae -5.0 <= -|2652-2647|
        self.assertEqual(quality["sl_exceeded_before_favourable_count"], 1)

    def test_all_null_excursions_give_zero_medians_not_a_crash(self):
        trades, _ = self.report.load_journal(NULLS_FIXTURE)
        quality = self.report.compute_exit_quality(trades[:1])
        self.assertEqual(quality["median_mfe"], 0.0)
        self.assertEqual(quality["median_mae"], 0.0)
        self.assertEqual(quality["tp_reached_before_sl_count"], 0)
        self.assertEqual(quality["sl_exceeded_before_favourable_count"], 0)

    def test_report_renders_a_journal_with_nulls(self):
        trades, warnings = self.report.load_journal(NULLS_FIXTURE)
        report = self.report.build_report(trades, warnings, NO_FILTERS)
        json.dumps(report, allow_nan=False)
        text = self.report.render_report(report)
        self.assertIn("DATA QUALITY", text)
        self.assertIn("3", text)


MQL5_FIXTURE = ROOT / "tests/fixtures/trade-journal-v1-mql5.jsonl"


class Mql5CrossLanguageTests(unittest.TestCase):
    """Bytes produced by CSmcTradeJournal, loaded by the Python analyzer.

    This fixture is captured output from tests/TestTradeJournal.mq5, not
    hand-written, so it is the only place where the two sides of the contract
    are proved to agree. The MQL5 test pins the same bytes by length and by
    two exact substrings; if serialization changes, both must be re-captured.
    """

    def setUp(self):
        self.report = load_report_module()
        self.trades, self.warnings = self.report.load_journal(MQL5_FIXTURE)

    def test_every_record_is_accepted_without_rejection(self):
        self.assertEqual(len(self.trades), 2)
        codes = [w.code for w in self.warnings]
        for fatal in ("rejected_row", "truncated_line", "inconsistent_pnl"):
            self.assertNotIn(fatal, codes)

    def test_only_the_warnings_the_fixture_deliberately_causes_appear(self):
        """The nulls record reuses the baseline's id and balance on purpose.

        Asserting the exact set matters: an empty assertion would also pass if
        the parser silently dropped a record.
        """
        self.assertEqual(sorted(w.code for w in self.warnings),
                         ["balance_divergence", "duplicate_position"])

    def test_rows_conform_to_the_published_schema(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for line in MQL5_FIXTURE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                jsonschema.validate(json.loads(line), schema)

    def test_mql5_number_precision_survives_the_round_trip(self):
        """The captured bytes carry the exact binary64 spelling.

        -0.34999999999999998 is the double nearest -0.35, and %.17g reproduces
        it exactly. Note this does not prove 17 digits are *needed* for the
        P&L identity: the analyzer allows a one-cent tolerance, so %.2f would
        also satisfy it. The genuine precision pin is the MQL5-side 0.1 + 0.2
        case, which no shorter format survives.
        """
        self.assertEqual(self.trades[0].commission, -0.34999999999999998)
        self.assertEqual(
            self.trades[0].gross_profit + self.trades[0].commission + self.trades[0].swap,
            self.trades[0].net_profit)
        self.assertEqual(self.trades[0].entry_price, 2650.0)

    def test_nulls_from_mql5_arrive_as_none_not_zero(self):
        self.assertEqual(self.trades[0].mfe, 10.0)
        self.assertIs(self.trades[0].in_ote, True)
        self.assertEqual(self.trades[0].zone_percent, 32.5)
        for name in ("mfe", "mae", "zone_percent", "in_ote"):
            self.assertIsNone(getattr(self.trades[1], name), name)

    def test_broker_local_timestamps_parse_without_a_timezone(self):
        opened = self.trades[0].open_time
        self.assertEqual((opened.year, opened.month, opened.day), (2026, 9, 21))
        self.assertEqual((opened.hour, opened.minute, opened.second), (8, 0, 0))
        self.assertIsNone(opened.tzinfo)

    def test_config_hash_is_the_full_64_bit_value(self):
        # Captured before the %016x truncation fix this was "0000000052c20a64".
        self.assertEqual(self.trades[0].config_hash, "ad40da8852c20a64")
        self.assertNotEqual(self.trades[0].config_hash[:8], "00000000")

    def test_metrics_compute_over_mql5_output(self):
        metrics = self.report.compute_metrics(self.trades)
        self.assertEqual(metrics["trade_count"], 2)
        self.assertAlmostEqual(metrics["total_net"], 20.0, places=9)
        self.assertAlmostEqual(metrics["win_rate"], 1.0, places=9)
        self.assertEqual(metrics["profit_factor"], float("inf"))
        quality = self.report.compute_exit_quality(self.trades)
        # Only the baseline record has an mfe, so it alone can reach its target.
        self.assertEqual(quality["tp_reached_before_sl_count"], 1)
        self.assertEqual(quality["sl_exceeded_before_favourable_count"], 0)
        self.assertAlmostEqual(quality["median_mfe"], 10.0, places=9)
        self.assertAlmostEqual(quality["median_mae"], -1.2, places=9)

    def test_risk_verdict_needs_a_loser_and_this_fixture_has_none(self):
        verdict = self.report.compute_risk_verdict(self.trades)
        self.assertEqual(verdict["n_losers"], 0)
        self.assertEqual(verdict["verdict"], "insufficient_data")

    def test_schema_session_enum_matches_the_mql5_tokens(self):
        """Two-sided pin on the session vocabulary.

        CSmcKillZone::GetSessionName returns "New York", "LDN-NY Overlap" and
        "None" -- three of the five values this enum rejects -- so filling the
        journal's `session` field from it would silently drop most trades into
        rejected_row. CSmcTradeJournal::SessionToken is the mapping to use, and
        TestTradeJournal.mq5 pins the same six tokens from the MQL5 side.
        """
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["session"]["enum"],
                         ["Asian", "London", "NewYork", "ldn_ny_overlap",
                          "none", "unknown"])

    def test_schema_trend_enum_matches_the_mql5_tokens(self):
        """SmcDirectionName returns "neutral"; the contract's token is "ranging"."""
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["htf_trend"]["enum"],
                         ["bullish", "bearish", "ranging", "unknown"])

    def test_report_renders_mql5_output(self):
        built = self.report.build_report(self.trades, self.warnings, NO_FILTERS)
        json.dumps(built, allow_nan=False)
        self.assertIn("Trade Journal Performance Report",
                      self.report.render_report(built))


if __name__ == "__main__":
    unittest.main()
