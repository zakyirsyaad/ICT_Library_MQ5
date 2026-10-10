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
