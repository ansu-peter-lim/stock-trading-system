# Algorithm Research Checkpoint Before Daily MA Unification

## 1. Executive summary

This is a documentation-only checkpoint.  It preserves prior Daily, Market-Time,
Market-Bar, MMA, and compression-strategy research as reusable evidence; it does
not declare any of that work obsolete, failed, or replaced.

The research sequence is being realigned to establish a single **Daily MA Unified
Swing Strategy** principle first.  Only after the Daily principle, its visual
interpretation, causal event audit, and validation are established should the
same trading philosophy be projected into Market-Bar/MMA space for a fair timing
comparison.

This document adds no strategy rule, signal, backtest, PnL calculation, network
request, or source/test-code change.

## 2. Why the research sequence changed

The pause is a **RESEARCH_SEQUENCE_REALIGNMENT**, not a technical failure of the
Market-Bar geometry or a rejection of the Market-Bar research family.  The next
primary question is how Daily price relates to Daily MA structure across rising,
box/reversal, and falling regimes.  That Daily swing principle must be specified
before comparing Daily and Market-Bar timing implementations.

Prior research is therefore `PAUSED_BUT_PRESERVED_RESEARCH`.  It remains
reference evidence and may be reused in later design comparisons without
silently changing historical outputs.

## 3. Research-track inventory

| Track | Status | Frozen finding / role | Primary references |
|---|---|---|---|
| Daily UP | `REFERENCE_ONLY` | `LOW_REQUIRED` is the surviving research baseline; H1/H2 alternatives did not replace it. | `src/strategy_review/`, `src/kiwoom_minute/small_up_path_proof.py`, commits `569e774`, `c117865`, `d7fed05` |
| Daily DOWN / BOX | `SUPPORTED_RESEARCH_EVIDENCE` | DOWN_BOX_REVERSAL V0.2 long-history zero-cost proof is strong historical research evidence, not an automatic rule for the new unified strategy. | `src/backtest_engine/down_box_strategy.py`, `src/kiwoom_daily/down_box_*`, `src/strategy_review/down_box_review.py`, commits `7d932e1`, `a83cb58`, `a234800`, `6cc4d29` |
| Market clock / Market-Time | `PARTIAL_RESEARCH_EVIDENCE` | Activity-time normalization and MA-role observations are preserved as descriptive research. | `src/kiwoom_daily/market_clock_*`, `market_time_*`, commits `362c03c` through `ea096b8` |
| Market-Bar geometry | `FROZEN_INFRASTRUCTURE` | Global integer activity-tau lattice and source-exact construction contract are frozen. | `src/kiwoom_daily/*market_bar*`, commits `1bf3164` through `000b5f1` |
| Market-Bar MMA | `PARTIAL_RESEARCH_EVIDENCE` | Visual hierarchy is readable; reaction superiority over Daily is not supported; MMA20 compression relation is repeated structural evidence. | `src/kiwoom_daily/market_bar_mma_*`, commits `c42d571`, `f017256`, `9ff1ad8`, `14ae1eb` |
| Market-Bar compression strategy | `NOT_SUPPORTED_HYPOTHESIS` for C1 signal-quality; `INCONCLUSIVE` for H1 | V1.8 causal proof passes; V1.9 DEV result is mixed; C2 is pre-registered and validation-required. | V1.7--V1.9A documents and modules; commits `9085bf7`, `ef1b862`, `872e4c6`, `78c275a`, `1bc0380` |
| Strategy-unseen Market-Bar validation input | `PAUSED` | V1.11 classified both attempted sources as `RETENTION_UNAVAILABLE`; no materialization or validation was performed. | V1.10/V1.11 modules and source plan; commits `2f51699`, `38576af` |

## 4. Daily UP status

The prior UP research used Daily adjusted-price signal space with the existing
Daily/5-minute proof path.  The frozen baseline is `LOW_REQUIRED`.

The MA10-direction H1 comparison did not replace that baseline: it reduced
trades and improved some aggregate descriptive measures, but removed a large
winner and had mixed stock-level judgments.  It is retained as a rejected or
weaker alternative, not as the default strategy rule.  `LOW_REQUIRED` is
**reference evidence only** for the new Daily MA research; it is not implicitly
carried into the new BUY-A/B/C concepts.

Relevant checkpoints include:

- `569e774235cd5a9ae6711cb0b0c04f721ff0c166` — UP entry policy comparison proof.
- `c117865a3e452204ca4af0b56c2bbee67340520a` — strategy improvement audits.
- `d7fed0580e911a9615998c01cf318a879ccc44f3` — MA10 direction comparison proof.

## 5. Daily DOWN / BOX status

Prior DOWN research evolved from SMA10 reversal/breakout exploration into a
frozen-box reversal lifecycle.  DOWN_BOX_REVERSAL V0.2 preserves a Daily
adjusted signal series with raw T+1-open execution in a zero-cost long-history
proof.  Its reported result is:

- completed trades: 24; wins: 14; losses: 9; flat: 1;
- profit factor: 3.3665;
- mean trade return: +2.3941%;
- realized PnL: 5,724,900.

Those numbers are copied research evidence, not recalculated here.  They are
classified as `STRONG_HISTORICAL_RESEARCH_EVIDENCE`; they do not cause the new
unified Daily strategy to adopt its box boundaries, entry timing, lifecycle, or
exit rules unchanged.

Relevant source/test areas include `src/backtest_engine/down_box_strategy.py`,
`src/kiwoom_daily/down_box_daily_execution_proof.py`,
`src/kiwoom_daily/down_box_v03a_proof.py`, and `src/strategy_review/down_box_review.py`.
Relevant checkpoints are `7d932e19122df48182733a5a8c34bdffe8afe9d3`,
`a83cb58800ff83cbdf4add6399b4cdcaed625b14`,
`a23480074d8f334ee4eaa23d0b9cef3fcd501f73`, and
`6cc4d292f5c8a3b3ca036bd1d7483143274cd72c`.

## 6. Market clock / Market-Time status

The frozen activity-time definition is:

```text
TrueRange = max(H-L, abs(H-prevC), abs(L-prevC))
TR_PCT = TrueRange / prevC
REFERENCE_TR = past-only median of the previous 252 valid sessions
DELTA_TAU = TR_PCT / REFERENCE_TR
```

There is no clip, cap, or log transform.  The work preserves observations about
FAST expansion, SLOW compression, MA roles, reaction, compression, and turn
propagation.  The observations remain descriptive (`PARTIAL_RESEARCH_EVIDENCE`)
and are not a runtime rule in the new Daily strategy.

Relevant checkpoints: `362c03c17d298521f1f3d679841f857cdb2f4181`,
`193bbf69a2aeb30c7ab0623403d376225eb940c0`,
`938c82b5b159534252a62be026c3ed018d8a4fa0`,
`1e98cb1470c64952fd62f5b2831e524c60271219`,
`7f009944f3b94faac7f312fa281f37a1bb524c29`,
`d15047345eab466fec6de395bb47e97e4022c011`,
`8e6d88d00c5b512df150ce0454216b13bfc55cc3`,
`d680065ec147c365bd23adfc1cbba5f75704560a`, and
`ea096b81a201409303d50f7d4da75f5454c9d8f1`.

## 7. Market-Bar geometry and pilot history

Market-Bar geometry is frozen infrastructure.  Its contract is a global
activity-tau lattice with fixed integer targets `1, 2, 3, ...`.  A Market Bar
closes at the first actual source endpoint that crosses the next integer target;
one integer target maps to one Market Bar.  A source segment crossing two or
more targets yields `MARKET_BAR_RESOLUTION_INSUFFICIENT` and breaks stream
continuity.

The contract forbids fractional source splitting, OHLC interpolation, synthetic
or duplicate bars, and unresolved-gap continuity.  OHLC is an exact aggregate
of actual source segments; volume is their exact sum.  Calendar time is
provenance only, and the Market-Bar x-axis is Market-Bar index.

The design evolution is preserved:

| Version | Frozen lesson |
|---|---|
| V0.1 | Exact tau with fractional OHLC was unsafe. |
| V0.2 | Island fragmentation required explicit handling. |
| V0.3 | Local tau reset drift was invalid. |
| V0.4 | Finer source resolution could still be insufficient. |
| V0.5 | Global integer tau lattice fixed the coordinate contract. |
| V0.6 | Resolution adequacy and geometry were frozen. |
| V0.7 | Continuous-stream audit completed the infrastructure sequence. |

Pilot work then materialized a 066570 200/201-bar stream and zero-fetch
replication work for 035420 and 105560.  Key checkpoints are `1bf3164`,
`cfa54d1`, `f60564b`, `3b1f01e`, `128d50b`, `30f7ea1`, `b4d1534`,
`1bde96f`, `0246f65`, `7d1fae5`, `fe4abbb`, `8d048d8`, `3c06616`,
`9e772dd`, and `000b5f1` (full hashes are available in the commit map).

## 8. MMA structural research

MMA5, MMA10, MMA20, and MMA60 are simple moving averages of Market-Bar close.
The hierarchy was visually readable.  FAST/SLOW MA-role normalization was
partial or inconclusive, and superiority of Market-Bar reaction over Daily was
not supported.  The repeated structural finding was the relation between MMA20
cross frequency and compression.

Frozen V1.6 compression evidence (MMA20 crosses per 100 Market Bars) is:

| Stock | C1 | C2 | C3 | C4 |
|---|---:|---:|---:|---:|
| 066570 | 22.2 | 9.5 | 1.9 | 0.0 |
| 035420 | 27.5 | 20.51 | 12.5 | 0.0 |
| 105560 | 34.21 | 21.43 | 15.0 | 0.0 |

This is `STRUCTURAL_RESEARCH_EVIDENCE`, never a profitability claim.  Relevant
checkpoints: `c42d5713b45de8da1d4423ff271b4f553e53ead5`,
`f017256f11874a7569af916dfde09d1b73ce5a94`,
`9ff1ad816b14c86cee146797e296927d69db863e`, and
`14ae1eb3c4649a544f400b9790156b38f02ae82b`.

## 9. Market-Bar compression strategy and validation status

V1.7 pre-registered the `MARKET_BAR_COMPRESSION_RELEASE` family:

- C0: raw MMA20 up-cross;
- C1: filter compressed crossings;
- H1: compression release;
- width: `(max(MMA5, MMA10, MMA20) - min(MMA5, MMA10, MMA20)) / MB_ATR20`;
- past-only 60 completed Market-Bar reference and causal Q25;
- recent-compression window: previous five Market Bars;
- execution: signal T close, then T+1 actual Market-Bar open.

V1.8 causal event proof passed for 066570, 035420, and 105560.  Common windows
were MB086--201 (116 bars) for 066570 and MB086--200 (115 bars) for both 035420
and 105560.  Causality violations, same-bar fills, synthetic execution, and
future leakage were all zero.  Input-order, stock-independence, and
variant-independence regressions passed.  Completed cycles were C0=31, C1=8,
and H1=1.

V1.9 is a zero-cost DEV proof, not a general validation result.  C1's original
signal-quality hypothesis is `NOT_SUPPORTED_DEV`; selectivity/risk reduction is
`OBSERVED_DEV`; H1 is `INCONCLUSIVE_SPARSE_DEV`; the overall DEV result is
`DEV_MIXED`.  C2 (`C2_COMPRESSED_ONLY`) is preserved as a pre-registered
validation-only variant.  It was not backtested on DEV stocks and is
`DEV_GENERATED`, `VALIDATION_REQUIRED`.

The relevant documents are:

- `docs/research/market_bar_strategy_hypothesis_v1_7.md`;
- `docs/research/market_bar_strategy_validation_registry_v1_8.md`;
- `docs/research/market_bar_strategy_dev_decision_v1_9a.md`.

Key checkpoints are `9085bf7055994aaf1d868c5b007d3831deabe957`,
`ef1b862a0303e5ae0aaff91e144e01aed9105aca`,
`872e4c64d9d8c626985080492cefba724ad0645b`,
`78c275a592de2e122c5b054ac8fb9e13bfb929d2`, and
`1bc0380fe4f3e3a4210bad6d0d19aeeffbe9c419`.

## 10. Exact Market-Bar pause point

V1.10 kept the validation registry unchanged:

| Stock | Role | Frozen preferred window | Target |
|---|---|---|---:|
| 005380 | `BALANCED_STRATEGY_VALIDATION_CANDIDATE` | 2025-06-12 .. 2026-01-20 | 200 |
| 068270 | `SLOW_DOMINANT_STRESS_VALIDATION_CANDIDATE` | 2024-12-27 .. 2025-10-31 | 200 |

V1.11 is completed.  It made one live ka10080 request for each stock (two
requests total, zero retries).  HTTP/API responses had `return_code = 0`, but
each contained a row whose timestamp, OHLC, and volume fields were empty.  The
typed parser result was `MALFORMED_ROW`; the final source classification for
both stocks was `RETENTION_UNAVAILABLE`.  Per-stock stop policy prevented
additional historical requests.

No Market Bars were materialized, no strategy output was inspected, and C2 was
not evaluated.  This is not strategy-validation evidence.

**MARKET_BAR_RESUME_POINT**

- V1.11 is completed.
- Original validation windows failed because of `RETENTION_UNAVAILABLE`.
- V1.11A cache-only technical replacement-window planning is
  `NOT_STARTED` and `DEFERRED`.
- C2 is `PREREGISTERED`, not DEV-backtested, and not validation-evaluated.

This track is `PAUSED_BEFORE_VALIDATION_INPUT_REPLACEMENT`.

## 11. New primary research point: Daily MA Unified Swing Strategy

**NEW_PRIMARY_RESEARCH_POINT:** `DAILY MA UNIFIED SWING` / `BUY POINT VISUAL
PROOF`.

The objective is one Daily swing principle built from the relationship between
price and Daily MAs, rather than initially dividing UP, BOX, and DOWN into
separate independent strategies.  This is a research concept only; there is no
runtime implementation in this checkpoint.

### Core Daily MA principles

- Style: swing trading.
- Center: MA20 is the primary swing-trading reference.
- MA slopes for MA10, MA20, and MA60 use
  `(MA[T] / MA[T-10] - 1) * 100`.
- Absolute slope up to and including 3% is `FLAT`; above +3% is `UP`; below
  -3% is `DOWN`.
- MA60 is a confirmed-inflection structural reference for medium/long-term
  trend transition and support/resistance.
- MA120 is secondary structural support/resistance.
- No "minimum three-month trend" runtime condition is frozen.
- Breakout/breakdown is evaluated at Daily close: up is `Close >= MA * 1.02`,
  down is `Close <= MA * 0.98`.  Event detection must distinguish a new
  crossing from the prior-bar state.

### BUY-A — LONG_RECOVERY_10

After Close remains below MA10 for at least 10 consecutive Daily bars, require
an MA5 upward inflection and a confirmed MA10 +2% breakout.  This describes a
long MA10-below adjustment followed by the beginning of short recovery.

### BUY-B — QUICK_RECLAIM_10

After a MA10 departure, require a confirmed MA10 +2% re-breakout within ten
days.  Its intended structure is a short pullback followed by MA10 trend
reclaim.  Its exact boundary relative to BUY-A remains intentionally open.

### BUY-C — LONG_RECOVERY_20

After Close remains below MA20 for at least 15 consecutive Daily bars, require
an MA10 upward inflection and a confirmed MA20 +2% breakout.  This describes a
long MA20 departure followed by structural recovery of the MA20 swing center.

### Shared initial restriction and execution principle

The initial visual-review restriction is MA10 10-bar slope `>= -20%`, intended
only to limit extreme falling-knife candidates.  It is not an optimized
profitability threshold.  A non-deferred Daily decision is made at T close and
executes at T+1 market open; same-bar execution is prohibited.

The frozen concept `RESISTANCE_DEFERRED_BUY` says a breakout near major MA60 or
MA120 resistance, especially one rejected below resistance at close, waits for
an MA10 pullback rather than chasing at T+1 open.  This remains a concept only.

The visual-review candidate for upward MA inflection is
`MA[T] > MA[T-1] and MA[T-1] <= MA[T-2]`.  It is
`PROVISIONAL_FOR_VISUAL_REVIEW`, not a final machine contract.

## 12. Intentionally unresolved definitions

No implementation may silently fill these gaps:

1. Exact machine definition of MA inflection.
2. BUY-B's exact “within ten days” boundary and state semantics.
3. Prior-bar state definition for a MA breakout event.
4. Exact near-resistance tolerance.
5. Exact resistance-rejection definition.
6. `DEFERRED_TO_MA10` touch and entry definition.
7. Deferred-waiting expiry.
8. Exact MA60 trend-continuation/reversal machine contract.
9. SELL rules.
10. Position sizing and tactical allocation.

## 13. Required research order

1. Freeze the Daily MA principle.
2. Define the Daily BUY-point machine contract.
3. Run a BUY-A/BUY-B/BUY-C Daily visual proof.
4. Correct rule interpretation only.
5. Run a causal event audit.
6. Run a zero-cost backtest.
7. Research SELL, invalidation, and position management.
8. Validate the unified Daily strategy.
9. Resume Market-Bar research.
10. Project the same trading philosophy into MMA.
11. Compare Daily and Market-Bar timing.

The immediate next task is explicitly visual, not a coding backtest: display
machine-interpreted BUY-A, BUY-B, and BUY-C candidates on Daily charts and
compare them with the intended locations.  It must not calculate future return,
PnL, or optimize thresholds.

## 14. Commit map

| Area | Commit |
|---|---|
| Daily UP entry comparison | `569e774235cd5a9ae6711cb0b0c04f721ff0c166` |
| Daily DOWN reversal proof | `eb3c0824c8fa689072898a53beeb25d156b03605` |
| Strategy-improvement audit | `c117865a3e452204ca4af0b56c2bbee67340520a` |
| MA10 direction A/B proof | `d7fed0580e911a9615998c01cf318a879ccc44f3` |
| DOWN box daily strategy proof | `7d932e19122df48182733a5a8c34bdffe8afe9d3` |
| DOWN box lifecycle correction | `a83cb58800ff83cbdf4add6399b4cdcaed625b14` |
| DOWN box 5-minute execution proof | `a23480074d8f334ee4eaa23d0b9cef3fcd501f73` |
| DOWN box long-history proof | `6cc4d292f5c8a3b3ca036bd1d7483143274cd72c` |
| Market-Bar geometry freeze sequence | `1bf316467716c48b9c9cbf9d00dd9271e64f1349` through `b4d15344d6cdda51ec674a701daac88d972bbbbe` |
| Market-Bar V1.0 visual proof | `7d1fae535782d458d4fcad89bee0407e21d072b9` |
| MMA role audit | `c42d5713b45de8da1d4423ff271b4f553e53ead5` |
| MMA multi-stock replication | `14ae1eb3c4649a544f400b9790156b38f02ae82b` |
| V1.7 strategy hypotheses | `9085bf7055994aaf1d868c5b007d3831deabe957` |
| V1.8 causal event proof | `ef1b862a0303e5ae0aaff91e144e01aed9105aca` |
| V1.9 zero-cost DEV proof | `78c275a592de2e122c5b054ac8fb9e13bfb929d2` |
| V1.9A decision gate | `1bc0380fe4f3e3a4210bad6d0d19aeeffbe9c419` |
| V1.10 source-readiness plan | `2f51699f008a6fb01cbbdbc45d22cd69678a3cee` |
| V1.11 acquisition failure checkpoint | `38576af0afefcd4a6f8f6f9754471008e970fd7c` |

## 15. Preservation rules

Do not delete, rename, move, or broadly reorganize prior source, tests,
documents, or artifacts for this reset.  Do not change prior strategy behavior
or historical regression outputs.  No credentials, tokens, authentication
material, account data, or raw credential-bearing logs belong in research
documentation.
