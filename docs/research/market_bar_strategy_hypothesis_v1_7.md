# Market-Bar Strategy Hypothesis Design V1.7

Status: design-only contract. This document defines hypotheses and proof contracts; it does not generate signals, run a backtest, calculate future returns or PnL, or make network requests.

## 1. Purpose and scope

V1.6 established a reusable Market-Bar structure. V1.7 freezes the first strategy hypotheses in that coordinate system so that the next phase can test causality before measuring performance.

The research question is:

> In a compressed Market-Bar state, are raw MMA20 crosses mostly noise, and does waiting for compression release plus an upper-cluster breakout produce a more selective event than the raw cross?

The strategy family is `MARKET_BAR_COMPRESSION_RELEASE`.

The three pre-registered variants are:

1. `C0_RAW_MMA20` — control using the unfiltered MMA20 cross.
2. `C1_FILTER_COMPRESSED` — control cross with compressed-state BUYs suppressed.
3. `H1_COMPRESSION_RELEASE` — compression-release and cluster-breakout hypothesis.

No additional variant, threshold, stock-specific parameter, optimizer, or grid search is permitted in this design.

## 2. V1.6 evidence freeze

### Supported / strongest evidence

Market-Bar MMA20 cross frequency changes monotonically with compression in the three-stock development set:

| Stock | C1 | C2 | C3 | C4 |
|---|---:|---:|---:|---:|
| 066570 | 22.2 | 9.5 | 1.9 | 0.0 |
| 035420 | 27.5 | 20.51 | 12.5 | 0.0 |
| 105560 | 34.21 | 21.43 | 15.0 | 0.0 |

These are descriptive events per 100 Market Bars, not strategy results. They support testing whether compressed-state MMA20 crosses are noisy; they do not establish that expansion or breakout is profitable.

### Partial evidence

- MMA10 compression relation.
- nearest-MMA role normalization.
- MMA turn propagation.

### Rejected as primary strategy evidence

The following V1.6 observations are not primary signal features in V1.7:

- wick reclaim/rejection;
- nearest-MMA adaptive entry;
- turn-propagation timing;
- Market-Bar reaction signatures as a claim of superiority over Calendar Daily data;
- source-calendar FAST/SLOW regime labels.

These findings remain descriptive artifacts only.

## 3. Frozen infrastructure and data coordinate

The following are frozen and must not be changed to improve a strategy result:

- Activity Tau definition;
- Market-Bar geometry and source-resolution policy;
- global integer Tau lattice;
- source endpoint snapping;
- OHLCV aggregation;
- MMA periods 5, 10, 20, and 60;
- Market-Bar ATR20 (`MB_ATR20`);
- `MMA_CLUSTER_WIDTH_ATR` formula;
- Market-Bar provenance and actual next-bar OPEN semantics.

Market-Bar is the strategy coordinate. Source Daily, 30-minute, and 5-minute resolutions remain provenance/resampling inputs, not alternative strategy timeframes.

## 4. Development and validation populations

The V1.6 stocks `066570`, `035420`, and `105560` form the `DEVELOPMENT_RESEARCH_SET`. They may be used to verify event construction and deterministic behavior, but their results must be labeled `DEVELOPMENT_ONLY` and must not be described as out-of-sample validation.

Unseen stocks and windows are required before any strategy candidate is accepted. `005380` is a balanced validation candidate and `068270` is a SLOW-dominant stress candidate. Their use is a later validation step; this V1.7 design does not collect or backtest them.

Validation selection is availability/source-quality based before performance is observed. A candidate cannot be exchanged because its development result is inconvenient.

## 5. Common definitions

All indicators below use completed Market Bars and Market-Bar `close` unless explicitly stated otherwise. No Calendar Daily MA, volume, flow, efficiency, TOP30 membership, or source-calendar regime is used by the strategy hypotheses.

### 5.1 Raw MMA20 cross

For a valid MMA20:

```text
UP_CROSS[T]   = close[T-1] <= MMA20[T-1] and close[T] > MMA20[T]
DOWN_CROSS[T] = close[T-1] >= MMA20[T-1] and close[T] < MMA20[T]
```

The event is known only after Market Bar `T` is complete.

### 5.2 Causal compression width

```text
MMA_CLUSTER_WIDTH_ATR[T] =
    (max(MMA5[T], MMA10[T], MMA20[T])
     - min(MMA5[T], MMA10[T], MMA20[T])) / MB_ATR20[T]
```

MMA60 is excluded from the cluster width. All four MMA values and `MB_ATR20` must be valid.

`REFERENCE_WIDTHS[T]` is the valid width sequence from the previous 60 completed Market Bars, `T-60 ... T-1`. The current bar is never included. If 60 valid historical widths are unavailable, the state is `COMPRESSION_STATE_UNAVAILABLE`; no compression-conditioned event may be created.

```text
PAST_Q25[T] = 25th percentile(REFERENCE_WIDTHS[T])
COMPRESSED[T] = WIDTH[T] <= PAST_Q25[T]
```

The percentile is computed only from the past reference sequence, per stock and Market-Bar stream, with a deterministic quantile implementation recorded in event metadata. It is not the V1.6 full-sample C1 quartile.

### 5.3 Recent compression

```text
RECENT_COMPRESSED[T] =
    any(COMPRESSED[T-5], ..., COMPRESSED[T-1])
```

Only the previous five completed bars are inspected. `T` is excluded from this history condition. At least one valid compressed bar is required; unavailable values do not become compressed by inference.

### 5.4 Release

```text
COMPRESSION_RELEASE[T] =
    WIDTH[T] > PAST_Q25[T]
    and WIDTH[T] > WIDTH[T-1]
```

The reference and `WIDTH[T-1]` must be available. This defines expansion out of a past compression zone and does not imply a profitable breakout.

### 5.5 Upper-cluster breakout

```text
UPPER_CLUSTER[T] = max(MMA5[T], MMA10[T], MMA20[T])

CLUSTER_BREAKOUT[T] =
    close[T-1] <= UPPER_CLUSTER[T-1]
    and close[T] > UPPER_CLUSTER[T]
```

Both current and previous cluster values must be valid. This is a close-based breakout, not an intrabar high test.

### 5.6 MMA20 direction

V1 uses only the non-declining condition:

```text
MMA20_NON_DECLINING[T] = MMA20[T] >= MMA20[T-1]
```

No percentage slope, ATR-normalized slope, or hard slope threshold is allowed in V1.7.

## 6. Variant contracts

### 6.1 C0 — `RAW_MMA20`

At the completed close of Market Bar `T`:

```text
BUY_SIGNAL[T]  = UP_CROSS[T]
SELL_SIGNAL[T] = DOWN_CROSS[T]
```

Compression is not read by C0. C0 is the control for separating the effect of compression filtering from the general Market-Bar coordinate.

### 6.2 C1 — `FILTER_COMPRESSED`

BUY uses the same up-cross as C0, but a compressed-state cross is suppressed:

```text
BUY_SIGNAL[T] = UP_CROSS[T] and not COMPRESSED[T]
SELL_SIGNAL[T] = DOWN_CROSS[T]
```

SELL is intentionally unchanged. This isolates the proposed removal of compressed-state BUY events.

### 6.3 H1 — `COMPRESSION_RELEASE_CLUSTER_BREAKOUT`

The first hypothesis BUY event is:

```text
BUY_SIGNAL[T] =
    RECENT_COMPRESSED[T]
    and COMPRESSION_RELEASE[T]
    and CLUSTER_BREAKOUT[T]
    and MMA20_NON_DECLINING[T]
```

All clauses are evaluated using information available at the completed close of `T`. No same-bar open or future observation is allowed.

The hypothesis does not add a stop, profit target, trailing exit, holding limit, or calendar-time exit.

## 7. Execution and position contract

- Long-only.
- One stock and one position at a time in the first proof.
- No pyramiding, averaging down, or overlapping entries.
- A BUY signal generated at `T` is eligible only for the actual OPEN of Market Bar `T+1`.
- Same-bar close or same-bar open fills are prohibited.
- A fill requires an actual next-bar OPEN with source provenance; synthetic execution prices are prohibited.
- If no next Market Bar exists, no fill is fabricated. The event is recorded as unavailable for later policy handling.
- Calendar midnight or calendar-day boundaries do not change the execution rule: the next Market-Bar OPEN is authoritative.
- Signal and execution calendar datetimes are retained as provenance metadata.

While holding, later BUY signals are recorded as `IGNORED_WHILE_HOLDING`. When a SELL condition occurs while holding, SELL has priority. BUY is permitted only while flat. Same-bar entry and exit are not combined into one position transition.

## 8. Exit contract

The common exit hypothesis for all three variants is the completed-bar MMA20 down-cross:

```text
SELL_SIGNAL[T] = close[T-1] >= MMA20[T-1]
                 and close[T] < MMA20[T]
```

Execution is the actual OPEN of Market Bar `T+1` under the same provenance and no-synthetic-price rule as entries.

The initial proof must not add stop loss, take profit, trailing stop, MMA5/MMA10 exits, maximum holding duration, or calendar-time exits.

## 9. Signal priority and event audit

The causal event order is:

```text
bar T completes
→ signal evaluated
→ order becomes eligible for T+1
→ T+1 actual OPEN fill
```

For each variant, the first proof must record, without using future outcomes:

- BUY and SELL signal counts;
- completed-trade-possible count;
- `IGNORED_WHILE_HOLDING` count;
- signal-to-execution Market-Bar lag;
- signal and execution calendar datetimes;
- compression width and past Q25;
- upper-cluster value;
- MMA20 direction;
- missing next-bar/open availability;
- deterministic event identifiers and source-bar identity.

No future return label is present in the causal event ledger.

## 10. Anti-lookahead invariants

An implementation is rejected if any of the following occurs:

1. A full-sample or future-derived quartile is used for a signal.
2. `WIDTH[T]` is included in `PAST_Q25[T]`.
3. Fewer than 60 valid historical widths are silently treated as a valid state.
4. `RECENT_COMPRESSED[T]` reads `T` or any future bar.
5. A cluster or cross uses an unfinished bar.
6. A signal fills on `T` rather than the actual `T+1` OPEN.
7. A synthetic or adjusted price is substituted for the execution OPEN.
8. Calendar FAST/SLOW, nearest-MMA, wick, turn-propagation, flow, efficiency, or future-return labels leak into signal construction.
9. Result changes when future bars are removed after the evaluated signal time.
10. Input row order changes the canonical event/ledger order.

The next proof must include truncation/property tests showing that removing future bars does not alter earlier signal fields, and fixture tests showing that a signal at `T` cannot fill at `T`.

## 11. Frozen parameters

The following values are fixed for the initial proof:

| Parameter | Value |
|---|---|
| MMA periods | 5 / 10 / 20 / 60 |
| Compression reference | Previous 60 completed Market Bars |
| Compression percentile | Past Q25 |
| Recent compression history | Previous 5 completed Market Bars |
| Cluster | MMA5 / MMA10 / MMA20 |
| Direction filter | MMA20 non-declining |
| Exit | MMA20 down-cross |
| Execution | Next Market-Bar actual OPEN |

These are registered design parameters, not optimization candidates.

## 12. Included and excluded features

### Allowed in the first hypothesis proof

- Market-Bar OHLC;
- MMA5, MMA10, MMA20;
- `MMA_CLUSTER_WIDTH_ATR`;
- past-only Q25;
- previous-five compression state;
- one-bar MMA20 direction;
- Market-Bar sequence/index;
- actual execution provenance.

### Excluded

- MMA60 as an entry filter;
- nearest-MMA role;
- wick reclaim/rejection;
- source-calendar FAST/SLOW;
- turn propagation;
- Calendar Daily MA;
- volume, flow, efficiency;
- TOP30 membership;
- stock-specific thresholds;
- future returns or PnL in signal generation;
- corporate-action inference in the strategy layer.

MMA60 remains available as warm-up/context data but is not an entry feature.

## 13. Evaluation order

Evaluation is frozen in this order:

### Step A — causal signal-event audit

Verify event counts, chronology, availability, bar identity, next-open eligibility, and anti-lookahead invariants. No PnL or future-return scoring.

### Step B — deterministic zero-cost proof

Run C0, C1, and H1 on the development set with the same data and execution contract. Report event and ledger behavior before performance interpretation.

### Step C — unseen validation

Run the pre-registered validation stocks/windows selected by availability and source quality. Development results must not select or exclude validation cases.

### Step D — cost sensitivity

Only after Steps A–C pass may configured costs, taxes, and slippage be evaluated. Cost assumptions are not part of V1.7 design execution.

## 14. Future performance metrics

These are reserved for the later backtest phase, not V1.7:

- trade count and win rate;
- average win/loss and payoff;
- profit factor;
- mean/median trade return;
- total return and MDD;
- average holding Market Bars;
- exposure and turnover.

No metric may be used to tune the frozen parameters.

## 15. Hypotheses and decision labels

- **H1:** suppressing compressed-state raw MMA20 up-crosses improves signal quality versus C0. `TO_BE_TESTED`.
- **H2:** recent compression followed by release and upper-cluster breakout is more selective/directional than raw MMA20 crossing. `TO_BE_TESTED`.
- **H3:** the common MMA20 down-cross exit is compatible with all three entry variants. `TO_BE_TESTED`.
- **H4:** the strategy family can be defined in Market-Bar coordinates without source-calendar FAST/SLOW features. `TO_BE_TESTED`.

V1.6 descriptive support does not pre-label any hypothesis as supported. Labels are assigned only after the pre-registered causal and validation sequence.

## 16. Design rejection criteria

Before any backtest interpretation, reject the design/run if it uses:

- future data in signal calculation;
- full-sample quartiles;
- fewer than 60 past valid widths without an explicit unavailable state;
- no actual `T+1` OPEN;
- synthetic execution price;
- same-bar fill;
- source-calendar regime metadata as a hidden signal feature;
- changed infrastructure or stock-specific parameters to improve a result.

## 17. Runtime boundary and next phase

V1.7 does not implement a strategy engine, signal generator, order executor, backtest runner, or performance report. It only freezes the contracts required to build the next proof.

The next phase is V1.8, `MARKET_BAR_COMPRESSION_STRATEGY_CAUSAL_SIGNAL_EVENT_PROOF`. V1.8 first constructs and audits C0/C1/H1 signal events and next-open eligibility. PnL remains prohibited until causal correctness is demonstrated.

## 18. Provenance and reproducibility

Every future event record must retain the Market-Bar stream identity, source artifact identity, source bar sequence, signal bar identity, execution bar identity, and the exact causal compression inputs used. Network credentials, tokens, and account information must never enter research artifacts.

This document is the V1.7 design contract. Runtime code, generated signal events, backtests, and PnL are intentionally absent.
