# Market-Bar Strategy Validation Registry V1.8

This registry is an availability/source-quality registry only. V1.8 does not
download, materialize, signal-calculate, inspect returns, or calculate PnL for
the validation candidates.

## Development set

| Stock | Role | V1.8 signal inspection |
|---|---|---|
| 066570 | `DEVELOPMENT_ONLY` | permitted on frozen V1.2C artifact |
| 035420 | `DEVELOPMENT_ONLY` | permitted on frozen V1.5 artifact |
| 105560 | `DEVELOPMENT_ONLY` | permitted on frozen V1.5 artifact |

Development results must not be described as out-of-sample validation and must
not be used to replace validation candidates because of their eventual
performance.

## Pre-registered validation candidates

| Stock | Role | Selection basis | V1.8 status |
|---|---|---|---|
| 005380 | `BALANCED_STRATEGY_VALIDATION_CANDIDATE` | availability/regime planning metadata | registry only; not inspected |
| 068270 | `SLOW_DOMINANT_STRESS_VALIDATION_CANDIDATE` | availability/regime planning metadata | registry only; not inspected |

These are strategy-unseen validation candidates. V1.8 does not calculate
signals, future returns, or PnL for either stock.

## Replacement policy

A validation candidate may be replaced only for a technical data reason:

- `SOURCE_UNAVAILABLE`
- `STRUCTURAL_GAP`
- `SOURCE_QUALITY_FAILURE`

The replacement must be selected from the existing pre-ranked universe using
availability-only evidence. Development performance cannot justify a
replacement.

## Freeze boundary

This registry is frozen before any V1.9 zero-cost performance comparison. The
V1.9 comparison must use the same common-comparison-window protocol after the
causal event proof passes. No validation signal or outcome is present in this
document.
