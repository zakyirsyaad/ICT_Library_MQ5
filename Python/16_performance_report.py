"""
16 - Performance Report / Trade Journal Analyzer
================================================
Reads a trade journal (JSONL, one closed trade per line) conforming to
`schemas/trade-journal.schema.json` and reports expectancy, cost, realized risk,
and exit quality.

Detection and journaling stay in MQL5; this script only consumes their output,
following the same division as the six readers under `examples/`.

Shape contract: `schemas/trade-journal.schema.json`. Validation here is explicit
rather than a per-row jsonschema call so large journals stay fast; the test
`test_trade_fields_match_schema_required` keeps the two field lists from drifting.

Timestamps are broker-local wall-clock values with no UTC suffix or offset, so
they are never converted or reinterpreted as UTC instants.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

JOURNAL_VERSION = "1.0"
MIN_TRADES_FOR_VERDICT = 100

# One cent. Money comparisons in this module are equality checks on values a
# terminal rounded independently, so they need a tolerance rather than `==`.
MONEY_TOLERANCE = 0.01

# Same pattern as $defs.timestamp in schemas/snapshot.schema.json: broker-local
# wall-clock, no "Z" and no offset.
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d$"
)

_UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")


@dataclass(frozen=True)
class Trade:
    """One closed trade, exactly as the journal records it."""

    ticket: int
    position_id: int
    symbol: str
    timeframe: str
    magic: int
    variant: str
    config_hash: str
    open_time: datetime
    close_time: datetime
    direction: int
    bars_held: int
    lot: float
    entry_price: float
    sl: float
    tp: float
    exit_price: float
    exit_reason: str
    spread_at_entry: float
    slippage_points: float
    gross_profit: float
    commission: float
    swap: float
    net_profit: float
    balance_at_open: float
    equity_at_open: float
    margin_used: float
    intended_risk_pct: float
    realized_risk_pct: float
    mfe: float
    mae: float
    htf_trend: str
    zone_percent: float
    in_ote: bool
    zone_types: str
    trigger: str
    session: str
    confluence_score: float
    confluence_count: int


TRADE_FIELDS = tuple(field.name for field in fields(Trade))

_INT_FIELDS = ("ticket", "position_id", "magic", "direction", "bars_held", "confluence_count")
_NUM_FIELDS = (
    "lot", "entry_price", "sl", "tp", "exit_price", "spread_at_entry", "slippage_points",
    "gross_profit", "commission", "swap", "net_profit", "balance_at_open", "equity_at_open",
    "margin_used", "intended_risk_pct", "realized_risk_pct", "mfe", "mae", "zone_percent",
    "confluence_score",
)
_STR_FIELDS = (
    "symbol", "timeframe", "variant", "config_hash", "exit_reason", "htf_trend",
    "zone_types", "trigger", "session",
)
_BOOL_FIELDS = ("in_ote",)
_TIME_FIELDS = ("open_time", "close_time")

_ENUMS = {
    "direction": (1, -1),
    "exit_reason": ("sl", "tp", "signal", "time_stop", "be_stop", "manual", "unknown"),
    "htf_trend": ("bullish", "bearish", "ranging", "unknown"),
    "trigger": ("displacement", "mss", "po3_distribution", "liq_sweep", "none", "unknown"),
    "session": ("Asian", "London", "NewYork", "none", "unknown"),
}

_POSITIVE = ("lot", "entry_price", "exit_price", "balance_at_open", "equity_at_open")
_NON_NEGATIVE = (
    "sl", "tp", "spread_at_entry", "margin_used", "intended_risk_pct",
    "realized_risk_pct", "mfe", "bars_held", "confluence_count",
)
_NON_POSITIVE = ("mae",)


@dataclass(frozen=True)
class JournalWarning:
    """A non-fatal problem with a journal.

    `line_number` is None for a warning about the journal as a whole rather than
    one row. Empty string, None, 0 and an absent field all mean different things
    in this contract, so a warning is never a substitute for a rejected row.
    """

    line_number: int | None
    code: str
    message: str


class JournalFormatError(Exception):
    """The journal cannot be read as UTF-8 text at all."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not _TIMESTAMP_RE.match(value):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _validate_types(row: dict, problems: list[str]) -> None:
    for name in _INT_FIELDS:
        if not _is_int(row[name]):
            problems.append(f"{name} must be an integer")
    for name in _NUM_FIELDS:
        if not _is_number(row[name]):
            problems.append(f"{name} must be a number")
        elif not math.isfinite(row[name]):
            problems.append(f"{name} must be finite")
    for name in _STR_FIELDS:
        if not isinstance(row[name], str) or not row[name]:
            problems.append(f"{name} must be a non-empty string")
    for name in _BOOL_FIELDS:
        if not isinstance(row[name], bool):
            problems.append(f"{name} must be a boolean")
    for name, allowed in _ENUMS.items():
        if name in row and row[name] not in allowed:
            problems.append(f"{name} must be one of {allowed}, got {row[name]!r}")


def _validate_ranges(row: dict, problems: list[str]) -> None:
    for name in _POSITIVE:
        if _is_number(row[name]) and row[name] <= 0:
            problems.append(f"{name} must be greater than zero")
    for name in _NON_NEGATIVE:
        if _is_number(row[name]) and row[name] < 0:
            problems.append(f"{name} must not be negative")
    for name in _NON_POSITIVE:
        if _is_number(row[name]) and row[name] > 0:
            problems.append(f"{name} must not be positive")


def _validate_invariants(parsed: dict, problems: list[str]) -> None:
    open_time = parsed["open_time"]
    close_time = parsed["close_time"]
    if open_time is None or close_time is None:
        problems.append("open_time and close_time must be broker-local YYYY-MM-DDTHH:MM:SS")
    elif close_time < open_time:
        problems.append(f"close_time {close_time} precedes open_time {open_time}")


def _parse_row(row: object, line_number: int) -> tuple[Trade | None, list[JournalWarning]]:
    if not isinstance(row, dict):
        return None, [JournalWarning(line_number, "rejected_row", "row is not a JSON object")]

    warnings: list[JournalWarning] = []
    unknown = sorted(set(row) - set(TRADE_FIELDS) - {"journal_version"})
    if unknown:
        warnings.append(JournalWarning(
            line_number, "unknown_field",
            "ignoring unrecognized fields for forward compatibility: " + ", ".join(unknown),
        ))

    problems: list[str] = []
    version = row.get("journal_version")
    if version != JOURNAL_VERSION:
        problems.append(f"journal_version must be {JOURNAL_VERSION!r}, got {version!r}")

    missing = [name for name in TRADE_FIELDS if name not in row]
    if missing:
        problems.append("missing required fields: " + ", ".join(missing))

    if problems:
        return None, warnings + [JournalWarning(line_number, "rejected_row", "; ".join(problems))]

    parsed = {name: _parse_timestamp(row[name]) for name in _TIME_FIELDS}
    _validate_types(row, problems)
    _validate_ranges(row, problems)
    _validate_invariants(parsed, problems)
    if problems:
        return None, warnings + [JournalWarning(line_number, "rejected_row", "; ".join(problems))]

    values = {name: row[name] for name in TRADE_FIELDS if name not in _TIME_FIELDS}
    values.update(parsed)
    trade = Trade(**values)
    expected_net = trade.gross_profit + trade.commission + trade.swap
    if abs(trade.net_profit - expected_net) > MONEY_TOLERANCE:
        # A warning, not a rejection: the row is usable, but CORE and COST would
        # quietly contradict each other and expectancy is built on net_profit.
        warnings.append(JournalWarning(
            line_number, "inconsistent_pnl",
            f"net_profit {trade.net_profit} does not equal gross_profit + commission "
            f"+ swap ({expected_net:.2f})",
        ))
    return trade, warnings


def _balance_divergence(trades: Sequence[Trade]) -> JournalWarning | None:
    """Compare the reconstructed equity curve against each recorded balance.

    The drawdown metrics rebuild equity from the first trade's balance plus each
    net result. That is exact for one strategy on one account with no cash
    movements, and silently wrong otherwise — a deposit, a withdrawal, or a
    second strategy sharing the account all break it while every row's real
    balance sits unread in the data. One aggregated warning rather than one per
    row, so a long journal does not bury its other warnings.
    """
    ordered = _chronological(trades)
    if not ordered:
        return None
    equity = ordered[0].balance_at_open
    count = 0
    first_ticket = None
    for index, trade in enumerate(ordered):
        if index and abs(trade.balance_at_open - equity) > MONEY_TOLERANCE:
            count += 1
            if first_ticket is None:
                first_ticket = trade.ticket
        equity += trade.net_profit
    if not count:
        return None
    return JournalWarning(
        None, "balance_divergence",
        f"balance_at_open diverges from the reconstructed equity curve on {count} of "
        f"{len(ordered)} trades (first at ticket {first_ticket}); the drawdown figures "
        "assume one account with no deposit, withdrawal, or second strategy, and a "
        "cash movement or shared account breaks that assumption",
    )


def load_journal(path: Path) -> tuple[list[Trade], list[JournalWarning]]:
    """Read a JSONL trade journal into trades plus non-fatal warnings.

    Rows are returned in file order; sorting is the metrics layer's concern.
    A malformed line yields a warning and is skipped, never an exception, so a
    partial write from an interrupted terminal cannot lose the rest of the run.

    The file is streamed rather than read whole. Holding the bytes, the decoded
    text, and a list of every line at once costs roughly four times the file
    size, and a multi-year M1 backtest can produce a journal of hundreds of
    megabytes.
    """
    path = Path(path)
    with path.open("rb") as probe:
        if probe.read(2) in _UTF16_BOMS:
            raise JournalFormatError(
                f"{path}: file is UTF-16 encoded (BOM detected). MT5 writes UTF-16 "
                "when a handle is opened with FILE_UNICODE; reopen the journal "
                "without FILE_UNICODE so it is written as UTF-8."
            )

    trades: list[Trade] = []
    warnings: list[JournalWarning] = []
    first_seen: dict[int, int] = {}
    last_line = 0
    try:
        # utf-8-sig strips a leading BOM when present and is byte-identical
        # otherwise; without it a BOM makes line 1 unparsable and silently drops
        # the first trade. Iterating the handle splits only on newlines, so a
        # U+2028 inside a JSON string cannot split a record the way splitlines().
        with path.open(encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                last_line = line_number
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    warnings.append(JournalWarning(
                        line_number, "truncated_line",
                        f"discarded a line that is not valid JSON ({exc.msg}, "
                        f"column {exc.colno})",
                    ))
                    continue
                trade, row_warnings = _parse_row(row, line_number)
                warnings.extend(row_warnings)
                if trade is None:
                    continue
                earlier = first_seen.get(trade.position_id)
                if earlier is None:
                    first_seen[trade.position_id] = line_number
                else:
                    warnings.append(JournalWarning(
                        line_number, "duplicate_position",
                        f"position_id {trade.position_id} was already recorded on line "
                        f"{earlier}; both rows are kept, so trade counts may be inflated",
                    ))
                trades.append(trade)
    except UnicodeDecodeError as exc:
        raise JournalFormatError(
            f"{path}: not valid UTF-8 ({exc}). If the journal came from MT5, check "
            "whether it was written with FILE_UNICODE, which produces UTF-16."
        ) from exc

    # Only a partial final line is plausibly an interrupted write. Blaming every
    # unparsable line on one sends the reader looking for a crash that never
    # happened and away from the real cause, which is a corrupt producer or edit.
    if last_line:
        warnings = [
            JournalWarning(
                warning.line_number, warning.code,
                warning.message + "; an interrupted write leaves a partial trailing line",
            ) if warning.code == "truncated_line" and warning.line_number == last_line
            else warning
            for warning in warnings
        ]

    divergence = _balance_divergence(trades)
    if divergence is not None:
        warnings.append(divergence)
    return trades, warnings


def _chronological(trades: Sequence[Trade]) -> list[Trade]:
    """Order trades by close time, breaking ties deterministically on ticket."""
    return sorted(trades, key=lambda trade: (trade.close_time, trade.ticket))


def _ratio(numerator: float, denominator: float) -> float:
    """Divide without raising; an undefined ratio reports infinity or zero.

    A zero denominator with a positive numerator means "no losing side exists",
    which is unbounded rather than unknown, so it reports infinity. Both zero
    means there is nothing to compare.
    """
    if denominator:
        return numerator / denominator
    return float("inf") if numerator > 0 else 0.0


def compute_metrics(trades: Sequence[Trade]) -> dict[str, object]:
    """Compute performance metrics for a set of closed trades.

    Conventions, all pinned by tests:

    - A win is `net_profit > 0` and a loss is `net_profit < 0`. A break-even
      trade (`net_profit == 0.0`) is neither, so it does not inflate the win
      rate and it breaks a losing streak.
    - Streaks and the equity curve use `close_time` order, not file order,
      because that is the order capital actually moved in.
    - `cost_pct_of_gross` counts commission and swap only. Spread is already
      inside the entry and exit prices that produced `gross_profit`, so adding
      it would double-count; spread is reported separately in pips. It is `None`
      when gross is not positive, because a share of a non-positive gross is
      undefined and a losing run is an expected outcome, not an edge case.
    - `max_drawdown_pct` is the percentage of the *same* drawdown that
      `max_drawdown_abs` measures, taken at the peak that preceded it. The two
      always describe one real event.
    - Every rate degrades to 0.0 on an empty input rather than raising.
    """
    ordered = _chronological(trades)
    count = len(ordered)
    wins = [trade.net_profit for trade in ordered if trade.net_profit > 0]
    losses = [trade.net_profit for trade in ordered if trade.net_profit < 0]

    total_net = math.fsum(trade.net_profit for trade in ordered)
    total_gross = math.fsum(trade.gross_profit for trade in ordered)
    total_commission = math.fsum(trade.commission for trade in ordered)
    total_swap = math.fsum(trade.swap for trade in ordered)
    gross_wins = math.fsum(wins)
    gross_losses = math.fsum(losses)

    avg_win = gross_wins / len(wins) if wins else 0.0
    avg_loss = gross_losses / len(losses) if losses else 0.0

    streak = 0
    worst_streak = 0
    for trade in ordered:
        if trade.net_profit < 0:
            streak += 1
            worst_streak = max(worst_streak, streak)
        else:
            streak = 0

    drawdown_abs = 0.0
    drawdown_pct = 0.0
    if ordered:
        equity = ordered[0].balance_at_open
        peak = equity
        for trade in ordered:
            equity += trade.net_profit
            peak = max(peak, equity)
            gap = peak - equity
            if gap > drawdown_abs:
                drawdown_abs = gap
                # Captured at the point the absolute maximum is set, so the pair
                # describes one real drawdown. Tracking the two independently
                # would report the worst absolute gap alongside the worst
                # percentage from a different, possibly much smaller event —
                # which on a growing account overstates risk systematically.
                drawdown_pct = gap / peak * 100 if peak > 0 else 0.0

    cost = -(total_commission + total_swap)
    return {
        "trade_count": count,
        "win_rate": len(wins) / count if count else 0.0,
        "expectancy": total_net / count if count else 0.0,
        "profit_factor": _ratio(gross_wins, -gross_losses),
        "payoff_ratio": _ratio(avg_win, -avg_loss),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "largest_win": max(wins) if wins else 0.0,
        "largest_loss": min(losses) if losses else 0.0,
        "max_consecutive_losses": worst_streak,
        "max_drawdown_abs": drawdown_abs,
        "max_drawdown_pct": drawdown_pct,
        "total_net": total_net,
        "total_gross": total_gross,
        "total_spread_pips": math.fsum(trade.spread_at_entry for trade in ordered),
        "total_commission": total_commission,
        "total_swap": total_swap,
        # Undefined once gross is not positive, and a losing run is an expected
        # outcome rather than an edge case. Reporting 0.0 there would state that
        # costs consumed none of the result, which is a definite-looking answer
        # to a question that has no answer. An empty journal is genuinely 0.0.
        "cost_pct_of_gross": (
            cost / total_gross * 100 if total_gross > 0 else (0.0 if not count else None)
        ),
        "sufficient_data": count >= MIN_TRADES_FOR_VERDICT,
    }


def compute_risk_verdict(trades: Sequence[Trade],
                         max_risk_pct: float = 2.0) -> dict[str, object]:
    """Report whether realized risk per losing trade stayed inside max_risk_pct.

    This answers a structural question, not a statistical one: whether the
    broker's minimum lot step lets the EA actually achieve the risk it asked
    for. A single trade losing 25% against an intended 1% already proves it
    cannot, so the verdict deliberately ignores MIN_TRADES_FOR_VERDICT — that
    threshold gates expectancy, where sample size really does matter.

    Only losing trades are measured. A winning trade's realized risk is 0 by
    definition, and averaging winners in would hide the sizing problem.
    `exceedances` counts losers whose realized risk strictly exceeds their own
    intended risk, so a trade landing exactly on target does not count.
    """
    losers = [trade for trade in trades if trade.net_profit < 0]
    realized = [trade.realized_risk_pct for trade in losers]
    if not losers:
        return {
            "n_losers": 0,
            "realized_min": 0.0,
            "realized_median": 0.0,
            "realized_max": 0.0,
            "exceedances": 0,
            "max_risk_pct": max_risk_pct,
            "verdict": "insufficient_data",
        }
    realized_max = max(realized)
    return {
        "n_losers": len(losers),
        "realized_min": min(realized),
        "realized_median": statistics.median(realized),
        "realized_max": realized_max,
        "exceedances": sum(
            1 for trade in losers
            if trade.realized_risk_pct > trade.intended_risk_pct
        ),
        "max_risk_pct": max_risk_pct,
        "verdict": "risk_exceeded" if realized_max > max_risk_pct else "risk_contained",
    }


EXIT_REASONS = _ENUMS["exit_reason"]

# strftime("%A") follows the process locale, so weekday names come from a fixed
# table. Without it the same journal would render different group labels on a
# machine with a non-English locale, and the golden output would not reproduce.
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_DERIVED_KEYS = ("hour", "weekday")
GROUPABLE_KEYS = (
    "variant", "session", "htf_trend", "trigger", "exit_reason",
) + _DERIVED_KEYS


def _group_value(trade: Trade, key: str) -> str:
    if key == "hour":
        # Zero-padded so lexicographic order equals chronological order.
        return f"{trade.open_time.hour:02d}"
    if key == "weekday":
        return _WEEKDAYS[trade.open_time.weekday()]
    return str(getattr(trade, key))


def _group_sort_key(key: str, name: str) -> tuple[int, str]:
    if key == "weekday":
        return (_WEEKDAYS.index(name), name)
    return (0, name)


def group_by(trades: Sequence[Trade], key: str) -> dict[str, dict[str, object]]:
    """Partition trades and report full metrics for each subset.

    Each value is exactly `compute_metrics` for that subset, so `trade_count`
    is already the sample size — no separate `n` is added, and no metric formula
    is duplicated here.

    `hour` and `weekday` derive from `open_time`, which is broker-local wall
    clock. It is never converted to another time zone: the session labels a
    broker reports are the ones being analysed.

    An unknown key raises rather than returning an empty dict, because a silent
    empty result reads as "no differences found" instead of "wrong key".
    """
    if key not in GROUPABLE_KEYS:
        raise ValueError(
            f"unknown grouping key {key!r}; expected one of {', '.join(GROUPABLE_KEYS)}"
        )
    buckets: dict[str, list[Trade]] = {}
    for trade in trades:
        buckets.setdefault(_group_value(trade, key), []).append(trade)
    return {
        name: compute_metrics(buckets[name])
        for name in sorted(buckets, key=lambda item: _group_sort_key(key, item))
    }


def compute_exit_quality(trades: Sequence[Trade]) -> dict[str, object]:
    """Report how exits behaved relative to the levels that were set.

    `tp_reached_before_sl_count` counts trades whose favourable excursion
    reached at least the target distance. It deliberately ignores whether the
    trade was profitable: the question this answers is "is the target placed
    beyond where price actually travels", which is about price, while
    `net_profit` is about cost. Coupling them would understate target reach
    precisely on small accounts, where commission is large relative to gross,
    and push a reader to move the target closer for the wrong reason. A low
    count relative to trade_count suggests targets are too far.

    `sl_exceeded_before_favourable_count` counts trades whose adverse excursion
    reached the stop distance. Comparing it against the winners among them
    suggests whether stops sit too close to entry.

    Both counts skip trades with no level set (`tp` or `sl` of 0), because 0
    means "unset" in this contract and is not a price at distance zero.

    Medians are used rather than means so a single excursion outlier cannot
    drag the summary. `exit_reason_counts` always emits every reason in
    `EXIT_REASONS` order, including zeros, so the rendered report has a stable
    row set across runs.
    """
    counts = {reason: 0 for reason in EXIT_REASONS}
    for trade in trades:
        counts[trade.exit_reason] += 1
    mfe_values = [trade.mfe for trade in trades]
    mae_values = [trade.mae for trade in trades]
    return {
        "exit_reason_counts": counts,
        "tp_reached_before_sl_count": sum(
            1 for trade in trades
            if trade.tp > 0
            and trade.mfe >= abs(trade.tp - trade.entry_price)
        ),
        "sl_exceeded_before_favourable_count": sum(
            1 for trade in trades
            if trade.sl > 0 and trade.mae <= -abs(trade.entry_price - trade.sl)
        ),
        "median_mfe": statistics.median(mfe_values) if mfe_values else 0.0,
        "median_mae": statistics.median(mae_values) if mae_values else 0.0,
    }


DEFAULT_MAX_RISK_PCT = 2.0

# Rendered in the report. exit_reason is deliberately absent: its distribution is
# already reported by exit_quality, and repeating it would show the same numbers twice.
GROUP_KEYS = ("variant", "session", "htf_trend", "trigger", "hour", "weekday")

_GROUP_COLUMNS = ("n", "expectancy", "win rate", "pf", "max dd", "net")


def _json_safe(value: object) -> object:
    """Replace non-finite floats so the report stays valid strict JSON.

    json.dumps renders infinity as the bare literal `Infinity`, which is not JSON
    and is rejected by jq and by browsers. Infinite ratios are common rather than
    exceptional here, because group_by scores small subsets: a group holding one
    winning trade has no losing side at all.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return value
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def build_report(trades: Sequence[Trade],
                 warnings: Sequence[JournalWarning],
                 filters: Mapping[str, object]) -> dict[str, object]:
    """Assemble the JSON-serializable report consumed by render_report.

    `filters` is echoed into the report unchanged. It is recorded because a saved
    report is only interpretable alongside the parameters that produced it — the
    same reason each trade carries a config_hash.

    Filtering by variant happens in main() before this call, so the trades passed
    in are already the selected subset; build_report does not filter again.
    """
    settings = dict(filters)
    threshold = settings.get("max_risk_pct", DEFAULT_MAX_RISK_PCT)
    return _json_safe({
        "metrics": compute_metrics(trades),
        "filters": settings,
        "risk_verdict": compute_risk_verdict(trades, threshold),
        "exit_quality": compute_exit_quality(trades),
        "groups": {name: group_by(trades, name) for name in GROUP_KEYS},
        "warnings": [
            {"line_number": warning.line_number, "code": warning.code,
             "message": warning.message}
            for warning in warnings
        ],
    })


def _number(value: object, decimals: int = 2) -> str:
    """Format a value that may already be a JSON-safe "inf" placeholder string."""
    if isinstance(value, str):
        return value
    if value is None:
        return "n/a"
    return f"{value:.{decimals}f}"


def _pct(value: object) -> str:
    """Format a value already expressed in percent units."""
    if isinstance(value, str) or value is None:
        return _number(value)
    return f"{value:.2f}%"


def _fraction_as_pct(value: object) -> str:
    """Format a 0..1 fraction as a percentage.

    Kept separate from _pct on purpose: win_rate is a fraction while
    max_drawdown_pct, cost_pct_of_gross and the realized-risk values are already
    percentages. Multiplying the latter by 100 again is the easy mistake here.
    """
    if isinstance(value, str) or value is None:
        return _number(value)
    return f"{value * 100:.2f}%"


def _key_value(pairs: Sequence[tuple[str, str]]) -> list[str]:
    width = max(len(label) for label, _ in pairs)
    return [f"  {label.ljust(width)} : {value}" for label, value in pairs]


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Render an aligned table: first column left, remaining columns right."""
    widths = [len(cell) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def render(cells: Sequence[str]) -> str:
        parts = [cells[0].ljust(widths[0])]
        parts += [cell.rjust(widths[index]) for index, cell in enumerate(cells) if index]
        return "  " + "  ".join(parts)

    return [render(header)] + [render(row) for row in rows]


def _group_section(key: str, groups: Mapping[str, Mapping[str, object]]) -> list[str]:
    rows = [
        [name,
         str(metrics["trade_count"]),
         _number(metrics["expectancy"]),
         _fraction_as_pct(metrics["win_rate"]),
         _number(metrics["profit_factor"]),
         _pct(metrics["max_drawdown_pct"]),
         _number(metrics["total_net"])]
        for name, metrics in groups.items()
    ]
    return [f"BY {key}"] + _table((key, *_GROUP_COLUMNS), rows)


MAX_RENDERED_WARNINGS = 20


def _warning_lines(warnings: Sequence[Mapping[str, object]]) -> list[str]:
    """Render warnings, capped, with a per-code rollup for the remainder.

    An uncapped list is not a cosmetic problem. A large journal whose rows all
    carry one forward-compatible extra field, or all carry a newer
    journal_version, produces one warning per row; tens of thousands of them
    push every metric megabytes below the DATA QUALITY header that is supposed
    to be read first.
    """
    shown = warnings[:MAX_RENDERED_WARNINGS]
    lines = []
    for warning in shown:
        number = warning["line_number"]
        where = "journal" if number is None else f"line {number}"
        lines.append(f"    {where} [{warning['code']}] {warning['message']}")
    hidden = len(warnings) - len(shown)
    if hidden:
        rollup = ", ".join(
            f"{count} x {code}"
            for code, count in sorted(Counter(w["code"] for w in warnings).items())
        )
        lines.append(f"    ... {hidden} more not shown ({rollup})")
    return lines


def render_report(report: Mapping[str, object]) -> str:
    """Render a built report as human-readable text ending in a single newline.

    Data quality comes first, and it should be read first: a reader must see the
    sample size, whether it is sufficient, and any journal warnings before seeing
    any performance number. A metric read without that context is not a
    conclusion.

    Rate metrics render as `n/a` when there are no trades. `0.00` would read as
    "measured, and it breaks even" rather than "nothing was measured". Sums keep
    `0.00`, because the total of nothing genuinely is zero.
    """
    metrics = report["metrics"]
    verdict = report["risk_verdict"]
    quality = report["exit_quality"]
    report_warnings = report["warnings"]
    trade_count = metrics["trade_count"]
    unmeasured = trade_count == 0

    def rate(value: object, formatter=_number) -> str:
        return "n/a" if unmeasured else formatter(value)

    sufficient = "yes" if metrics["sufficient_data"] else (
        f"no - insufficient ({trade_count} of {MIN_TRADES_FOR_VERDICT} trades "
        "required for a statistical verdict)"
    )

    lines = ["Trade Journal Performance Report", "", "DATA QUALITY"]
    lines += _key_value([
        ("trades analysed", str(trade_count)),
        ("sufficient data", sufficient),
        ("warnings", str(len(report_warnings))),
    ])
    lines += _warning_lines(report_warnings)

    lines += ["", "CORE"]
    lines += _key_value([
        ("expectancy / trade", rate(metrics["expectancy"])),
        ("win rate", rate(metrics["win_rate"], _fraction_as_pct)),
        ("profit factor", rate(metrics["profit_factor"])),
        ("payoff ratio", rate(metrics["payoff_ratio"])),
        ("avg win", rate(metrics["avg_win"])),
        ("avg loss", rate(metrics["avg_loss"])),
        ("largest win", rate(metrics["largest_win"])),
        ("largest loss", rate(metrics["largest_loss"])),
        ("max consecutive losses", str(metrics["max_consecutive_losses"])),
        ("max drawdown", "n/a" if unmeasured else
         f"{_number(metrics['max_drawdown_abs'])} ({_pct(metrics['max_drawdown_pct'])})"),
        ("total net", _number(metrics["total_net"])),
    ])

    lines += ["", "COST"]
    lines += _key_value([
        ("total gross", _number(metrics["total_gross"])),
        ("commission", _number(metrics["total_commission"])),
        ("swap", _number(metrics["total_swap"])),
        ("cost as % of gross", _pct(metrics["cost_pct_of_gross"])),
        ("spread paid (pips)", _number(metrics["total_spread_pips"])),
    ])

    lines += ["", "RISK REALIZATION"]
    lines += _key_value([
        ("losing trades", str(verdict["n_losers"])),
        ("realized risk min", _pct(verdict["realized_min"])),
        ("realized risk median", _pct(verdict["realized_median"])),
        ("realized risk max", _pct(verdict["realized_max"])),
        ("exceeded intended risk", str(verdict["exceedances"])),
        ("threshold", _pct(verdict["max_risk_pct"])),
        ("verdict", str(verdict["verdict"])),
    ])

    lines += ["", "EXIT QUALITY"]
    lines += _key_value([
        ("target reached", str(quality["tp_reached_before_sl_count"])),
        ("stop breached", str(quality["sl_exceeded_before_favourable_count"])),
        ("median MFE", _number(quality["median_mfe"])),
        ("median MAE", _number(quality["median_mae"])),
    ])
    counts = quality["exit_reason_counts"]
    lines.append("  exit reasons : " + " ".join(
        f"{reason}={counts[reason]}" for reason in EXIT_REASONS))

    for key in GROUP_KEYS:
        lines += ["", *_group_section(key, report["groups"][key])]

    filters = report["filters"]
    lines += ["", "FILTERS"]
    lines += _key_value([
        ("variant", "(all)" if filters.get("variant") is None else str(filters["variant"])),
        ("max risk %", _number(filters.get("max_risk_pct", DEFAULT_MAX_RISK_PCT))),
    ])
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point.

    Exit codes: 0 on success, 1 when the journal is missing, unreadable, or
    yielded no usable row, and 2 for invalid arguments (raised by argparse).

    A variant that matches no trades is a successful run reporting zero trades,
    not an error: "this configuration produced nothing" is a finding. A journal
    whose rows were all rejected is different — the file had content and none of
    it could be read, which is a failure to report rather than a result.
    """
    parser = argparse.ArgumentParser(
        description="Report performance metrics from an MQL5 trade journal.",
    )
    parser.add_argument("--journal", required=True, type=Path,
                        help="path to a trade journal .jsonl file")
    parser.add_argument("--variant", default=None,
                        help="analyse only trades carrying this ablation label")
    parser.add_argument("--json-out", type=Path, default=None,
                        help="also write the report as strict JSON to this path")
    parser.add_argument("--max-risk-pct", type=float, default=DEFAULT_MAX_RISK_PCT,
                        help="realized-risk threshold for the verdict "
                             f"(default {DEFAULT_MAX_RISK_PCT})")
    args = parser.parse_args(argv)

    try:
        trades, warnings = load_journal(args.journal)
    except JournalFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(f"error: journal not found: {args.journal}", file=sys.stderr)
        return 1
    except OSError as exc:
        # A directory, a permission problem, or a vanished mount. Without this
        # the user gets a traceback, and the documented exit code 1 would only
        # hold by accident via SystemExit(main()).
        print(f"error: cannot read journal {args.journal}: {exc}", file=sys.stderr)
        return 1

    if not trades and warnings:
        rollup = ", ".join(f"{count} x {code}" for code, count in
                           sorted(Counter(w.code for w in warnings).items()))
        print(f"error: every journal line was rejected or unreadable ({rollup}); "
              "no trades to report. A journal_version mismatch against a newer "
              "producer is the usual cause.", file=sys.stderr)
        return 1

    if args.variant is not None:
        trades = [trade for trade in trades if trade.variant == args.variant]

    report = build_report(
        trades, warnings,
        {"variant": args.variant, "max_risk_pct": args.max_risk_pct},
    )
    # write, not print: render_report already ends in exactly one newline.
    sys.stdout.write(render_report(report))

    if args.json_out is not None:
        try:
            # allow_nan=False makes the strict-JSON guarantee enforced rather
            # than assumed; build_report sanitizes, so this should never trigger.
            args.json_out.write_text(
                json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write JSON report to {args.json_out}: {exc}",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
