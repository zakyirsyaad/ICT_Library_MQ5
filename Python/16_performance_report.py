"""
16 - Performance Report / Trade Journal Analyzer
================================================
Reads a trade journal (JSONL, one closed trade per line) written by the MQL5
`CSmcTradeJournal` utility and reports expectancy, cost, realized risk, and
exit quality.

Detection and journaling stay in MQL5; this script only consumes their output,
following the same division as the six readers under `examples/`.

Shape contract: `schemas/trade-journal.schema.json`. Validation here is explicit
rather than a per-row jsonschema call so large journals stay fast; the test
`test_trade_fields_match_schema_required` keeps the two field lists from drifting.

Timestamps are broker-local wall-clock values with no UTC suffix or offset, so
they are never converted or reinterpreted as UTC instants.
"""
from __future__ import annotations

import json
import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

JOURNAL_VERSION = "1.0"
MIN_TRADES_FOR_VERDICT = 100

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
    """A non-fatal problem with one journal line.

    Empty string, None, 0 and an absent field all mean different things in this
    contract, so a warning is never a substitute for a rejected row.
    """

    line_number: int
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


def _validate_invariants(row: dict, parsed: dict, problems: list[str]) -> None:
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
    _validate_invariants(row, parsed, problems)
    if problems:
        return None, warnings + [JournalWarning(line_number, "rejected_row", "; ".join(problems))]

    values = {name: row[name] for name in TRADE_FIELDS if name not in _TIME_FIELDS}
    values.update(parsed)
    return Trade(**values), warnings


def load_journal(path: Path) -> tuple[list[Trade], list[JournalWarning]]:
    """Read a JSONL trade journal into trades plus non-fatal warnings.

    Rows are returned in file order; sorting is the metrics layer's concern.
    A malformed line yields a warning and is skipped, never an exception, so a
    partial write from an interrupted terminal cannot lose the rest of the run.
    """
    raw = Path(path).read_bytes()
    if raw[:2] in _UTF16_BOMS:
        raise JournalFormatError(
            f"{path}: file is UTF-16 encoded (BOM detected). MT5 writes UTF-16 when a "
            "handle is opened with FILE_UNICODE; reopen the journal without FILE_UNICODE "
            "so it is written as UTF-8."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalFormatError(
            f"{path}: not valid UTF-8 ({exc}). If the journal came from MT5, check whether "
            "it was written with FILE_UNICODE, which produces UTF-16."
        ) from exc

    trades: list[Trade] = []
    warnings: list[JournalWarning] = []
    first_seen: dict[int, int] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.append(JournalWarning(
                line_number, "truncated_line",
                f"discarded incomplete JSON line ({exc.msg} at column {exc.colno}); "
                "an interrupted write leaves a partial trailing line",
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
                f"position_id {trade.position_id} was already recorded on line {earlier}; "
                "both rows are kept, so trade counts may be inflated",
            ))
        trades.append(trade)
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
      it would double-count; spread is reported separately in pips.
    - `max_drawdown_abs` and `max_drawdown_pct` are independent maxima over the
      equity curve, so they may occur at different points.
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
            drawdown_abs = max(drawdown_abs, gap)
            if peak > 0:
                drawdown_pct = max(drawdown_pct, gap / peak * 100)

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
        "cost_pct_of_gross": cost / total_gross * 100 if total_gross > 0 else 0.0,
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
    reached at least the target distance and which closed positive. A low count
    relative to trade_count suggests targets are placed beyond where price
    actually travels.

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
            and trade.net_profit > 0
            and trade.mfe >= abs(trade.tp - trade.entry_price)
        ),
        "sl_exceeded_before_favourable_count": sum(
            1 for trade in trades
            if trade.sl > 0 and trade.mae <= -abs(trade.entry_price - trade.sl)
        ),
        "median_mfe": statistics.median(mfe_values) if mfe_values else 0.0,
        "median_mae": statistics.median(mae_values) if mae_values else 0.0,
    }
