# Trade journal measurement

[Documentation](README.md) · [Data contract](DATA_CONTRACT.md) · [Detection rules](ICT_RULES.md)

This page describes how to record closed trades and measure what a configuration
actually did. It documents measurement tooling only. Nothing here evaluates a
strategy, and no number in this repository establishes that any configuration is
profitable — the same position the [project README](../README.md) takes for pattern
detections.

The authoritative shape of a journal record is
[trade-journal.schema.json](../schemas/trade-journal.schema.json). The analyzer is
[Python/16_performance_report.py](../Python/16_performance_report.py).

## Division of responsibility

Recording stays in MQL5 and analysis stays in Python, mirroring the split used by the
[snapshot data contract](DATA_CONTRACT.md): the terminal knows the prices, spreads,
commissions, and margin it actually applied, and only the terminal can report them
faithfully. The analyzer never re-derives a trade outcome and never reimplements
detection. It reads a file that conforms to the schema and reports on it.

The writer is `CSmcTradeJournal` in
[Include/SMC/Utils/TradeJournal.mqh](../Include/SMC/Utils/TradeJournal.mqh), a
static-method-only utility in the same family as `CSmcDataExporter` and
`CSmcSnapshotExporter`. It writes under `FILE_COMMON`, so journals land in the terminal's
`Common/Files` directory and are reachable from Python without copying.
`CSmcTradeJournal::JournalFilename(magic)` builds the conventional name for one run —
`trade-journal-<magic>-<chart>-<yyyymmdd-hhmmss>.jsonl` — and should be called once per
run, not per append.

Two of its helpers exist because the library's own human-readable names are **not** the
contract's tokens. `CSmcKillZone::GetSessionName` returns `"New York"`,
`"LDN-NY Overlap"` and `"None"`, three of the five values the schema rejects, and
`SmcDirectionName` returns `"neutral"` where the schema wants `"ranging"`. Fill the
`session` and `htf_trend` fields through `CSmcTradeJournal::SessionToken` and
`CSmcTradeJournal::TrendToken` instead. Writing a rejected value does not raise: the row
simply disappears into a `rejected_row` warning and leaves every metric.

## Journal format

A journal is UTF-8 JSON Lines: one closed trade per line, appended as positions close.
Append-only JSON Lines is deliberate. An interrupted write damages at most the final
line, and the analyzer discards that line with a warning instead of failing on the
whole run.

Every field below is required. As in the snapshot contract, a producer writes all of
them even when a value equals its default, and the schema's `default` annotations are
documentation — a consumer must not use them to repair an incomplete record.

| Group | Fields |
| --- | --- |
| Version | `journal_version` |
| Identity | `ticket`, `position_id`, `symbol`, `timeframe`, `magic`, `variant`, `config_hash` |
| Time and direction | `open_time`, `close_time`, `direction`, `bars_held` |
| Execution | `lot`, `entry_price`, `sl`, `tp`, `exit_price`, `exit_reason`, `spread_at_entry`, `slippage_points` |
| Profit and loss | `gross_profit`, `commission`, `swap`, `net_profit` |
| Account | `balance_at_open`, `equity_at_open`, `margin_used`, `intended_risk_pct`, `realized_risk_pct` |
| Excursion | `mfe`, `mae` |
| Strategy context | `htf_trend`, `zone_percent`, `in_ote`, `zone_types`, `trigger`, `session`, `confluence_score`, `confluence_count` |

Semantics that are easy to get wrong:

- **Timestamps are broker-local wall clock**, formatted `YYYY-MM-DDTHH:MM:SS` with no
  `Z` and no offset. They are not UTC instants, and the analyzer never converts them.
  A broker's clock rules may change with daylight saving time.
- **`sl` and `tp` of `0` mean "not set"**, not a level at distance zero. Trades with no
  level are excluded from the excursion counts rather than treated as instantly reached.
- **`mfe` is non-negative and `mae` is non-positive**, both in price units measured from
  `entry_price`. The sign convention is part of the contract.
- **Four fields are nullable**: `zone_percent`, `in_ote`, `mfe`, and `mae`. `null` means
  the producer could not compute the value; `0` and `false` are measurements. A producer
  that does not evaluate premium/discount or the OTE zone must write `null`, because
  `zone_percent: 0` asserts "price sat at the swing low" and `in_ote: false` asserts
  "evaluated and outside the zone". The analyzer excludes null excursions from medians
  and from the target-reached and stop-breached counts rather than reading them as zero.
- **`intended_risk_pct` is what the EA asked for; `realized_risk_pct` is what happened**,
  as a percentage of `balance_at_open`. The gap between them is the point of the field.
  A winning or break-even trade records `realized_risk_pct` as `0.0`: no capital was
  lost, and a positive figure would not describe risk.
- **`variant` labels which strategy layers were active** and `config_hash` identifies the
  parameter set. Without them, two runs cannot be compared and a saved report cannot be
  traced back to its inputs.
- Empty string, `null`, `0`, and an absent field all mean different things and are not
  interchangeable. Where a value is genuinely unknown, write `"unknown"` — the enums for
  `exit_reason`, `htf_trend`, `trigger`, and `session` include it.

## Running the analyzer

```sh
python Python/16_performance_report.py --journal path/to/trade-journal.jsonl
```

| Option | Effect |
| --- | --- |
| `--journal PATH` | Required. The journal to read. |
| `--variant NAME` | Analyse only trades carrying this ablation label. |
| `--json-out PATH` | Also write the report as strict JSON. |
| `--max-risk-pct FLOAT` | Realized-risk threshold for the verdict. Default `2.0`. |

Exit codes are `0` on success, `1` when the journal is missing, unreadable, or yielded
no usable row, and `2` for invalid arguments. A `--variant` value that matches no trade
exits `0` and reports zero trades: an empty result is a finding, not a failure.

The text report goes to standard output. `--json-out` writes the same report as JSON
with six top-level keys — `metrics`, `filters`, `risk_verdict`, `exit_quality`,
`groups`, `warnings` — and `filters` records the parameters used, so a saved report
stays interpretable on its own. Non-finite ratios are written as the string `"inf"`
rather than the bare `Infinity` literal, which is not valid JSON.

**Units in the JSON payload are mixed and the key names do not all say so.** Read this
before consuming the file:

- `win_rate` is a **fraction between 0 and 1**. Multiply by 100 to display it.
- Every key ending in `_pct`, plus `max_risk_pct`, `realized_min`, `realized_median`,
  `realized_max` and `cost_pct_of_gross`, is **already 0 to 100**. Do not scale again.
- `total_spread_pips` is in pips, not currency. Every other `total_*` key is money in the
  account currency.
- `mfe` is non-negative and `mae` is non-positive, both in price units.
- A `null` means the value is undefined rather than zero — `cost_pct_of_gross` is `null`
  when gross profit is not positive, where `0.0` would falsely claim costs consumed
  nothing.

Guessing wrong on `win_rate` reports a 28.57% win rate as either 0.29% or 2857%, and
nothing downstream will object.

## Reading the report

**Data quality comes first, and it should be read first.** The section states the trade
count, whether the sample is sufficient, and every journal warning. A metric read
without that context is not a conclusion.

| Section | What it reports |
| --- | --- |
| `DATA QUALITY` | Trade count, sufficiency, and warnings |
| `CORE` | Expectancy per trade, win rate, profit factor, payoff ratio, average and extreme outcomes, longest losing streak, maximum drawdown, total net |
| `COST` | Gross result, commission, swap, cost as a share of gross, and spread paid in pips |
| `RISK REALIZATION` | Distribution of realized risk across losing trades and the verdict |
| `EXIT QUALITY` | Exit-reason distribution, how often the target distance was reached, how often the stop distance was breached, and median excursions |
| `BY <key>` | The same core metrics grouped by `variant`, `session`, `htf_trend`, `trigger`, `hour`, and `weekday` |
| `FILTERS` | The parameters that produced this report |

Three definitions matter when comparing numbers:

- **A win is `net_profit > 0` and a loss is `net_profit < 0`.** A break-even trade is
  neither. It does not count toward the win rate, and it ends a losing streak.
- **`cost_pct_of_gross` counts commission and swap only.** Spread is already inside the
  entry and exit prices that produced `gross_profit`, so including it would count the
  same cost twice. Spread is reported separately, in pips.
- **Streaks and the equity curve follow `close_time` order**, not the order lines appear
  in the file, because that is the order capital actually moved in.

Grouping by `variant` is how a layer's contribution is isolated: run the same journal
through the analyzer once per variant and compare. A group's `n` is its `trade_count`,
so a group holding a handful of trades is visibly weaker evidence than one holding
hundreds.

## Sample size and `insufficient_data`

`MIN_TRADES_FOR_VERDICT` is 100. Below it, `sufficient_data` is `false` and the report
says so in its first section. **Metrics computed from fewer trades are not evidence.**
A win rate from 7 trades has a confidence interval wide enough to include both a strong
edge and a strong disadvantage, and expectancy from a small sample is dominated by
whichever few trades happened to land in it.

The risk-realization verdict is the one part of the report that deliberately does not
wait for that threshold. It answers a structural question rather than a statistical one:
whether the broker's minimum lot step lets a configuration achieve the risk per trade it
asked for. A single trade that lost far more than its intended percentage already answers
that, and waiting for 100 trades would only delay a fact that is arithmetic rather than
sampling.

The verdict has three values:

| Verdict | Meaning |
| --- | --- |
| `risk_contained` | No losing trade exceeded `--max-risk-pct` |
| `risk_exceeded` | At least one losing trade exceeded it |
| `insufficient_data` | No losing trade was observed, so realized risk is unmeasured |

`insufficient_data` here means *no losers were seen*, not *too few trades*. A journal of
only winning trades says nothing about how large a loss the configuration can produce,
which is the only thing this verdict measures.

## Warnings

| Code | Meaning |
| --- | --- |
| `truncated_line` | A line was not valid JSON and was discarded. Only a partial **final** line is attributed to an interrupted write; a malformed line elsewhere in the file points at a corrupt producer or a bad edit. |
| `unknown_field` | A record carried fields this version does not define. They are ignored so an older analyzer can read a newer journal. |
| `duplicate_position` | A `position_id` appeared more than once. Both rows are kept, so counts may be inflated. |
| `rejected_row` | A record failed a required-field, type, range, or ordering check and was excluded. |
| `inconsistent_pnl` | `net_profit` did not equal `gross_profit + commission + swap` within one cent. The row is kept, but the `CORE` and `COST` sections would otherwise contradict each other silently. |
| `balance_divergence` | The recorded `balance_at_open` values do not follow from each other. Reported once per journal, not per row. |

Warnings never abort a run, and the report always states how many there were. The
**rendered** list is capped at 20 with a per-code rollup for the remainder, because a
large journal whose rows all carry one unrecognized field would otherwise push every
metric megabytes below the section that is supposed to be read first. The `--json-out`
payload deliberately keeps **every** warning: it is read by a program, and truncating it
would discard the data a consumer needs.

If a journal has content but **no** usable row, that is an error rather than a result:
the analyzer exits `1` and names the reason. A zero-trade report that exits successfully
would let a pipeline record a pass on data it never read — most often a
`journal_version` mismatch against a newer producer. An empty file, by contrast, exits
`0` and reports zero trades.

A leading UTF-8 byte-order mark is stripped rather than treated as content. Without
that, a journal re-saved by a Windows editor or by PowerShell's `Out-File -Encoding utf8`
loses its first trade and reports the loss as a malformed line.

### The drawdown assumption

`max_drawdown_abs` and `max_drawdown_pct` describe one event: the percentage is taken at
the peak that preceded the largest absolute gap, not tracked separately. The equity curve
they are computed from is reconstructed from the first trade's `balance_at_open` plus
each net result, which is exact for **one strategy on one account with no cash
movements**. A deposit, a withdrawal, or a second strategy sharing the account breaks
that reconstruction, and the analyzer reports `balance_divergence` when the recorded
balances say it happened. Read the drawdown figures only when that warning is absent.

A journal that cannot be read as UTF-8 at all is an error rather than a warning. MT5
writes UTF-16 when a file handle is opened with `FILE_UNICODE`; the analyzer detects the
byte-order mark and names that cause instead of raising a bare decoding error.

## Contract fixture

[tests/fixtures/trade-journal-v1.jsonl](../tests/fixtures/trade-journal-v1.jsonl) and its
[expected output](../tests/fixtures/trade-journal-v1.expected.txt) pin the format and the
analyzer's rendering. Each line exercises one contract case, including an unrecognized
field and a truncated final line. Following the convention of
[snapshot-v1.json](../tests/fixtures/snapshot-v1.json), it is a contract fixture: its
identifiers are symbolic and its values are chosen to exercise the format, not to report
observed market results.

[tests/fixtures/trade-journal-v1-nulls.jsonl](../tests/fixtures/trade-journal-v1-nulls.jsonl)
is a second, separate fixture covering the nullable fields and the metrics that must skip
them. It is kept apart deliberately: the first fixture's every aggregate value is verified
by hand, and adding rows to it would silently change all of them.

## Related

- [Data contract](DATA_CONTRACT.md) for the snapshot JSON that carries detection results.
- [Development](DEVELOPMENT.md) for local validation and the pull request workflow.
