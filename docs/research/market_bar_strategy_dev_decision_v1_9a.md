# Market-Bar Compression Strategy DEV Evidence Decision Gate V1.9A

이 문서는 V1.9 zero-cost development proof의 결과를 전략 규칙 변경 없이
동결하고, unseen validation 전에 해석과 다음 가설을 사전 등록한다.

## Scope and freeze

- 개발 종목: `066570`, `035420`, `105560`
- validation 종목: `005380`, `068270` — 이 단계에서 읽거나 계산하지 않음
- 비교 구간: V1.8 common comparison window
  - `066570`: MB086--MB201 (116 bars)
  - `035420`: MB086--MB200 (115 bars)
  - `105560`: MB086--MB200 (115 bars)
- 각 stock/variant는 common window의 첫 bar에서 `FLAT`으로 시작한다.
- MB001--MB085는 causal indicator warm-up에만 사용한다.
- 실행은 signal bar 다음 Market-Bar의 실제 `OPEN`이다.
- exit는 기존 MMA20 down-cross를 사용한다.
- commission, tax, slippage는 모두 0이다.
- 이 문서 작성으로 추가 backtest, parameter tuning, validation 계산 또는
  network data access를 수행하지 않았다.

V1.9 거래·event·equity 산출물은 이 문서의 기준 결과이며 재계산으로
변경하지 않는다.

## Original C1 hypothesis

V1.7에서 사전 등록한 C1 가설은 다음과 같다.

> compressed Market-Bar state에서 발생하는 raw MMA20 up-cross를 제거하면
> C0 raw-cross보다 signal quality가 개선될 가능성이 있다.

가설 문구와 C1 규칙은 변경하지 않는다.

## Frozen development evidence

| stock | C0 completed | C1 completed | C0 realized | C1 realized | C0 MTM | C1 MTM | C0 PF | C1 PF | C0 MDD | C1 MDD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 066570 | 4 | 2 | +80.84% | -1.01% | +80.84% | -1.01% | 15.19 | 0.54 | -15.23% | -9.78% |
| 035420 | 9 | 4 | -2.86% | -7.94% | -2.86% | -7.94% | 0.96 | 0.00 | -24.46% | -13.49% |
| 105560 | 9 + open 1 | 2 + open 1 | -18.33% | -6.18% | -5.84% | +8.18% | 0.15 | 0.00 | -18.33% | -7.29% |

`105560`은 terminal open position이 있으므로 realized와 terminal MTM을
분리해 해석한다.

### Removed/retained cohort observation

Entry 시점의 causal `COMPRESSED[T]`만으로 C0 entry를 분류했다. 거래 결과를
분류 기준으로 사용하지 않았다.

| stock | REMOVED_AS_COMPRESSED mean | RETAINED_BY_C1 mean |
|---|---:|---:|
| 066570 | +43.08% | -0.49% |
| 035420 | +1.47% | -2.04% |
| 105560 | -1.93% | -3.13% |

세 개발 종목 모두 관찰 표본에서 `REMOVED_AS_COMPRESSED` 평균이
`RETAINED_BY_C1`보다 높았다. 이는 아래의 exploratory observation으로만
기록하며 C1 지지 또는 alpha 증거로 사용하지 않는다.

## Decisions

### C1 signal-quality decision

`STRATEGY_C1_SIGNAL_QUALITY = NOT_SUPPORTED_DEV`

근거는 다음을 함께 사용했다.

- 066570의 대형 positive outcome을 C1이 제거했다.
- 035420에서도 C1 realized return과 PF가 개선되지 않았다.
- 105560은 MTM/MDD가 개선됐지만 realized return과 PF만으로 일관된
  quality improvement를 보이지 않았다.
- removed/retained cohort가 C1의 quality-improvement 방향을 지지하지 않았다.

C1은 validation에서 제거하지 않는다. 원래 pre-registered hypothesis의
out-of-development 재현성을 확인하기 위해 control과 함께 유지한다.

### Selectivity/risk observation

개발 결과에서 C1은 trade count, exposure, MDD를 줄이는 경향을 보였다.
이를 다음으로 별도 기록한다.

`C1_SELECTIVITY_RISK_REDUCTION = OBSERVED_DEV`

이는 신호 품질 또는 alpha 지지가 아니며, C1 strategy supported로 해석하지
않는다.

### H1 compression-release decision

완료 cycle은 전체 1건이다.

`H1_COMPRESSION_RELEASE = INCONCLUSIVE_SPARSE_DEV`

단일 거래 결과로 H1 규칙의 강화·완화·우월성을 주장하지 않는다.

### Common exit and Market-Bar-only implementation

- MMA20 down-cross 공통 exit: `MECHANICALLY_SUPPORTED`
- Calendar FAST/SLOW, daily MA, volume 또는 future-return을 사용하지 않고
  frozen Market-Bar causal path만 구현: `MARKET_BAR_ONLY_IMPLEMENTATION = SUPPORTED`
- 이는 exit 최적성이나 alpha 주장이 아니다.

### Overall development result

`DEV_MIXED`

V1.9 구현/causality는 성공했지만, C0 결과는 종목별로 혼합되어 있고,
원래 C1 signal-quality hypothesis는 개발 표본에서 지지되지 않았으며,
H1은 sparse하다. 이 결과만으로 Market-Bar strategy family 전체를
기각하지 않으며 `DEV_PROMISING`으로 과장하지도 않는다.

## DEV-generated exploratory observation

`DEV_GENERATED_OBSERVATION_1`

개발 표본에서 compressed MMA20 cross를 제거한 cohort의 평균 trade return이
세 종목 모두 retained cohort보다 높았다. 이 관찰은 작은 개발 표본,
종목별 상이한 outcome, open-at-end 처리를 포함한 descriptive observation일
뿐이다. 즉시 compressed-only strategy를 개발 종목에서 재백테스트하지 않으며,
post-DEV hypothesis와 validation 결과가 동시에 섞이지 않도록 한다.

## Pre-registered validation hypothesis

### V2_COMPRESSED_MMA20_CROSS

가설:

> Market-Bar compressed state의 MMA20 up-cross는 non-compressed MMA20
> up-cross보다 열등하지 않을 가능성이 있으며, 개발에서 관찰된 cohort
> 차이가 unseen data에서도 반복될 수 있다.

상태: `DEV_GENERATED`, `VALIDATION_REQUIRED`
현재 `SUPPORTED` 판정은 금지한다.

### C2_COMPRESSED_ONLY

validation-only exploratory variant의 정확한 사전등록 계약:

```text
BUY[T] = C0_UP_CROSS[T]
         AND COMPRESSION_STATE_AVAILABLE[T]
         AND COMPRESSED[T] == true

EXIT[T] = 기존 MMA20 down-cross
EXECUTION = T+1 actual Market-Bar OPEN
```

다음 파라미터는 V1.7/V1.8과 동일하게 고정한다.

- MMA5, MMA10, MMA20, MMA60
- MB_ATR20
- past 60 Market Bars
- causal Q25
- recent compression previous 5 Market Bars
- MMA5/10/20 cluster
- MMA20 non-declining 조건은 C2에 추가하지 않음

**C2는 066570/035420/105560 개발 세트에서 backtest하지 않는다.**
C2 성능 평가는 unseen validation에서만 수행한다.

## Validation registry and order

기존 registry 후보는 변경하지 않는다.

1. `005380`: `BALANCED_STRATEGY_VALIDATION_CANDIDATE` (primary)
2. `068270`: `SLOW_DOMINANT_STRESS_VALIDATION_CANDIDATE` (secondary stress)

005380 결과가 좋지 않다는 이유만으로 068270을 취소하거나 다른 종목으로
교체하지 않는다. source failure만 기술적 예외로 기록할 수 있다.

validation에서는 최소한 다음을 함께 유지한다.

- `C0_RAW_MMA20` control
- `C1_FILTER_COMPRESSED` original pre-registered hypothesis
- `C2_COMPRESSED_ONLY` DEV-generated validation hypothesis
- `H1_COMPRESSION_RELEASE` original sparse hypothesis

C2가 성공하면 `NEW_HYPOTHESIS_VALIDATION_SUPPORTED`, 실패하면
`DEV_COMPRESSED_COHORT_OBSERVATION_NOT_REPLICATED`로 종료한다. C2 결과를
보고 compression percentile, 60-bar lookback 또는 MMA 조건을 사후 변경하지
않는다.

## Checkpoint and provenance

- V1.8 design checkpoint: `9085bf7055994aaf1d868c5b007d3831deabe957`
- V1.8 event artifact: `data/processed/strategy_review/market_bar_compression_strategy_events_v1_8.csv`
- V1.9 proof artifact: `data/processed/strategy_review/market_bar_compression_strategy_zero_cost_dev_proof_v1_9.json`
- V1.9 trade ledger: `data/processed/strategy_review/market_bar_compression_strategy_trade_ledger_v1_9.csv`
- validation signal/return/PnL 계산: 수행하지 않음
- network calls: `0`

이 문서는 V1.9 숫자를 재생성하거나 변경하지 않는 decision gate이며,
다음 단계는 validation source readiness와 materialization 계획 수립이다.
