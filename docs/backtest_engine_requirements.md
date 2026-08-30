# 백테스트 엔진 요구사항 및 설계서

## 1. 문서 목적

이 문서는 `docs/strategy_v1.md`의 전략을 재현 가능하고 검증 가능한 백테스트로 구현하기 위한 엔진 요구사항과 설계를 정의한다. 전략 규칙과 범용 백테스트 기능을 분리하고, 일봉 신호와 5분봉 체결 사이의 시간 관계를 명시하여 룩어헤드 편향을 방지하는 것이 핵심 목표다.

본 문서는 전략 원문을 변경하거나 대체하지 않는다. 다만 전략 원문에서 구현 의미가 열려 있던 항목은 이 문서의 「Strategy V1 구현 계약」으로 확정하며, 엔진은 그 계약을 따라야 한다. 전략 원문의 명시 규칙과 이 계약 사이에 새로운 직접 충돌이 발견되면 임의로 선택하지 않고 구현을 중단하여 문서부터 정정한다.

## 2. 범위

### 포함 범위

- KOSPI/KOSDAQ 보통주 중심의 종목별 백테스트
- 일봉 기반 추세 판정, 진입 후보 선정, Core 목표 결정
- 5분봉 기반 Core 실행 및 Tactical 비중 조절
- 완성된 봉만 사용하는 이벤트 기반 시뮬레이션
- 현금, 주문, 체결, 포지션, 손익 및 비용 관리
- 신호부터 체결까지 전 과정을 추적할 수 있는 원장
- 매도 방식 A/B/C 비교와 기준안 C의 재현
- 단위·통합·회귀 테스트 및 데이터 품질 검증

### 제외 범위

- 실시간 주문 및 증권사 주문 연동
- 미래 데이터 보간이나 누락 가격 추정
- 합병·종목코드 변경 관계의 자동 추론
- 포트폴리오 종목 선정 모델 자체의 개발
- 장중 호가, 체결 틱, 시장충격의 정밀 시뮬레이션
- 전략 원문에 없는 최적화 규칙의 자동 추가

## 3. 설계 원칙

1. **전략과 엔진 분리**: 전략은 상태와 목표 비중을 결정하고, 엔진은 시간 진행·주문·체결·회계·성과 계산을 담당한다.
2. **완성 봉 원칙**: 일봉과 5분봉 모두 해당 봉이 완료되기 전에는 값을 참조하지 않는다.
3. **다음 봉 체결 원칙**: 5분봉 종가로 생성된 신호는 원칙적으로 다음 거래 가능한 5분봉의 실제 시가에서만 체결한다. 연속 봉에서는 신호 봉의 `bar_end_at`과 다음 봉의 `bar_start_at`이 같은 wall-clock timestamp일 수 있으므로 datetime의 strict 대소관계가 아니라 event key와 bar sequence로 인과관계를 검증한다. 단, 장 마지막 봉의 Core ENTER와 Tactical 신호는 이월하지 않으며, FULL_EXIT만 다음 거래 가능한 시점으로 이월한다.
4. **가격 계열 분리**: 신호와 지표에는 수정주가 계열을 사용하고, 체결과 현금 회계에는 당시 실제 거래가격(raw price)을 사용한다. 두 계열을 덮어쓰거나 혼합하지 않는다.
5. **결정론적 실행**: 동일 입력, 설정, 코드 버전이면 주문·체결·성과가 동일해야 한다.
6. **감사 가능성**: 모든 신호, 거절, 주문, 체결 및 상태 전이에 시간과 사유를 기록한다.
7. **추정 금지**: 누락 봉, 거래정지, 거래량 0, 시가 누락 또는 실제 체결 가능한 가격 부재 시 임의 가격을 만들지 않는다. 주문은 `UNFILLED` 또는 `REJECTED`로 기록하고 action별 만료·이월 정책을 적용한다.

## 4. 용어와 시간 기준

- `T`: 일봉이 완료된 거래일
- `T+1`: 거래소 캘린더상 다음 거래일
- `signal_generated_at`: 신호 계산에 사용한 마지막 봉의 종료 시각
- `signal_available_at`: 신호를 실제로 알 수 있게 된 최초 시각
- `execution_signal_at`: 5분봉 조건이 확정된 시각
- `executed_at`: 실제 체결을 가정한 다음 거래 가능 봉의 시작 시각
- `eligible_at`: 선택된 체결 대상 봉의 시작 시각. 다음 봉 경계에서는 `signal_available_at`과 같을 수 있음
- `bar_start_at`: 5분봉 포함 구간의 시작 시각
- `bar_end_at`: 5분봉 포함 구간의 종료 시각
- `stock_full_weight`(FULL): 각 종목에 설정된 **전체 포트폴리오 대비 최대 배정 비중**
- Core: 일봉이 결정하는 FULL 내부의 90% 기본 배정
- Tactical: 보유 중 5분봉 교차 신호에 따라 0 또는 1 unit으로 조절하는 FULL 내부의 10% 배정

### 4.1 Weight 단위 계약

FULL은 전체 포트폴리오의 100%를 뜻하지 않는다. 모든 weight 값은 다음 두 단위를 구분한다.

- `_fraction_of_full`: 해당 종목 FULL 내부 비율
- `_weight`: 전체 포트폴리오 자산 대비 비율

계산식은 다음과 같다.

```text
core_target_weight = stock_full_weight * core_fraction_of_full
tactical_unit_weight = stock_full_weight * tactical_unit_fraction_of_full
max_position_weight = stock_full_weight
desired_total_weight = core_target_weight + tactical_units * tactical_unit_weight
```

V1 기본값은 `core_fraction_of_full=0.90`, `tactical_unit_fraction_of_full=0.10`, `tactical_units in {0, 1}`이다. 예를 들어 `stock_full_weight=0.10`인 종목의 Core 목표는 전체 포트폴리오의 9%, Tactical 1 unit은 1%, 최대 목표 보유 비중은 10%다. `core_fraction_of_full=0.90`을 전체 포트폴리오의 90%로 해석해서는 안 된다.

문서에서 “Core 90%”, “Tactical 10%”, “90%↔100%”라고 줄여 쓸 때에는 항상 **해당 종목 FULL 내부 비율**을 의미한다. 실제 포트폴리오 weight는 위 식으로 변환한다. 가격 변동으로 실제 평가 비중은 목표에서 벗어날 수 있으므로 `target_weight`와 `actual_weight`도 별도 필드로 관리한다.

### 4.2 시간 기준

모든 내부 timestamp는 timezone-aware 값으로 저장한다. 한국 주식시장 데이터는 `Asia/Seoul`로 해석하고, 출력 시 timezone을 명시한다. 날짜만 있는 `report_date` 및 거래일은 거래소 캘린더의 로컬 날짜다.

Source adapter는 원천 timestamp가 봉 시작인지 종료인지 `START` 또는 `END`로 반드시 선언해야 한다. 엔진은 이를 추정하지 않으며 선언이 없거나 봉 간격과 모순되면 validation error로 중단한다. Adapter는 이 선언을 이용해 `bar_start_at`, `bar_end_at`, `signal_available_at`을 생성한다. 역사 데이터에서 별도 지연 정보가 없다면 `signal_available_at = bar_end_at`이고, 더 늦은 원천 가용 시각이 명시되면 그 시각을 사용한다.

Strategy V1의 장중 대상은 KRX 정규장 `09:00 <= bar_start_at < bar_end_at <= 15:30`이다. 시간외, 장전 및 장후 데이터는 지표와 체결에서 제외한다.

### 4.3 Event key와 bar identity

Wall-clock timestamp가 같은 이벤트의 인과관계는 다음 구조의 정렬 가능한 key로 표현한다.

```text
EventKey = (timestamp, event_phase, deterministic_tie_break)
```

- `timestamp`: timezone-aware datetime이며 비교할 때 동일 UTC instant 기준을 사용한다. 결과 metadata의 canonical timezone identifier는 ISO offset과 별도로 `Asia/Seoul`을 기록한다.
- `event_phase`: 동일 timestamp 내부의 논리적 처리 단계
- `deterministic_tie_break`: 입력 행 순서와 무관한 canonical tuple

V1 event phase의 최소 순서는 다음과 같다. 숫자 사이에는 향후 phase를 삽입할 수 있도록 간격을 둔다.

| 순서 | event phase | 의미 |
|---:|---|---|
| 0 | `CORPORATE_ACTION` | 향후 같은 시각의 회계 event를 먼저 적용하기 위한 예약 phase |
| 10 | `PREVIOUS_BAR_CLOSE_AVAILABLE` | bar N 종료와 데이터 가용성 확정 |
| 20 | `SIGNAL_EVALUATION` | 사용 가능한 지표로 신호 평가 |
| 30 | `ORDER_CREATED_OR_SCHEDULED` | intent 기록 및 다음 bar 주문 예약 |
| 40 | `NEXT_BAR_OPEN_FILL` | 선택된 다음 bar의 raw open 체결 |

따라서 연속 봉 경계가 09:05라면 `signal_available_at == eligible_at == executed_at == 09:05`일 수 있지만 반드시 `fill_event_key > signal_event_key`여야 한다. Timestamp equality는 허용하고 `executed_at >= signal_available_at`을 요구한다.

각 5분봉에는 다음 identity를 부여한다.

- `bar_id`: `(stock_code, bar_start_at의 UTC instant, bar_end_at의 UTC instant)`에서 만든 안정적 identity
- `bar_sequence`: 종목별 정규장 시계열을 canonical 시간순으로 정렬한 뒤 부여하는 0부터 시작하는 연속 정수. 거래일 경계에서 reset하지 않음

신호는 `signal_source_bar_id`와 `signal_source_bar_sequence`, 주문은 `eligible_bar_id`와 `eligible_bar_sequence`, 체결은 `fill_bar_id`와 `fill_bar_sequence`를 보존한다. 다음 봉 체결은 반드시 `fill_bar_sequence > signal_source_bar_sequence`를 만족해야 하며, 정상적인 바로 다음 봉은 두 sequence의 차이가 1이다. Generic scheduler가 다음 봉을 찾지 못하면 bar identity를 추정하지 않고 `NO_NEXT_BAR`를 반환한다.

동일 timestamp와 phase에서 여러 종목·이벤트를 정렬하는 `deterministic_tie_break`는 다음 tuple로 고정한다.

```text
(stock_code, source_bar_sequence, entity_kind_rank, stable_entity_id)
```

`source_bar_sequence`가 없는 외부 event는 명시된 sentinel `-1`을 사용한다. `entity_kind_rank`는 문서화된 enum 순서이며, `stable_entity_id`는 canonical 입력으로부터 결정론적으로 생성하거나 입력에서 제공받는다. Random UUID, 입력 위치 및 append 도착 순서를 tie-break로 사용하지 않는다.

## 5. 입력 데이터 계약

### 5.1 일봉

필수 필드:

| 필드 | 형식 | 규칙 |
|---|---|---|
| `stock_code` | string | 6자리, leading zero 보존 |
| `trade_date` | date | 종목별 유일, 오름차순 |
| `raw_open`, `raw_high`, `raw_low`, `raw_close` | decimal | 당시 실제 거래가격; 가격 관계 규칙 충족 |
| `signal_open`, `signal_high`, `signal_low`, `signal_close` | decimal | 수정주가 계열; 가격 관계 규칙 충족 |
| `raw_volume`, `signal_volume` | integer | 0 이상; 조정 여부와 방식을 metadata에 명시 |

일봉 수익률, 이동평균 및 피벗 등 모든 전략 신호는 `signal_*` 수정주가 계열로 계산한다. 일간 상승률은 `(signal_close_t / signal_close_{t-1} - 1) * 100`이다. SMA10, SMA20, SMA60을 계산하며, 추세 기울기는 현재 SMA와 5거래일 전 SMA의 변화율이다. 정확한 산식은 `(MA_t / MA_{t-5} - 1) * 100`으로 고정하며, 분모가 없거나 0이면 판정 불가다.

### 5.2 5분봉

필수 필드:

| 필드 | 형식 | 규칙 |
|---|---|---|
| `stock_code` | string | 6자리 |
| `source_timestamp` | timezone-aware datetime | 원천값 그대로 보존 |
| `source_timestamp_semantics` | enum | adapter가 `START` 또는 `END`로 필수 선언 |
| `bar_start_at`, `bar_end_at` | timezone-aware datetime | adapter가 명시적 의미로 계산 |
| `signal_available_at` | timezone-aware datetime | `bar_end_at` 이상 |
| `raw_open`, `raw_high`, `raw_low`, `raw_close` | decimal | 당시 실제 거래가격; 체결·회계용 |
| `signal_open`, `signal_high`, `signal_low`, `signal_close` | decimal | 수정주가 계열; 신호·지표용 |
| `raw_volume`, `signal_volume` | integer | 0 이상 |
| `session` | enum | V1은 `REGULAR`만 허용 |

5분봉 SMA10, SMA20, SMA60은 `signal_close`를 사용하며 거래일마다 초기화하지 않고 종목의 정규장 시계열 전체에서 연속 계산한다. 휴장·야간 시간은 봉으로 채우지 않는다.

### 5.3 보조 입력

- 거래소 거래일 캘린더
- `stock_code`, `market`, 종목명 유효기간을 가진 historical stock master
- 전략 대상 종목과 최대 종목별 배정 비중
- 수수료, 세금, 슬리피지 설정
- 실제 장기 실행에서는 split, reverse split, 감자 등 보유 수량·현금을 바꾸는 corporate-action event와 coverage metadata
- 선택적으로 벤치마크 일별 수익률

`CorporateActionEvent` 확장 계약의 최소 필드는 다음과 같다.

| 필드 | 의미 |
|---|---|
| `stock_code` | 대상 6자리 종목코드 |
| `effective_at` | 포지션·현금에 효력이 발생하는 timezone-aware 시각 |
| `action_type` | split, reverse split, capital reduction 등 원천 의미를 보존하는 유형 |
| `quantity_factor` | 기존 보유수량에 적용할 배수; 양의 Decimal |
| `cash_adjustment` | 해당 이벤트로 반영할 종목별 현금 조정액; 없으면 0 |
| `source` | 출처 및 원천 레코드 식별자 |

이번 문서는 이벤트 삽입 지점과 품질 계약만 정의하며 action별 수량 반올림·단주·세금 처리 로직이나 실제 데이터를 구현하지 않는다.

### 5.4 가격 조정 정책

Strategy V1은 신호 계산에 수정주가인 `signal_ohlcv`, 체결·현금 회계에 당시 실제 거래가격인 `raw_ohlcv`를 사용한다. 주문의 체결 기준 가격은 `raw_open`이며 슬리피지는 이 가격에 별도로 적용한다. 신호 가격으로 체결하거나 raw 가격으로 지표를 계산해서는 안 된다.

실행 metadata에는 최소 `signal_price_basis=ADJUSTED`, `execution_price_basis=RAW`, 조정 데이터 제공자·버전·산식 식별자, 기업행동 및 배당 반영 범위, 거래량 조정 여부를 기록한다. 원천이 두 가격 계열을 제공하지 못하거나 같은 행에서 날짜·timestamp 정렬이 맞지 않으면 자동 환산하지 않고 validation error로 처리한다.

## 6. 전체 구조

```text
Daily/5m Data + Calendar + Stock Master
          + Corporate Action Events
                    |
           Data Validation/Alignment
                    |
              Indicator Engine
                    |
 Daily Trend Classifier + Box Structure Detector
                    |
          Daily Signal Generator
                    |
            Pending Core Action
                    |
          5-Minute Execution Engine
                    |
   Position Manager (Core + Tactical + Cash)
                    |
       Order/Fill/Signal/State Ledgers
                    |
          Performance & Audit Report
```

### 모듈 책임

| 모듈 | 책임 |
|---|---|
| Data Adapter | 소스별 스키마를 표준 OHLCV 계약으로 변환 |
| Validator | 중복, 정렬, 결측, 가격 관계, 캘린더 정합성 검사 |
| Corporate Action Adapter/Validator | 원천 event와 coverage metadata를 검증하고 표준 `CorporateActionEvent`로 제공 |
| Indicator Engine | 수정주가로 SMA와 기울기, 교차, 피벗 등 순수 파생값 계산 |
| Daily Trend Classifier | MA20/MA60 기울기로 상승·하락·혼합·중립·판정불가 결정 |
| Box Structure Detector | 최근 확정 피벗으로 박스 구조의 유효성을 별도 판정 |
| Daily Signal Generator | 전략 필터와 일봉 진입·청산 조건 평가 |
| Pending Action Manager | T 신호를 T+1 이후 실행 가능한 상태로 전환 |
| 5-Minute Execution Engine | 장중 조건 확인, 다음 봉 주문 예약 및 체결 요청 |
| Position Manager | Core/Tactical 목표·실제 수량·sell lock 관리 |
| Portfolio/Accounting | raw 체결가로 현금, 비용, 세금, 평가액, 실현·미실현 손익 계산 |
| Corporate Action Processor | `effective_at`에 Position/Accounting이 적용할 이벤트 hook 제공 |
| Cost Model | 유효기간·시장별 수수료, 세금, 슬리피지 설정 적용 |
| Ledger | 신호·상태·주문·체결·거절 사유를 append-only로 기록 |
| Metrics | 성과·위험·거래 통계와 A/B/C 비교 산출 |

전략 모듈은 주문 API를 직접 호출하지 않고 `DesiredPosition` 또는 `StrategyIntent`만 반환한다. 엔진이 이를 주문 가능성, 현금 및 체결 정책에 따라 처리한다.

## 7. 상태 모델

### 7.1 일봉 상태

`DailyTrendState`:

- `UP`: MA20 5일 기울기 > 0, MA60 5일 기울기 > 0
- `DOWN`: 두 기울기 모두 < 0
- `MIXED`: 두 기울기의 부호가 반대
- `NEUTRAL`: 하나 이상의 기울기가 0
- `INSUFFICIENT_DATA`: 필요한 과거 봉 또는 지표 부족

기울기 상태와 실제 박스 구조를 혼합하지 않는다. `MIXED`는 박스 전략의 필요조건일 뿐 충분조건이 아니다. 별도 `BoxStructureState`는 다음 값을 가진다.

- `VALID`: 확정된 기준 Pivot High/Low가 있고 폭이 25% 이상
- `INVALID`: 피벗은 평가 가능하지만 쌍 또는 폭 조건이 유효하지 않음
- `INSUFFICIENT_DATA`: 60거래일 탐색 또는 확정 피벗 데이터가 부족

T 종가 기준의 deterministic 피벗 선택 규칙은 다음과 같다.

1. 좌2/우2 방식으로 만들어진 pivot 중 `confirmed_at <= T의 일봉 종료 시각`인 것만 사용한다.
2. pivot 발생 거래일이 T를 포함한 최근 60거래일 안에 있는 후보만 남긴다.
3. Pivot Low와 Pivot High에서 각각 발생 거래일이 가장 최근인 하나를 선택한다. 동일 날짜 중복은 입력 오류로 처리하며 값으로 tie-break하지 않는다.
4. 두 기준값이 존재하고 `reference_high > reference_low`이며 `(reference_high - reference_low) / reference_low * 100 >= 25`이면 `VALID`, 아니면 `INVALID`다.
5. 판정 결과에 선택된 pivot ID, 발생일, `confirmed_at`, 가격을 저장한다.

이 규칙은 T 이후의 봉이나 아직 확정되지 않은 pivot을 참조하지 않으며 동일 입력에서 항상 같은 기준 High/Low를 선택한다. 박스 전략은 `DailyTrendState=MIXED`와 `BoxStructureState=VALID`를 동시에 만족할 때만 활성화한다.

### 7.2 Core pending 상태

`PendingCoreAction`은 다음 필드를 가진다.

- `action_id`
- `stock_code`
- `action`: `ENTER`, `FULL_EXIT`
- `stock_full_weight`
- `target_core_weight`: 전체 포트폴리오 단위이며 `stock_full_weight * core_fraction_of_full`
- `generated_trade_date`
- `activation_trade_date`
- `reason`
- `status`: `PENDING`, `ARMED`, `ORDER_SCHEDULED`, `FILLED`, `CANCELLED`, `EXPIRED`, `REJECTED`
- `superseded_by`

한 종목에는 동시에 하나의 유효한 Core pending action만 허용한다. 신규 full exit는 entry를 취소하고 가장 높은 우선순위를 가진다.

- `ENTER`는 `activation_trade_date=T+1`의 정규장 동안만 유효하다. 개별 주문이 체결 불가이면 해당 주문은 `UNFILLED`/`REJECTED`로 종료하고 자동 재시도하지 않는다. 이후 독립적인 신규 실행 조건이 다시 발생하면 당일에 한해 새 주문을 만들 수 있다. 실행 조건 미발생, 마지막 봉 실행신호 또는 장 종료까지 미체결이면 pending action을 `EXPIRED` 처리하고 다음 날로 이월하지 않는다.
- `FULL_EXIT`는 T+1에 활성화한 뒤 청산 체결이 완료될 때까지 유지한다. 장 종료, 다음 봉 부재 또는 체결 불가만으로 만료하지 않는다.
- `FULL_EXIT`가 유효한 동안 Tactical buy는 금지한다.

### 7.3 포지션 상태

- `stock_full_weight`: 해당 종목의 전체 포트폴리오 대비 최대 배정 비중
- `core_target_weight`: 미보유 시 0, 보유 시 `stock_full_weight * 0.90`
- `tactical_unit_weight`: `stock_full_weight * 0.10`
- `tactical_units`: 0 또는 1
- `desired_total_weight`: 0, `stock_full_weight * 0.90`, `stock_full_weight` 중 하나
- `actual_quantity`, `average_cost`, `market_value`, `actual_weight`
- `tactical_sell_lock`: tactical sell 후 다음 tactical buy까지 `true`

새 Core 포지션은 해당 종목 FULL의 90% 목표로 시작하며 `tactical_units=0`, `tactical_sell_lock=false`로 초기화한다. Tactical GC 매수로 FULL의 100% 목표가 되고, 이후 DC 매도로 다시 FULL의 90% 목표가 된다. Tactical 수량이 없는 상태의 DC는 Core를 줄이지 않으며 `NO_TACTICAL_POSITION`으로 거절한다. Full exit 완료 시 Core·Tactical과 sell lock을 모두 초기화한다.

## 8. 일봉 전략 요구사항

### 8.1 공통 처리

1. T 일봉 종료 후 지표를 계산한다.
2. 신호의 생성 시각은 T 종가 확정 시각, 가용 시각은 그 이후로 기록한다.
3. T에서 만든 Core ENTER는 T+1 거래일에만 활성화한다. FULL_EXIT는 T+1부터 완료 시까지 유지한다.
4. 지표가 부족하거나 추세 상태가 `NEUTRAL`이면 신규 진입 신호를 만들지 않는다. `MIXED`는 유효 박스 구조가 있을 때만 박스 진입을 평가한다.

### 8.2 하락 추세

- 돌파일 T를 제외한 직전 10거래일 T-10~T-1 각각에서 `signal_close < SMA10`이어야 하고, T에서 `signal_close > SMA10`이어야 한다. 결측 거래일이나 SMA10 미산출일은 조건을 충족한 것으로 세지 않는다.
- 일간 수익률 5% 이상 10% 이하이면 기본 후보로 판정한다.
- 5% 미만은 최근 3거래일 각각 `signal_close > signal_open`이고 동시에 `signal_close[t] > signal_close[t-1] > signal_close[t-2]`인 적삼병일 때만 후보가 된다.
- 10% 초과 급등일에는 즉시 진입하지 않고 `SurgePullbackSetup`을 만든다. 급등일 다음 거래일부터 10번째 거래일까지를 유효 구간으로 하며, 이 기간에 `signal_low` 또는 `signal_close`가 당일 SMA10 ±3% 영역에 진입하면 후보를 재활성화한다. 10번째 거래일 종료까지 미충족이면 setup을 `EXPIRED`로 폐기한다.
- MA20 5일 기울기가 -5% 이하이면 진입을 차단한다.
- 고가가 MA20 또는 MA60 ±3%에 접근했으나 해당 이동평균 아래에서 종가가 끝나면 진입을 차단한다.
- 보유 중 일봉 종가가 MA10 아래이면 full exit pending을 만든다.

재활성화일에도 MA20 급하락과 MA20/MA60 저항 필터를 다시 평가한다. Setup은 원 급등일, 활성 시작일, 10번째 거래일인 만료일, 충족일 및 종료 사유를 기록한다. 같은 종목에서 새 급등 setup이 생기면 가장 최근 setup으로 교체하고 이전 setup은 `SUPERSEDED`로 남긴다.

### 8.3 혼합 기울기와 박스 구조

- `DailyTrendState=MIXED`이고 별도 `BoxStructureState=VALID`여야 한다.
- 피벗은 좌측 2개와 우측 2개의 완성 봉으로 확인하며, t의 피벗은 t+2 봉 종료 후 `confirmed_at`부터만 사용할 수 있다.
- 후보 탐색은 T를 포함한 최근 60거래일로 제한하고 7.1절의 deterministic 선택 알고리즘을 사용한다.
- `signal_low`가 확정 피벗 저점 ±5%이고 일간 수익률이 5~10%이면 매수 후보가 된다.
- `signal_close`가 기준 피벗 저점 아래면 full exit pending을 만든다.
- `signal_high`가 기준 피벗 고점 ±5% 영역에 도달하면 전량익절 `FULL_EXIT`를 만든다.

피벗 후보는 미래 두 봉을 보므로 지표 계산 결과의 `confirmed_at`을 t+2로 저장해야 한다. t 시점으로 소급해 매수시키면 안 된다.

### 8.4 상승 추세

- MA20 ±3% 접근 구간에서 Core 매수 후보를 만든다.
- Core 신규 진입 목표는 `stock_full_weight * 0.90`이다. 가격이 MA10 이상이면 일봉 신호만으로 해당 종목 FULL의 100%까지 추격하지 않으며, 나머지 `stock_full_weight * 0.10`은 이후 새 Tactical GC가 발생할 때만 추가한다.
- 보유 중 종가가 MA20 아래이면 full exit pending을 만든다.

## 9. 5분봉 실행 요구사항

### 9.1 Core 진입

1. Pending entry는 T+1 정규장에 `ARMED`가 된다.
2. 첫 번째 완성 5분봉에서 MA20 > MA60이면 Core 매수 실행 신호를 만든다.
3. 그렇지 않으면 이후 새 MA20/MA60 골든크로스를 기다린다.
4. 조건이 확정된 5분봉의 다음 **당일 정규장** 5분봉 `raw_open`에 `core_target_weight = stock_full_weight * 0.90`인 Core 주문을 체결한다.
5. 당일 다음 봉이 없거나 T+1 정규장 안에 체결되지 않으면 주문과 pending entry를 `EXPIRED`로 기록하며 이월하지 않는다.

골든크로스는 `MA20_prev <= MA60_prev`이고 `MA20_now > MA60_now`인 경우로 정의한다.

### 9.2 Core full exit

1. T 일봉에서 생성된 full exit는 T+1에 최우선으로 활성화하고, Strategy V1 기본 `ExitPolicy=C`를 적용한다.
2. T+1 첫 번째 완성 5분봉부터 `signal_close < signal_SMA60`이 최초 확정되는 시점을 찾는다.
3. 해당 봉 다음 거래 가능한 5분봉의 `raw_open`에 Core와 Tactical을 모두 전량 매도한다.
4. 신호가 마지막 봉에서 확정되거나 예정 봉이 체결 불가이면 full exit 주문과 pending 상태를 다음 거래 가능한 시점까지 이월한다.
5. 청산 완료 전에는 자동 만료하지 않으며 tactical buy를 허용하지 않는다.

### 9.3 Tactical

- 보유 중에만 작동한다.
- 1 unit은 현재 보유량이나 전체 포트폴리오의 10%가 아니라 `stock_full_weight * 0.10`이다.
- `tactical_units=0`에서 새 MA20/MA60 골든크로스가 발생하면 +1 unit을 요청하여 해당 종목 FULL의 90%에서 100% 목표로 조절한다.
- `tactical_units=1`에서 새 MA10/MA20 데드크로스가 발생하면 -1 unit을 요청하여 해당 종목 FULL의 100%에서 90% 목표로 조절한다.
- 데드크로스는 `MA10_prev >= MA20_prev`이고 `MA10_now < MA20_now`다.
- Tactical 매도 후 `tactical_sell_lock=true`로 설정하며, 이후 tactical 매도는 거절한다.
- 다음 tactical buy 체결 시 sell lock을 해제한다.
- Tactical은 Core를 침범하지 않으며 총 목표 비중은 `stock_full_weight * 0.90` 또는 `stock_full_weight`다.
- Core 신규 진입 또는 full exit 체결 시 tactical 상태를 초기화한다.
- Tactical 실행신호와 미체결 주문은 장 종료 시 모두 `EXPIRED`로 처리하고 다음 거래일로 이월하지 않는다.
- Tactical 개별 주문이 체결 불가이면 해당 주문은 자동 재시도하지 않으며, 이후 발생한 별도의 신규 교차 신호만 새 주문을 만들 수 있다.

### 9.4 동일 시각 우선순위

동일 종목·동일 이벤트 시각에 여러 조건이 발생하면 다음 순서로 처리한다.

1. Pending daily full exit
2. 진입 차단 필터
3. 일봉 추세 상태
4. 일봉 신호
5. Core 목표
6. 다음 날 5분봉 Core 실행
7. Tactical 조절

상위 규칙이 전량 청산 또는 진입 차단을 결정하면 충돌하는 하위 주문은 `REJECTED_BY_PRIORITY`로 기록한다. 동일 시각 Core와 Tactical 신호가 같은 방향이면 Core만 처리하고 중복 Tactical 주문은 생성하지 않는다. 감사 원장에는 Tactical 신호를 `SUPPRESSED_BY_CORE`로 기록한다.

## 10. 이벤트와 체결 모델

### 10.1 이벤트 순서

엔진은 각 wall-clock timestamp에서 `EventKey` 순서로 이벤트를 처리한다. 거래일 시작 시 전일 pending action을 활성화한 뒤, 각 5분봉 경계의 표준 순서는 다음과 같다.

1. `CORPORATE_ACTION`: 현재 시각에 효력이 발생하는 향후 `CorporateActionEvent` hook
2. `PREVIOUS_BAR_CLOSE_AVAILABLE`: bar N 종료 및 `signal_available_at` 도달 확인
3. `SIGNAL_EVALUATION`: bar N의 수정주가 지표와 신호 평가
4. `ORDER_CREATED_OR_SCHEDULED`: 신호 원장 기록, 주문 생성 및 bar N+1 identity 예약
5. 향후 같은 `eligible_at` batch의 현금 배분 phase
6. `NEXT_BAR_OPEN_FILL`: 예약된 bar N+1의 `raw_open`으로 체결 시도
7. 정규장 종료 시 action별 잔여 신호·주문 만료 또는 이월
8. 일봉 확정, 다음 거래일 pending action 생성 및 raw 종가 평가 원장 저장

연속된 bar N과 N+1의 경계에서는 2~6단계가 같은 datetime일 수 있으나 event phase가 순서를 결정한다. Signal source와 fill target의 bar identity도 함께 검증하므로 phase 순서만 조작해 같은 bar에서 체결할 수 없다.

`CorporateActionEvent`는 같은 `effective_at`의 주문 체결, 포지션 평가 및 전략 실행보다 먼저 적용한다. 이 hook은 기존 보유수량과 현금 장부를 조정하기 위한 것으로, 수정주가 신호 계열을 다시 조정하는 단계가 아니다. 향후 processor는 이벤트 적용 전·후 수량과 현금, 반올림·단주 결과를 별도 원장에 남겨야 한다.

### 10.2 주문 및 수량

- 기본 주문 유형은 다음 5분봉 `raw_open`의 시장가 체결 가정이다. ExitPolicy A만 T+1 일봉의 실제 `raw_open`을 사용한다.
- Intraday 주문의 `eligible_at`은 wall-clock 시각만으로 체결 대상을 정하지 않는다. Scheduler가 선택한 `eligible_bar_id`와 `eligible_bar_sequence`가 authoritative하며 `eligible_at`은 해당 bar의 `bar_start_at`을 중복 기록한 값이다.
- `target_weight`는 항상 전체 포트폴리오 단위다. `stock_full_amount = portfolio_equity_at_decision * stock_full_weight`, `core_target_amount = stock_full_amount * core_fraction_of_full`, `tactical_unit_amount = stock_full_amount * tactical_unit_fraction_of_full`로 계산한다.
- 신규 Core 주문 요청금액은 미보유 상태에서 `core_target_amount`이며, Tactical 주문 요청금액은 `tactical_unit_amount`다. 어떤 경우에도 `core_fraction_of_full=0.90`을 직접 포트폴리오 weight로 사용하지 않는다.
- 단일 주문의 주식 수량은 비용과 현금을 고려하여 정수 주로 내림한다.
- 매도 수량은 실제 보유 수량을 초과할 수 없다.
- 거래정지, 거래량 0, `raw_open` 누락 또는 실제 체결 가능한 가격 부재 시 임의 체결가를 생성하지 않고 `UNFILLED` 또는 `REJECTED` 사유를 기록한다.

동일 `eligible_at`의 복수 종목 신규 Core 주문을 하나의 batch로 처리한다. 각 요청금액은 종목별 `portfolio_equity_at_decision * stock_full_weight * core_fraction_of_full`이다. 가용 현금이 총 요청금액보다 작으면 다음 규칙으로 비례 배분한다.

같은 batch의 모든 종목은 corporate action 적용 이후, 주문 체결 직전의 동일한 `portfolio_equity_at_decision` snapshot을 사용한다.

1. 종목별 요청금액 비율로 가용 현금의 이상적 배정액을 계산한다.
2. 각 배정액 안에서 예상 비용을 포함해 살 수 있는 정수 주를 내림하여 1차 수량을 결정한다.
3. 1차 배정 뒤 잔여현금은 `이상적 주식 수량 - 1차 정수 수량`의 소수부가 큰 종목부터 한 주씩 추가 배정한다.
4. 소수부가 같으면 6자리 `stock_code` 오름차순으로 tie-break한다.
5. 요청 수량을 넘거나 비용 포함 한 주를 살 수 없으면 건너뛰며, 더 배정 가능한 종목이 없을 때 종료한다.

따라서 입력 행 순서나 이벤트 도착 순서가 현금 배분을 결정하지 않는다. 배분 계산의 Decimal 정밀도와 비용 반올림 규칙은 `ExecutionConfig` 및 cost table에 명시한다.

### 10.3 비용 모델

비용은 전략 코드와 분리된 effective-dated 설정 테이블로 관리한다. 각 행은 최소 `effective_from`, `effective_to`, `market`, `buy_commission_rate`, `sell_commission_rate`, `sell_tax_rate`, `slippage_model`, `slippage_value`, `minimum_fee`, `rounding_rule`을 가진다. `effective_to`는 exclusive boundary로 정의한다.

엔진은 다음 두 실행 profile을 모두 지원한다.

- `ZERO_COST`: 모든 수수료·세금·슬리피지를 0으로 두는 구조 검증용 실행
- `CONFIGURED_COST`: 거래일·시장에 맞는 설정 테이블 행을 적용하는 현실 비용 실행

비용 요소는 다음과 같이 분리한다.

- 매수 수수료율
- 매도 수수료율
- 매도 세금률과 시장별 차이
- 고정 또는 비율 슬리피지
- 최소 수수료와 반올림 규칙

성과는 비용 전·후를 모두 제공한다. 설정 테이블의 출처·버전·적용 행을 실행 metadata와 fill 원장에 기록한다. 해당 거래일·시장에 유효한 행이 없거나 둘 이상 겹치면 `CONFIGURATION_ERROR`로 중단한다.

### 10.4 체결 불가와 세션 경계

다음 봉이 없거나 체결 가능 가격이 없으면 체결을 만들지 않는다. 이때 action별 정책은 다음과 같다.

Generic scheduler는 먼저 다음 거래 가능한 정규장 bar의 존재 여부만 판단한다. 없으면 `eligible_at`, `eligible_bar_id`, `eligible_bar_sequence`를 비워 두고 scheduling result를 `NO_NEXT_BAR`로 append한다. 이 결과 자체는 expire/carry를 결정하지 않으며 아래 action별 정책 계층이 후속 결정을 내린다.

- Core ENTER: 개별 미체결 주문은 자동 재시도하지 않으며, pending은 당일의 별도 신규 실행 조건만 다시 평가하고 정규장 종료 시 `EXPIRED`
- FULL_EXIT: 실제 전량청산까지 다음 거래 가능한 시점으로 이월
- Tactical: 개별 미체결 주문은 자동 재시도하지 않고 정규장 종료 시 `EXPIRED`

시간외 봉은 전략 시계열과 체결 대상에서 제외한다. FULL_EXIT의 “다음 거래 가능한 시점”은 정규장 안에서 유효한 raw 체결가격이 최초로 제공되는 bar start이며 임의 보간 가격이 아니다.

## 11. 공개 인터페이스 초안

```python
class Strategy(Protocol):
    def on_daily_close(self, context: DailyContext) -> list[StrategyIntent]: ...
    def on_intraday_close(self, context: IntradayContext) -> list[StrategyIntent]: ...

class ExecutionModel(Protocol):
    def schedule(self, intent: StrategyIntent, context: MarketContext) -> Order: ...
    def try_fill(self, order: Order, bar: Bar) -> FillResult: ...

class CorporateActionProcessor(Protocol):
    def apply(self, event: CorporateActionEvent, portfolio: PortfolioState) -> AccountingResult: ...

class BacktestEngine:
    def run(self, request: BacktestRequest) -> BacktestResult: ...
```

`StrategyIntent`는 최소 `stock_code`, `intent_type`, 전체 포트폴리오 단위의 `target_weight` 또는 `unit_delta`, 생성·가용 시각, 사유, 전략 상태를 가진다. `Bar`는 원천 timestamp와 내부 `bar_start_at`, `bar_end_at`, `signal_available_at`, raw/signal OHLCV를 동시에 보존한다. 전략은 현금 잔고나 체결가를 직접 변경하지 않는다. `CorporateActionProcessor`는 전략 인터페이스와 분리하며 Position/Accounting만 변경한다.

## 12. 설정 모델

확정 정책을 코드 상수로 숨기지 않는다. 다음은 `StrategyConfig` 후보 필드와 V1 기본값이다.

| 후보 필드 | V1 값 |
|---|---|
| `strategy_version` | `V1` |
| `signal_price_basis` | `ADJUSTED` |
| `daily_ma_periods`, `intraday_ma_periods` | `(10, 20, 60)` |
| `slope_lookback_sessions` | `5` |
| `downtrend_below_ma10_sessions` | `10` |
| `reversal_return_min_pct`, `reversal_return_max_pct` | `5`, `10` |
| `red_three_soldiers_sessions` | `3` |
| `surge_threshold_pct` | `10` 초과 |
| `surge_pullback_valid_sessions` | 급등일 다음 날부터 `10`거래일 |
| `surge_pullback_ma10_band_pct` | `3` |
| `ma20_steep_decline_pct` | `-5` 이하 |
| `resistance_band_pct` | `3` |
| `pivot_left_bars`, `pivot_right_bars` | `2`, `2` |
| `pivot_lookback_sessions` | `60` |
| `box_min_width_pct` | `25` |
| `box_pivot_band_pct` | `5` |
| `box_high_exit_fraction` | `1.0` |
| `uptrend_ma20_band_pct` | `3` |
| `core_fraction_of_full` | `0.90` |
| `tactical_unit_fraction_of_full` | `0.10` |
| `max_tactical_units` | `1` |
| `exit_policy` | `C` |

다음은 `ExecutionConfig` 후보 필드와 V1 기본값이다.

| 후보 필드 | V1 값 |
|---|---|
| `timezone` | `Asia/Seoul` |
| `regular_session_start`, `regular_session_end` | `09:00`, `15:30` |
| `include_after_hours` | `false` |
| `source_timestamp_semantics` | adapter가 `START`/`END` 중 하나를 필수 제공; 추정값 금지 |
| `execution_price_basis` | `RAW` |
| `entry_activation_sessions` | `1` (T+1 당일만) |
| `entry_last_bar_policy` | `EXPIRE` |
| `full_exit_last_bar_policy` | `CARRY_TO_NEXT_TRADABLE` |
| `tactical_session_end_policy` | `EXPIRE` |
| `non_tradable_price_policy` | `NO_SYNTHETIC_FILL` |
| `non_exit_unfilled_order_policy` | `NO_AUTOMATIC_RETRY` |
| `same_direction_core_tactical_policy` | `CORE_ONLY` |
| `cash_shortage_allocation` | `PRO_RATA_REQUESTED_AMOUNT` |
| `share_rounding` | `FLOOR` |
| `cash_remainder_method` | `LARGEST_FRACTIONAL_REMAINDER` |
| `cash_remainder_tie_break` | `STOCK_CODE_ASC` |
| `currency`, `decimal_precision` | `KRW`, `28` |
| `cost_profile` | `ZERO_COST` 또는 `CONFIGURED_COST` |
| `corporate_action_data_policy` | 실제 장기 실행은 `REQUIRE_COMPLETE`; synthetic fixture는 명시적으로 `ALLOW_NONE_FOR_FIXTURE` 가능 |

실행 요청에는 이와 별도로 백테스트 시작일·종료일, 초기 자본, 대상 시장·종목, 종목별 `stock_full_weight`, 데이터 버전, stock master 버전, cost table 버전, corporate-action dataset 및 coverage 버전이 필요하다. `stock_full_weight`는 전체 포트폴리오 대비 종목별 최대 weight이며 `[0, 1]` 범위다. 확률 체결 모델을 향후 추가한다면 seed도 명시해야 하지만 V1 체결 모델 자체는 확률을 사용하지 않는다.

실행 결과에는 설정 전체, 실제 선택된 cost table 행, 가격 조정 metadata, 입력 파일 SHA-256 또는 데이터셋 버전을 보존한다.

## 13. 원장과 출력 스키마

모든 원장은 append-only event log다. 기존 row의 status나 timestamp를 update하지 않고 상태 전이마다 `ledger_sequence`, `event_key`, `previous_status`, `status`를 가진 새 row를 추가한다. 동일 입력에서 `event_key` 정렬 후 `ledger_sequence`가 같아야 한다. 조회용 현재 상태는 마지막 유효 transition을 projection하여 얻는다.

### 13.1 Signal ledger

필수 필드:

- `signal_id`, `stock_code`
- `signal_type`, `side`
- `signal_generated_at`
- `signal_available_at`
- `execution_signal_at`
- `executed_at`
- `signal_event_key`
- `signal_source_bar_id`, `signal_source_bar_sequence`
- `bar_start_at`, `bar_end_at`
- `reason`
- `strategy_state`, `box_structure_state`
- `indicator_snapshot`, `signal_price_snapshot`
- `status`, `rejection_reason`

`signal_generated_at`은 신호 계산의 원인이 된 bar가 종료된 wall-clock 시각, `signal_available_at`은 신호를 알 수 있는 최초 wall-clock 시각, `execution_signal_at`은 실제 주문으로 이어질 실행 조건이 확정된 wall-clock 시각이다. 세 값은 역할이 다르며 우연히 같더라도 하나의 필드로 합치지 않는다. Wall-clock 관계는 `signal_generated_at <= signal_available_at <= execution_signal_at <= executed_at`이고 각 equality를 허용한다.

`signal_event_key`는 해당 주문의 원인이 된 actionable `SIGNAL_EVALUATION` transition의 key다. 일봉 신호와 이후 5분봉 실행 조건처럼 단계가 분리되면 각각 별도 append row와 event key를 가지며, 주문은 자신을 직접 발생시킨 실행 신호 row를 참조한다.

`executed_at`은 fill transition이 생기기 전에는 비어 있다. 체결 transition에서는 `executed_at >= signal_available_at`, `fill_event_key > signal_event_key`, `fill_bar_sequence > signal_source_bar_sequence`를 모두 만족해야 한다.

### 13.2 Order/Fill ledger

- 주문: `order_id`, `signal_id`, `batch_id`, `created_at`, `created_event_key`, `eligible_at`, `eligible_bar_id`, `eligible_bar_sequence`, `side`, `requested_amount`, `allocated_amount`, `requested_quantity`, `status`, `reason`
- 체결: `fill_id`, `order_id`, `filled_at`, `fill_event_key`, `fill_bar_id`, `fill_bar_sequence`, `raw_price`, `slippage`, `fill_price`, `quantity`, `commission`, `tax`, `cost_config_id`

`created_at`은 주문 row가 생성된 wall-clock 시각이고 `eligible_at`은 선택된 대상 bar의 시작 시각이다. 두 값이 같을 수 있으므로 `created_event_key < fill_event_key`와 bar sequence를 함께 검증한다. `NO_NEXT_BAR` scheduling row에는 eligible 관련 세 필드가 비어 있어야 하며 Fill row를 만들 수 없다.

### 13.3 Position/Equity ledger

- 종목별: 거래일, `stock_full_weight`, `core_fraction_of_full`, 전체 포트폴리오 단위 `core_target_weight`, `tactical_unit_weight`, tactical units 0/1, `desired_total_weight`, `actual_weight`, 실제 수량, 평균단가, raw 평가액, 실현·미실현 손익, sell lock
- 포트폴리오별: 현금, 총자산, gross/net exposure, 일별 수익률, 누적 비용, drawdown

### 13.4 Trade ledger

진입부터 청산까지의 round trip을 구성하고 진입·청산 사유, 보유 기간, 최대 유리/불리 변동, 비용 전·후 손익을 기록한다. 부분 체결과 Tactical 거래는 fill 원장을 기준으로 재구성 가능해야 한다.

### 13.5 Corporate action ledger

향후 processor가 활성화되면 최소 `event_id`, `stock_code`, `effective_at`, `action_type`, `quantity_factor`, `cash_adjustment`, `quantity_before`, `quantity_after`, `cash_before`, `cash_after`, `source`, `status`를 append-only로 기록한다. 원천 event와 회계 반영 결과를 모두 역추적할 수 있어야 한다.

## 14. 검증 규칙과 불변조건

실행 전 검증:

- 종목코드가 6자리 문자열이며 master 유효기간 안에 존재한다.
- 모든 `stock_full_weight`가 `[0, 1]`이고 Core/Tactical의 portfolio weight 변환식이 4.1절과 일치한다.
- OHLCV 키가 종목·timestamp별 유일하고 오름차순이다.
- 일봉과 5분봉의 거래일 및 timezone이 캘린더와 일치한다.
- Source adapter의 timestamp 의미가 명시되어 있고 내부 세 시간 필드가 `bar_start_at < bar_end_at <= signal_available_at`을 만족한다.
- V1에 공급되는 5분봉이 KRX 정규장 범위에 있고 시간외 데이터가 제외되어 있다.
- 같은 종목·시각의 raw/signal OHLCV가 일대일 정렬되고 두 계열의 metadata가 존재한다.
- 미래 봉을 참조하는 파생값에는 실제 `available_at`이 지정된다.
- 필요한 warm-up 구간이 시작일 전에 확보되어 있다.
- Cost table의 날짜·시장 구간이 겹치지 않고 `CONFIGURED_COST` 실행 전 기간을 덮는다.
- `CorporateActionEvent`의 종목·유효시각·출처가 유효하고 `quantity_factor > 0`이며 동일 원천 event가 중복되지 않는다.
- `REQUIRE_COMPLETE` 실행은 대상 기간과 universe를 덮는 corporate-action coverage 선언이 있어야 한다. 알려진 action이 누락되었거나 coverage를 입증할 수 없으면 `MISSING_CORPORATE_ACTION_DATA`로 실행을 차단한다.

실행 중 불변조건:

- 현금과 수량은 허용 정책 없이는 음수가 되지 않는다.
- 종목별 목표 weight는 해당 `stock_full_weight`를 초과하지 않는다.
- 보유 목표는 `stock_full_weight * 0.90`과 Tactical 0/1 unit에 의해 `stock_full_weight * 0.90` 또는 `stock_full_weight`만 가진다.
- 보유량보다 많은 매도 체결은 없다.
- Wall-clock 기준 `executed_at >= signal_available_at`이다. 연속 봉 경계의 equality는 정상이다.
- 전체 wall-clock chain은 `signal_generated_at <= signal_available_at <= execution_signal_at <= executed_at`이며 같은 경계에서 equality를 허용한다.
- 인과관계 기준 `fill_event_key > signal_event_key`이고 `created_event_key < fill_event_key`다.
- Bar identity 기준 `fill_bar_sequence > signal_source_bar_sequence`이며 `fill_bar_id != signal_source_bar_id`다.
- 5분봉 종가 신호가 source bar의 시가 또는 동일 source bar 안에서 체결되지 않는다.
- `eligible_at`만으로 다음 봉을 판정하지 않으며 order의 `eligible_bar_id`/`eligible_bar_sequence`와 실제 fill bar가 일치한다.
- `NO_NEXT_BAR` scheduling result에서 합성 Fill row를 만들지 않는다.
- 같은 timestamp·phase의 event는 canonical tie-break로 정렬하며 입력 행 순서가 ledger 결과에 영향을 주지 않는다.
- 피벗은 확인 시각 이전에 사용되지 않는다.
- 신호·지표는 signal 가격, 체결·현금 회계는 raw 가격만 사용한다.
- 장 종료 뒤 Core ENTER 또는 Tactical 주문이 남아 있지 않는다.
- FULL_EXIT pending은 청산 완료 전에 만료되지 않는다.
- full exit pending 중 tactical buy 체결이 없다.
- tactical sell lock 중 반복 tactical sell 체결이 없다.
- 같은 시각의 corporate action은 주문 체결과 평가보다 먼저 적용되고, 적용 결과가 corporate action ledger에 기록된다.

위반 시 자동 보정하지 않고 실행을 실패시키거나 해당 이벤트를 명시적으로 거절한다.

## 15. 성과 지표

최소 산출 지표:

- 누적 수익률, CAGR
- 최대 낙폭(MDD)과 회복 기간
- 승률, 평균 이익, 평균 손실, 손익비
- Profit Factor
- 거래 수와 평균 보유 기간
- 회전율
- 수수료·세금·슬리피지 총액과 비용 차감 전후 성과
- 월별·연도별 수익률
- 종목별·추세 상태별·박스 구조별·진입 사유별 성과
- 노출 비중과 현금 비중

매도 방식 A/B/C는 동일 입력·진입 신호·비용 설정으로 각각 실행한다. 기준안 C에서는 일봉 청산 신호 시점의 가상 즉시 청산 가격과 실제 5분봉 지연 청산 가격을 함께 기록하여 `exit_delay_incremental_pnl`을 계산한다.

## 16. 테스트 요구사항

### 16.1 단위 테스트

- SMA 및 5일 기울기 계산과 데이터 부족 처리
- `UP/DOWN/MIXED/NEUTRAL` 추세와 `BoxStructure VALID/INVALID`의 독립 판정
- T-10~T-1 모두 `Close < MA10`, T에서 `Close > MA10`인 정확한 돌파 및 결측일 실패
- 수익률 5%, 10% 경계와 최근 3일 모두 양봉이면서 종가가 연속 상승하는 적삼병
- 10% 초과 setup의 T+1~T+10 포함 범위, 눌림 재활성화, 만료 및 신규 setup 대체
- MA20 -5%, 이동평균 ±3% 필터 경계
- t+2 이전 피벗 사용 금지, 60거래일 lookback, 최근 피벗 deterministic 선택 및 박스 폭 25% 경계
- 박스 고점 ±5% 도달 시 전량익절 생성
- 일봉 T Core ENTER의 T+1 활성화와 당일 만료, FULL_EXIT의 완료 시까지 유지
- 첫 5분봉 조건과 신규 골든/데드크로스 판정
- 다음 봉 시가 체결 및 같은 봉 체결 금지
- `signal_available_at == next_bar.bar_start_at`이고 `executed_at == signal_available_at`인 연속 봉 경계의 정상 체결
- 위 equality 사례에서 `fill_event_key > signal_event_key`와 `fill_bar_sequence > signal_source_bar_sequence`를 동시에 만족
- source bar와 fill bar가 같은 fixture는 event phase가 뒤여도 validation 실패
- `eligible_at`이 같아도 `eligible_bar_id`/`eligible_bar_sequence`가 다르면 잘못된 fill을 거부
- 마지막 봉의 scheduler가 `NO_NEXT_BAR`를 기록하고 synthetic fill을 만들지 않음
- 서로 다른 `stock_full_weight`에서 Core/Tactical을 portfolio weight로 변환하고 FULL 내부 목표 90%↔100%를 유지하는지 검증
- 동일 시각 같은 방향 Core 우선 및 Tactical 중복 주문 억제
- full exit 우선순위와 tactical buy 차단
- 장 마지막 봉의 ENTER 만료, FULL_EXIT 이월, Tactical 만료
- 거래정지·거래량 0·raw 시가 누락에서 합성 체결 금지
- Core ENTER/Tactical 미체결 주문의 자동 재시도 금지와 별도 신규 신호 처리
- START/END timestamp adapter 변환, 가용 시각 검증 및 시간외 제외
- 수정주가 신호와 raw 체결·회계 계열의 분리
- 비례 현금 배분, 정수 주 내림, 최대 소수 잔여법과 stock code tie-break
- 기간·시장별 비용 행 선택, 겹침 오류, zero/configured cost 실행
- ExitPolicy A/B/C의 정확한 시그널·체결 시점
- corporate action hook이 같은 시각의 주문·평가보다 먼저 호출되는지 검증
- `REQUIRE_COMPLETE`에서 event/coverage 누락 시 실행 차단, fixture opt-out은 명시적일 때만 허용하는지 검증

### 16.2 통합 시나리오 테스트

- 일봉 진입 → 다음 날 첫 5분봉 Core 체결 → Tactical 증감 → 일봉 청산 → 다음 날 전량 청산
- 첫 봉 조건 불충족 후 장중 골든크로스 진입
- 거래정지·5분봉 누락·마지막 봉에서 action별 만료 또는 이월
- 피벗 확정 전후 동일 데이터에서 룩어헤드가 없는지 비교
- `MIXED+INVALID`에서 박스 진입이 없고 `MIXED+VALID`에서만 진입하는지 검증
- 복수 종목 동시 신호, 현금 부족 비례 배분 및 입력 순서 독립성
- 같은 timestamp에 여러 종목·신호·주문이 있을 때 canonical tie-break로 원장 순서와 결과가 동일함
- FULL이 서로 다른 복수 종목의 Core 요청금액이 각 `equity * stock_full_weight * 0.90`으로 계산되는지 검증
- 매도 A/B/C가 진입 구간은 같고 청산 시점만 각 계약대로 다른지 검증
- C가 T+1에 조건 미발생 시 다음 거래일에도 pending을 유지하고 최초 조건에서 청산하는지 검증
- `ZERO_COST`와 동일 거래의 `CONFIGURED_COST` 결과 및 비용 차이 검증

### 16.3 회귀·속성 테스트

- 고정 fixture의 signal/order/fill/equity golden file 비교
- 입력 행 순서를 바꿔도 정렬 후 결과 동일
- 동일 입력 반복 시 event key와 append-only signal/order/fill transition 원장이 동일
- 미래 데이터를 제거해도 제거 시점 이전 결과 동일
- raw 가격만 변경하면 신호는 같고 체결·회계만 달라지며, signal 가격만 변경하면 그 반대가 되는지 검증
- 모든 fill이 기존 order와 signal로 역추적 가능
- 모든 fill에서 `fill_bar_sequence > signal_source_bar_sequence`이고 같은 source bar 체결이 없음
- 포트폴리오 회계식 `이전 자산 + 손익 - 비용 = 현재 자산` 검증

네트워크 없이 전체 테스트가 가능해야 하며, 실제 데이터 검증은 별도의 선택적 integration test로 분리한다.

## 17. 매도 방식 비교 설계

공통 `ExitPolicy` 인터페이스 아래 실행 시점을 다음과 같이 확정한다.

- `A`: T 일봉 full exit 확정 후 T+1 정규장 시가의 `raw_open`에 전량청산한다.
- `B`: T+1 첫 5분봉이 완료되고 `signal_available_at`에 도달한 뒤, 그 다음 5분봉 시가의 `raw_open`에 전량청산한다.
- `C`: T+1 이후 최초 `5분 signal_close < signal_SMA60`이 확정된 뒤, 다음 거래 가능한 5분봉 시가의 `raw_open`에 전량청산한다.

Strategy V1 기본값은 C다. A/B/C 모두 full exit이므로 예정 시점에 체결 가능한 raw 가격이 없으면 임의 가격을 생성하지 않고 다음 정규장의 거래 가능한 시점으로 이월한다. C는 T+1에 조건이 없더라도 pending을 만료하지 않고 이후 거래일의 연속 5분봉에서 최초 조건을 계속 탐색한다.

## 18. 수용 기준

MVP는 다음 조건을 모두 만족해야 한다.

1. 단일 종목 fixture에서 전략 상태, Core/Tactical 주문 및 포지션을 끝까지 재현한다.
2. 일봉 T의 Core ENTER 신호가 T 또는 T+1 첫 5분봉 시가에 소급 체결되지 않는다.
3. 모든 신호에 필수 시간 필드와 사유·상태가 기록된다.
4. 종목별 `stock_full_weight`를 기준으로 FULL 내부 Core 90%와 Tactical 0/1 unit을 전체 포트폴리오 weight로 변환하고, action별 만료·이월 및 우선순위 불변조건을 위반하지 않는다.
5. 비용 전후 equity curve와 필수 성과 지표를 생성한다.
6. 동일 실행을 반복하면 byte-level 또는 허용 오차 내 동일 결과가 나온다.
7. 잘못된 입력은 자동 보정 없이 명확한 validation error를 낸다.
8. 전략 테스트와 범용 엔진 테스트가 분리되어 있다.
9. 신호 계산에는 수정주가만, 체결·현금 회계에는 raw 가격만 사용했음을 원장과 metadata로 입증할 수 있다.
10. ExitPolicy A/B/C와 `ZERO_COST`/`CONFIGURED_COST`를 동일 입력에서 독립 재현할 수 있다.
11. 실제 장기 데이터 실행에서 corporate-action coverage가 불완전하면 조용히 계속하지 않고 품질 gate로 차단한다.
12. 연속 봉 경계에서 `executed_at == signal_available_at`인 다음 봉 체결을 허용하되 `fill_event_key > signal_event_key`와 `fill_bar_sequence > signal_source_bar_sequence`를 입증한다.
13. 동일 timestamp의 복수 종목·이벤트 입력 순서를 바꿔도 canonical event key 정렬 후 원장 결과가 동일하다.
14. 같은 source bar 체결과 `NO_NEXT_BAR` synthetic fill은 validation error 또는 명시적 거절로 차단된다.

## 19. Strategy V1 구현 계약

기존의 구현 전 결정사항은 다음과 같이 확정한다. 상세 산식과 상태 전이는 앞 절을 따르며, 해당 값은 `StrategyConfig`, `ExecutionConfig`, source adapter metadata 또는 effective-dated cost table에 명시한다.

1. 신호·지표는 adjusted `signal_ohlcv`, 체결·현금 회계는 `raw_ohlcv`를 사용한다.
2. Source adapter가 timestamp의 `START`/`END` 의미를 선언하고 내부 세 시각으로 변환한다. V1은 KRX 정규장 09:00~15:30만 사용한다.
3. 장 마지막 실행신호는 Core ENTER 만료, FULL_EXIT 이월, Tactical 만료로 처리한다.
4. 거래정지, 거래량 0, 시가 누락 또는 실제 체결가 부재 시 합성 가격 없이 `UNFILLED`/`REJECTED`로 기록한다.
5. 10% 초과 급등 setup은 다음 거래일부터 10거래일 동안 MA10 ±3% 눌림을 기다린다.
6. 하락 반전은 T-10~T-1의 모든 종가가 각 MA10 아래이고 T 종가가 MA10 위인 경우다.
7. Pivot은 좌2/우2 확정 방식, 최근 60거래일, `confirmed_at` 제한과 7.1절의 최근 High/Low 선택법을 사용한다.
8. 박스 기준 고점 ±5% 도달은 100% full exit다.
9. FULL은 종목별 `stock_full_weight`다. `core_target_weight = stock_full_weight * 0.90`, `tactical_unit_weight = stock_full_weight * 0.10`이고 목표 보유는 해당 종목 FULL 내부에서 90%↔100%다.
10. 동일 시각·같은 방향 Core/Tactical 충돌에서는 Core만 주문한다.
11. Core ENTER는 T+1 정규장만 유효하고, FULL_EXIT는 청산 완료까지 유지한다.
12. 동일 batch 현금 부족은 요청금액 비례 배분 후 최대 소수 잔여법과 stock code 오름차순 tie-break를 적용한다.
13. 비용은 기간·시장별 설정 테이블로 분리하며 zero-cost와 configured-cost 실행을 모두 지원한다.
14. ExitPolicy는 A=T+1 시가, B=T+1 첫 5분봉 완료 후 다음 시가, C=T+1 이후 최초 5분 종가<MA60 후 다음 시가이며 V1 기본은 C다.

적삼병은 최근 3거래일 모두 `Close > Open`이면서 `Close[t] > Close[t-1] > Close[t-2]`인 경우로 확정한다. 추세 기울기 반대는 `DailyTrendState=MIXED`, 박스 유효성은 별도 `BoxStructureState`로 관리한다.

## 20. 남은 외부 데이터·실행 준비사항

위 Strategy V1의 매매 의미에는 미결정 항목이 없다. 다만 실제 데이터로 실행하기 전 다음 입력과 adapter 사실은 확인해야 한다.

1. 수정주가 제공자의 기업행동·배당·거래량 조정 산식과 데이터 버전
2. 각 일봉·5분봉 원천 timestamp의 공식 START/END 의미와 결측 봉 표현
3. split, reverse split, 감자 등 보유 수량·현금 회계를 바꾸는 corporate-action event 데이터, 적용 시각 및 대상 기간·universe coverage 증명
4. 거래정지 및 실제 체결 가능 여부를 판정할 원천 필드; 확인할 수 없는 봉은 보수적으로 미체결 처리
5. 백테스트 기간·시장별 실제 수수료·세금·슬리피지 설정 행과 근거
6. 실행별 초기 자본, 종목별 `stock_full_weight`, 투자 universe 및 benchmark

이는 전략 규칙의 재해석 항목이 아니라 source adapter, 회계 입력 및 실행 parameter 준비사항이다. 확인되지 않은 값을 엔진이 추정해서는 안 된다. 특히 실제 장기 실행에서 필요한 corporate-action event 또는 coverage가 없으면 `CorporateActionProcessor` hook을 건너뛰고 계속하지 않고 데이터 품질 gate가 실행을 차단한다.

## 21. 권장 구현 순서

1. 표준 OHLCV 모델, 캘린더, validator 및 지표 엔진
2. 이벤트 루프, 시간 가용성 모델, signal/order/fill 원장
3. 비용 없는 단일 종목 Core 진입·청산 MVP
4. Tactical 상태 머신과 우선순위
5. 현금·비용·복수 종목 포트폴리오 회계와 `CorporateActionEvent` hook
6. 매도 A/B/C 정책과 비교 리포트
7. 실제 일봉·5분봉 어댑터 및 데이터 품질 게이트
8. 성과 분석, 회귀 fixture, 실행 manifest

각 단계는 룩어헤드 방지 테스트와 원장 재현 테스트를 통과한 뒤 다음 단계로 진행한다.
