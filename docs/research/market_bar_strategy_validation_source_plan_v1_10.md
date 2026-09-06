# Market-Bar strategy-unseen validation source plan V1.10

This is an offline source-readiness checkpoint. It does not calculate a
strategy signal, inspect a strategy output, calculate a return/PnL, or call a
network API. It uses the frozen V1.8 validation registry and V1.9A decision
gate, with `005380` first and `068270` second.

## Frozen roles and selection

| Stock | Frozen role | Preferred window | Target | Expected Market Bars | New FAST fetch dates |
|---|---|---:|---:|---:|---:|
| `005380` | `BALANCED_STRATEGY_VALIDATION_CANDIDATE` | 2025-06-12 .. 2026-01-20 | 200 | 200 | 24 |
| `068270` | `SLOW_DOMINANT_STRESS_VALIDATION_CANDIDATE` | 2024-12-27 .. 2025-10-31 | 200 | 201 | 48 |

`005380` uses the best deterministic balanced 200-bar planning candidate
(non-zero pure FAST and pure SLOW capacity). `068270` uses the deterministic
best 200-bar source-regime candidate for its frozen SLOW-dominant stress role;
it is not replaced with a balanced candidate.

The report distinguishes the general V1.10 source-readiness candidate from
the role-specific frozen preferred candidate. The general 200-bar candidates
are:

| Stock | General V1.10 candidate | Expected capacity | New FAST fetch |
|---|---|---:|---:|
| `005380` | 2026-01-06 .. 2026-05-08 | 200 | 0 |
| `068270` | 2026-01-02 .. 2026-06-12 | 200 | 0 |

These general candidates do not satisfy the frozen validation roles: the
005380 candidate is not the balanced role window, and the 068270 candidate is
not the SLOW-dominant stress window. They are retained as source-only ranking
evidence and are not used to replace the pre-registered windows above.

General candidate ordering is source-only:

1. structural gap count (zero first);
2. sufficient expected capacity;
3. missing required FAST sessions (fewest first);
4. shorter calendar span;
5. higher expected capacity;
6. fewer source-quality anomalies;
7. earlier start and stock/date/target deterministic tie-breaks.

The role-specific selection above uses the frozen role sub-ranking (balanced
for 005380, SLOW-dominant for 068270), not strategy performance. This resolves
the apparent 005380 `61 missing` versus `24 missing` discrepancy: the former
was the older V1.4 regime-ranked reference, while the latter is the frozen
balanced role candidate. The report labels both explicitly.

## Local source evidence

The local Daily cache contains two raw pages and two adjusted pages per stock,
1,200 rows in each mode, with matching date sets and no duplicate or malformed
dates. The available date range is 2021-10-06 through 2026-08-31; the research
window contains 726 sessions per stock.

The local raw minute cache contains 21 pages per stock. It covers 242 dates
from 2025-09-01 through 2026-08-28, with 18,819 rows for `005380` and 18,818
rows for `068270`. Timestamp parsing found no malformed or duplicate
timestamps and no missing required source fields. The cache index is derived
from actual `cntr_tm` values; it is not inferred from a date range.

No price sign is normalized in this step. The inventory preserves raw-artifact
provenance through deterministic SHA-256 digests and only records timestamp,
date, row-count, and field-presence quality metadata.

## Exact planned acquisition list

The complete machine-readable lists are in:

`data/processed/strategy_review/market_bar_strategy_validation_source_readiness_v1_10.json`

They contain 24 dates for `005380` (oldest `2025-06-16`, newest `2025-08-29`)
and 48 dates for `068270` (oldest `2024-12-27`, newest `2025-08-29`). Each row
is marked `MARKET_BAR_RESOLUTION_REQUIRED`; no credentials or API parameters
are written.

## Structural and source-quality gate

Both preferred candidates have structural gap count `0`. Local Daily raw,
Daily adjusted, and minute raw source-quality anomaly lists are empty. This is
only a readiness result: the missing dates still require an explicitly
authorized live acquisition proof, and endpoint retention is
`LIVE_VALIDATION_REQUIRED`.

The next materialization stage must keep the following invariants:

- one continuous Market-Bar island;
- actual bars at least the selected target;
- no unresolved internal gap, skip, or duplicate;
- source-exact OHLC and volume;
- no fractional split, interpolation, or synthetic bar.

The phases are: (A) existing cache only, (B) exact missing-date acquisition,
(C) coverage/provenance verification, (D) frozen-contract Market-Bar
materialization, and (E) continuity invariant verification. Only phase A and
the offline B fetch list were performed here.

## Scope guard

The generated report records `network_calls = 0`,
`strategy_outputs_inspected = false`, and no strategy signals, future returns,
trades, PnL, or parameter changes. Market-Bar geometry, global tau, source
resolution, and compression rules remain frozen. The next step is V1.11
source acquisition/materialization; strategy-unseen validation is deferred to
V1.12 after that checkpoint.
