# Codebase Token-Efficiency Audit V0.1

## Executive summary

This is an offline, descriptive audit of `src/**/*.py`, `tests/**/*.py`, and
`docs/research/*.md`.  It does not refactor or move code, change a strategy,
run a backtest, or call a network API.  The goal is not general cleanup: it is
to minimize the context needed for the active **Daily MA Unified Swing** track
without damaging reproducibility of prior research.

The evidence supports a narrow policy:

1. preserve frozen Daily UP/DOWN, Market-Time, Market-Bar, and V1.8/V1.9 proof
   modules unchanged;
2. make future Daily MA code use a small, new shared core only;
3. keep versioned research runs thin orchestration layers;
4. use the existing master checkpoint as the start document and keep historical
   Market-Bar material `REFERENCE_ON_DEMAND`.

## Addendum: existing Daily BUY V0.1 as the measured case

The V0.1 BUY visual proof is already implemented, tested, and intentionally
uncommitted active research. This audit inspected it read-only. Its source is
`ACTIVE_PRIMARY`, its direct test is `ACTIVE_PRIMARY_TEST`, and
`strategy_review/chart.py` is `ACTIVE_SUPPORT`.

The V0.1 outcome-free artifact reports A=26, provisional B=343, C=30, A+B=21,
and B+C=14; MA10 slope rejection is zero; six review windows and twelve charts
were created. These are not profitability results. The high B count marks
BUY-B as an `ACTIVE_UNSTABLE_CONTRACT`; it must not become a stable shared API
before visual review freezes its boundary. The overlaps mean future signal
design must permit multiple tags on a bar rather than prematurely forcing one
mutually exclusive enum.

The immediate context burden is not principally duplicate arithmetic.  It is
the active V0.1 visual proof importing `_load_stock` from the 1,579-line frozen
DOWN_BOX proof, which in turn imports the 359-line experimental UP proof for a
private Daily loader.  This coupling is valid historical reuse today, but it is
the first low-risk boundary to replace **for new Daily code only**.

## Current shape and inventory

The audit tool recorded the complete per-file inventory—path, line count,
approximate bytes, classification, direct imports, importers, matching tests,
research version, active relevance, risk, and token burden—in its ignored JSON
output.  Counts include the currently uncommitted V0.1 Daily proof and its test.

| Scan scope | Files |
|---|---:|
| `src/**/*.py` | 100 |
| `tests/**/*.py` | 75 |
| `docs/research/*.md` | 6 |

| Classification | Files |
|---|---:|
| `ACTIVE_PRIMARY` | 2 |
| `ACTIVE_PRIMARY_TEST` | 1 |
| `ACTIVE_SUPPORT` | 1 |
| `PRODUCTION_FOUNDATION` | 20 |
| `FROZEN_RESEARCH` | 123 |
| `HISTORICAL_ARCHIVE` | 5 |
| `LEGACY_OR_UNUSED_CANDIDATE` | 29 |

`ACTIVE_PRIMARY` currently means the Daily MA master checkpoint, the V0.1
visual-proof module, and its test.  `ACTIVE_SUPPORT` is the reusable Daily
chart renderer.  The classification is a navigation aid, not a deletion list;
in particular, an unreferenced research module remains an
`UNREFERENCED_RESEARCH_ARTIFACT`, not dead code to remove.

## Largest context burdens

The largest source files are predominantly frozen research, so size alone is
not a refactoring priority.

| Lines | File | Status |
|---:|---|---|
| 1,579 | `src/kiwoom_daily/down_box_daily_execution_proof.py` | `FROZEN_RESEARCH` |
| 1,378 | `src/kiwoom_daily/down_box_v03a_proof.py` | `FROZEN_RESEARCH` |
| 1,289 | `src/kiwoom_minute/down_box_execution_proof.py` | `FROZEN_RESEARCH` |
| 1,200 | `src/kiwoom_daily/market_bar_mma_role_stability_audit.py` | `FROZEN_RESEARCH` |
| 1,197 | `src/backtest_engine/down_box_strategy.py` | `PRODUCTION_FOUNDATION` |
| 1,161 | `src/kiwoom_daily/market_clock_audit.py` | `FROZEN_RESEARCH` |
| 1,123 | `src/kiwoom_daily/market_bar_compression_strategy_causal_signal_event_proof_v1_8.py` | `FROZEN_RESEARCH` |
| 1,098 | `src/kiwoom_daily/market_bar_compression_strategy_zero_cost_dev_proof_v1_9.py` | `FROZEN_RESEARCH` |
| 1,082 | `src/kiwoom_daily/market_bar_strategy_validation_source_acquisition_materialization_v1_11.py` | `FROZEN_RESEARCH` |
| 931 | `src/kiwoom_daily/market_clock_compression_audit_v0_2.py` | `FROZEN_RESEARCH` |

The largest tests are likewise historical or foundation regressions: the top
three are 749-line Kiwoom Daily pipeline, 652-line DOWN_BOX strategy, and
556-line Daily eligibility tests.  The five research documents range from 46
to 376 lines; the 354-line Daily-unification checkpoint is the only current
context document.

## Active versus frozen dependency analysis

The current V0.1 proof directly uses:

```text
Daily MA V0.1 (736 lines)
  -> strategy_review/chart.py (870 lines)
  -> backtest_engine/{models, indicators, calendar, validation} (713 lines)
  -> down_box_daily_execution_proof.py (1,579 lines; private _load_stock)
       -> kiwoom_minute/small_up_path_proof.py (359 lines; private loader)
```

The actual static transitive closure is 17 project-local source files / 8,109
lines. Adding the 354-line master checkpoint and 186-line direct test makes
the practical V0.1 investigation set about 19 files / 8,649 lines. It does
**not** require Market-Bar V0/V1 geometry, MMA compression,
validation acquisition, or their tests for a normal Daily BUY task.  Those
tracks should remain `REFERENCE_ON_DEMAND` until a prompt explicitly needs
their evidence.

| V0.1 responsibility | Functions | Approx. function lines | Assessment |
|---|---:|---:|---|
| Daily feature calculation | 7 | 118 | generic primitive candidate |
| BUY state machine | 3 | 143 | unstable strategy contract; do not stabilize |
| Visual selection/annotation | 3 | 91 | keep run-local initially |
| Metadata serialization | 3 | 37 | share only after a second active consumer |
| Loading/orchestration | 3 | 166 | replace frozen-loader boundary in new code only |

The static import graph shows that `models.py`, `validation.py`,
`trading_calendar.py`, and `indicators.py` are production foundations used by
many tracks.  They are suitable dependencies, but their contracts—not every
historical importer—are the relevant context for new Daily code.

## Duplicate logic and constants

AST analysis distinguishes repeated names from exact normalized structural
duplicates.  It is evidence, not permission to consolidate.

| Candidate | Evidence | Classification | Decision |
|---|---|---|---|
| `main` | 56 files | `SHARE_FOR_NEW_CODE_ONLY` | Do not centralize historical CLIs; use thin new run modules. |
| `_json_default` | 27 names; 5 structural matches | `FROZEN_DUPLICATION_ACCEPTED` | A new JSON helper may be used by new code only. |
| `_decimal` | 15 names; 9 structural matches | `FROZEN_DUPLICATION_ACCEPTED` | Do not alter old parsers; a strict new helper is reasonable later. |
| `_csv_value`, `_write_csv` | 9 / 7 repeated | `FROZEN_DUPLICATION_ACCEPTED` | Preserve historical schemas; share only future artifact plumbing. |
| `_percentile`, `_median`, `_distribution` | 5 / 7 / 8 repeated | `FROZEN_DUPLICATION_ACCEPTED` | Not needed for the current Daily BUY visual proof. |
| Daily-bar test `_bar` fixtures | 6 occurrences | `SHARE_FOR_NEW_CODE_ONLY` | A small Daily MA fixture factory is optional only after a second active test needs it. |
| `EPSILON`, `TARGETS`, `MINUTE_ROOT` constants | repeated in Market-Bar files | `DO_NOT_TOUCH` | Frozen geometry/provenance contracts. |
| MA periods and research start/end constants | repeated across proofs | `SHARE_FOR_NEW_CODE_ONLY` | New Daily configuration can own its explicit values; do not rewrite old scripts. |

No audit finding supports refactoring frozen Market-Bar geometry, V1.8/V1.9
causal/backtest proofs, Daily UP proofs, or DOWN_BOX lifecycle/proofs.  Their
duplication is part of reproducible historical artifacts.

## Documentation context policy

`algorithm_research_checkpoint_before_daily_unification.md` is an adequate
source of truth for the transition, but it is an inventory/checkpoint rather
than a short operational brief.  Do not create a second document now.  At the
first approved Daily-core refactor, extract a concise future
`docs/research/CURRENT_RESEARCH.md` that links to (rather than repeats) the
master checkpoint and contains only active contracts, current phase, forbidden
actions, and the minimal read-set.

| Documentation group | Policy |
|---|---|
| Daily-unification checkpoint | `CURRENT_CONTEXT` |
| Market-Bar V1.7/V1.8/V1.9A documents | `REFERENCE_ON_DEMAND` |
| V1.10 source plan and registry | `HISTORICAL_ARCHIVE` unless Market-Bar resumes |

## Minimal Daily MA context proposal

For a future Daily BUY change, begin with only:

1. the concise current-context document (until it exists, the master
   checkpoint);
2. the active Daily MA module(s) and their direct tests;
3. `backtest_engine/models.py` and `indicators.py` contracts;
4. a dedicated Daily adjusted-bar read adapter contract;
5. `strategy_review/chart.py` only when rendering changes.

Read `down_box_daily_execution_proof.py` only to compare a specific frozen
finding, never merely to obtain a loader.  Read Market-Bar/MMA work only for a
requested Daily-versus-Market-Bar projection.

| Proxy | Current V0.1 chain | Proposed new-code chain |
|---|---:|---:|
| Likely files to read | about 19 | 6--7 |
| Likely source/doc lines | about 8,649 incl. checkpoint/test | about 1,400--2,000 |
| Historical proof dependency | frozen proof transitive chain | none |
| Context reduction | — | `HIGH` estimate |

The proposed numbers are planning ranges, not measured Codex-token values.

## Proposed future Daily package (not created)

```text
src/kiwoom_daily/daily_ma/
    features.py      # SMA, slope, below-MA runs, distance
    primitives.py    # breakout, breakdown, provisional inflection
research_runs/
    daily_buy_visual_v0_1.py  # thin run-local BUY/visual orchestration
```

Option A above is preferred over an immediate `signals.py`, `visual_review.py`,
and `metadata.py` split: more files would currently increase the read-set while
BUY contracts remain unstable. The package must not force historical modules
to import it. Existing V0.x/V1.x scripts remain frozen.

## Candidate priority matrix

| Candidate | Token reduction | Refactor risk | Reproducibility risk | Cost | Recommendation |
|---|---|---|---|---|---|
| New Daily adjusted-bar adapter, avoiding private frozen loaders | High | Low | Low | Low | `DO_NOW` after visual review approval |
| New Daily MA features/primitives/signals core | High | Low | Low | Medium | `DO_NOW` after BUY contract V0.2 freezes |
| Thin versioned Daily run module | Medium | Low | Low | Low | `DO_NOW` with new core |
| Short current-context document | High | Low | Low | Low | `DO_LATER` at first approved refactor |
| New shared artifact/JSON helper | Medium | Medium | Low | Medium | `DO_LATER`; only after two active consumers |
| New shared Daily test fixture helper | Low | Low | Low | Low | `DO_LATER`; only after actual repeated active fixture cost |
| Extend shared chart renderer | Medium | Medium | Medium | Medium | `DO_LATER`; existing renderer is currently adequate |
| Refactor frozen Daily/Market-Bar proofs | Medium | High | High | High | `SKIP` |
| Rename or migrate V0/V1 research modules | Low | High | High | High | `DO_NOT_TOUCH` |
| Generic provider/plugin/DI framework | Low | Medium | Medium | High | `SKIP` |

## Test-cost policy

Use three levels rather than reading/running all 500+ tests each iteration:

1. **Level 1:** active module and direct tests on each edit.
2. **Level 2:** active research plus direct foundation/chart regressions before
   a local checkpoint.
3. **Level 3:** full pytest, lint, compile, and `git diff --check` before a
   commit/push or a material architecture milestone.

This preserves safety while keeping normal Daily rule-interpretation work from
requiring unrelated Market-Bar context or execution tests on every change.

## Recommended phases (not executed)

### Phase A — New Daily research core only

After visual review approves the BUY contract, introduce the small Daily data,
feature, primitive, signal, and visual boundaries above.  Expected token
benefit: high.  Risk: low.  Regression: active Daily tests plus foundation and
chart tests.

### Phase B — Shared low-risk utilities only when earned

Introduce a serializer or fixture helper only when at least two active Daily
modules duplicate the same contract.  Expected benefit: medium.  Risk: low to
medium.  Regression: consumers plus artifact-schema tests.

### Phase C — Historical cleanup decision

Do not begin automatically.  Any cleanup of frozen research requires a
separate reproduction plan, frozen-artifact comparison, and explicit approval.

## Codex startup contract

> Read the current Daily research context, the active `daily_ma` code and its
> direct tests, then the DailyBar/indicator/data-adapter contracts.  Do not
> search or read Market-Bar V0/V1, MMA, DOWN_BOX, or validation-acquisition
> research unless this task explicitly requests that evidence.  Preserve frozen
> modules and artifacts; use new shared code only for new Daily work.

## Audit limits

Static imports cannot prove runtime reachability, and AST structural similarity
does not prove semantic equivalence.  Therefore `LEGACY_OR_UNUSED_CANDIDATE`
means “review on demand,” never “delete.”  This audit made no source/strategy
behavior changes and no network calls.
