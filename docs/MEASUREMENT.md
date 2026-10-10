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
- **`intended_risk_pct` is what the EA asked for; `realized_risk_pct` is what happened**,
  as a percentage of `balance_at_open`. The gap between them is the point of the field.
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

Exit codes are `0` on success, `1` when the journal is missing or unreadable, and `2`
for invalid arguments. A `--variant` value that matches no trade exits `0` and reports
zero trades: an empty result is a finding, not a failure.

The text report goes to standard output. `--json-out` writes the same content as JSON
with six top-level keys — `metrics`, `filters`, `risk_verdict`, `exit_quality`,
`groups`, `warnings` — and `filters` records the parameters used, so a saved report
stays interpretable on its own. Non-finite ratios are written as the string `"inf"`
rather than the bare `Infinity` literal, which is not valid JSON.

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
| `truncated_line` | A line was incomplete JSON and was discarded. Expected after an interrupted write. |
| `unknown_field` | A record carried fields this version does not define. They are ignored so an older analyzer can read a newer journal. |
| `duplicate_position` | A `position_id` appeared more than once. Both rows are kept, so counts may be inflated. |
| `rejected_row` | A record failed a required-field, type, range, or ordering check and was excluded. |

Warnings never abort a run, and they are always listed in the report. A journal whose
warnings were silently dropped would look clean while reporting on a subset of its own
data.

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

## Related

- [Data contract](DATA_CONTRACT.md) for the snapshot JSON that carries detection results.
- [Development](DEVELOPMENT.md) for local validation and the pull request workflow.
