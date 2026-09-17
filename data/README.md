# 실험별 CSV와 채점 결과

한 파일은 모델 하나, 시험 조건 하나의 결과이며 한 행은 호출 한 번이다. 24개 CSV에 153개 호출 기록을 담았다. 이 중 실제 응답 원본은 151건, 채점 완료는 147건이다. 중단 2건과 권장 설정 워밍업 4건은 미채점이다.

## 파일명

`model_trial_YYYYMMDD-HHMMSS.csv` 형식의 영어 이름이다. 날짜와 시간은 한국 표준시 기준이며 임의 ID나 순번을 붙이지 않는다.

예: `qwen3.5-9b_recommended-nonthinking-english_20260916-143543.csv`

`basic`은 기본 설정, `recommended`는 권장 설정, `thinking`은 추론, `nonthinking`은 비추론을 뜻한다. 기본 추론은 `tokens2048` 또는 `tokens4096`으로 생성 한도를 구분한다. `warmup`은 워밍업, `completion-check`는 대표 문항 완료 여부 점검, `rerun`은 별도 재실행이다. 기존 파일의 시각은 첫 호출 시각이며, 새 실행은 시험 시작 시각을 사용한다.

## 열을 읽는 방법

1. `system_instruction`에서 실제 전달한 공통 지시문 확인.
2. `input_` 열에서 시장 자료, 계좌와 이력, 거래 제한과 세 필수 질문 확인.
3. 다섯 출력 필드와 `thinking`에서 모델이 반환한 값과 문장 확인.
4. 요청 설정, 시간, 토큰과 GPU 측정값 확인.
5. `review_1_*`~`review_3_*`에서 기대 설명, 판정, 점수, 인용문과 한국어 근거 확인.

| 원래 입력의 위치 | CSV 열 예시 |
| --- | --- |
| 기준 시각 | `input_as_of` |
| 첫 5분봉 종가 | `input_market_input_ohlcv_minute5_1_close` |
| 첫 시간봉 EMA | `input_market_input_ohlcv_minute60_1_ema` |
| 두 번째 일봉 거래량 | `input_market_input_ohlcv_day_2_volume` |
| 매수호가 | `input_market_input_orderbook_bid_price` |
| 공포탐욕지수 값 | `input_market_input_fear_and_greed_value` |
| 가용 현금 | `input_strategy_state_cash_available_krw` |
| 첫 실제 체결 수량 | `input_strategy_state_executions_1_btc` |
| 수수료율 | `input_trading_rules_fee_rate` |
| 첫 필수 질문 | `input_required_observations_1` |

입력 키의 경로를 밑줄로 연결하며 배열 번호는 1부터 시작한다. 여러 일봉과 체결은 각각의 열에 보존하며 배열 길이는 `*_count`다. Q03의 일봉 1은 앞선 일봉, 일봉 2는 최신 일봉이다. `null`은 입력에 명시된 결측값이고 count 0은 빈 배열이다. 다른 문항에만 존재하는 열의 공란은 그 행에 해당 항목이 없음을 뜻한다.

파일명과 열 이름은 영어이며, 실제 입력과 출력 및 인용문은 번역하지 않는다. **채점 근거와 검토용 기대 설명은 한국어 그대로 유지**한다. `review_1_source_paths` 등의 입력 위치는 원래 JSON 경로이므로 배열 인덱스가 0부터 시작한다. CSV의 배열 번호와 하나 차이가 나는 표기다.

정상 응답은 `decision`, `buy_allocation_percentage`, `sell_allocation_percentage`, `reason`, `reflection_log`로 분리한다. 파싱할 수 없는 JSON은 `unparsed_response`에 원문을 보존한다. `thinking`이 30,000자를 넘으면 같은 행의 `thinking_part_2` 등에 이어 담으며, 전체 내용을 연결해 읽는다. 긴 내용을 별도 행으로 분리하지 않는다.

설정값은 `num_ctx`, `num_predict`, `think`, `temperature`, `top_p` 등이다. 토큰 수는 `input_tokens`, `generated_tokens`, 시간은 `wall_seconds`, `load_seconds`, GPU는 `gpu_peak_mib`, `model_gpu_mib` 등으로 구분한다. 미측정값은 공란이며 0으로 채우지 않는다. `json_valid`와 `trading_valid`는 자동 검사이며 사실 점수가 아니다.

채점은 한 벌의 열에 작성한다. `review_1_verdict`~`review_3_verdict`에 pass/partial/fail, 각 score에 1/0을 기록하고 총점 `score`는 직접 합산한다. 새 호출은 사실 판정과 점수가 공란이다. 기존 파일의 점수, 한국어 근거 및 추가 오류 관찰은 당시 판정을 유지한다. 관리용 ID, 검토자 정보와 별도 재검토 열은 없다. 실제 입력에 있던 가상 주문 ID와 과거 호출 실패 정보는 자료의 일부이므로 보존한다.

CSV는 UTF-8 BOM 형식이다. Excel의 데이터 가져오기를 이용하면 긴 숫자와 시각 및 원문 열을 텍스트로 지정할 수 있다. 셀 안 줄바꿈을 켜고, 쉼표로 직접 나누지 말고 CSV 읽기 기능으로 확인한다. 파일 편집과 채점은 실행 종료 후 진행한다.

## 파일 목록

점수 분모는 실제 채점 기록이 있는 응답의 세 항목이다. 호출 수와 분모가 다른 파일은 중단 여부를 함께 확인한다. 워밍업은 본 시험 점수에 합산하지 않는다.

| 모델 | 조건 | 언어 | 단계 | 시도 수 | 채점 수 | 점수 | 파일 |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| Gemma | 기본 설정 비추론, 생성 2048 | 영문 | 별도 재실행 | 1 | 1 | 3/3 | [CSV](gemma4-12b_basic-nonthinking-english-rerun_20260915-211500.csv) |
| Gemma | 기본 설정 비추론, 생성 2048 | 영문 | 본 시험 | 20 | 20 | 50/60 | [CSV](gemma4-12b_basic-nonthinking-english_20260915-203548.csv) |
| Gemma | 기본 설정 비추론, 생성 2048 | 영문 | 워밍업 | 1 | 1 | 1/3 | [CSV](gemma4-12b_basic-nonthinking-english-warmup_20260915-201750.csv) |
| Gemma | 기본 설정 비추론, 생성 2048 | 한국어 | 본 시험 | 6 | 6 | 6/18 | [CSV](gemma4-12b_basic-nonthinking-korean_20260915-203846.csv) |
| Gemma | 기본 설정 추론, 생성 2048 | 영문 | 본 시험 | 13 | 12 | 0/36 | [CSV](gemma4-12b_basic-thinking-english-tokens2048_20260916-115756.csv) |
| Gemma | 기본 설정 추론, 생성 4096 | 영문 | 본 시험 | 10 | 9 | 0/27 | [CSV](gemma4-12b_basic-thinking-english-tokens4096_20260916-125414.csv) |
| Gemma | 기본 설정 추론, 생성 2048 | 영문 | 워밍업 | 1 | 1 | 0/3 | [CSV](gemma4-12b_basic-thinking-english-tokens2048-warmup_20260916-115321.csv) |
| Gemma | 권장 설정 비추론, 생성 16384 | 영문 | 본 시험 | 10 | 10 | 22/30 | [CSV](gemma4-12b_recommended-nonthinking-english_20260916-143615.csv) |
| Gemma | 권장 설정 비추론, 생성 16384 | 영문 | 워밍업 | 1 | 0 | 미채점 | [CSV](gemma4-12b_recommended-nonthinking-english-warmup_20260916-143511.csv) |
| Gemma | 권장 설정 추론, 생성 16384 | 영문 | 본 시험 | 10 | 10 | 28/30 | [CSV](gemma4-12b_recommended-thinking-english_20260916-145732.csv) |
| Gemma | 권장 설정 추론, 생성 16384 | 영문 | 대표 문항 점검 | 1 | 1 | 2/3 | [CSV](gemma4-12b_recommended-thinking-english-completion-check_20260916-140129.csv) |
| Gemma | 권장 설정 추론, 생성 16384 | 영문 | 워밍업 | 1 | 0 | 미채점 | [CSV](gemma4-12b_recommended-thinking-english-warmup_20260916-145206.csv) |
| Luna | Luna 비교, 생성 2048 | 영문 | 본 시험 | 5 | 5 | 15/15 | [CSV](gpt-5.6-luna_cloud-nonthinking-english_20260915-210424.csv) |
| Qwen | 기본 설정 비추론, 생성 2048 | 영문 | 본 시험 | 20 | 20 | 36/60 | [CSV](qwen3.5-9b_basic-nonthinking-english_20260915-203518.csv) |
| Qwen | 기본 설정 비추론, 생성 2048 | 영문 | 워밍업 | 1 | 1 | 2/3 | [CSV](qwen3.5-9b_basic-nonthinking-english-warmup_20260915-201709.csv) |
| Qwen | 기본 설정 비추론, 생성 2048 | 한국어 | 본 시험 | 6 | 6 | 6/18 | [CSV](qwen3.5-9b_basic-nonthinking-korean_20260915-203818.csv) |
| Qwen | 기본 설정 추론, 생성 2048 | 영문 | 본 시험 | 12 | 12 | 0/36 | [CSV](qwen3.5-9b_basic-thinking-english-tokens2048_20260916-115629.csv) |
| Qwen | 기본 설정 추론, 생성 4096 | 영문 | 본 시험 | 10 | 10 | 0/30 | [CSV](qwen3.5-9b_basic-thinking-english-tokens4096_20260916-125141.csv) |
| Qwen | 기본 설정 추론, 생성 2048 | 영문 | 워밍업 | 1 | 1 | 2/3 | [CSV](qwen3.5-9b_basic-thinking-english-tokens2048-warmup_20260916-115140.csv) |
| Qwen | 권장 설정 비추론, 생성 32768 | 영문 | 본 시험 | 10 | 10 | 17/30 | [CSV](qwen3.5-9b_recommended-nonthinking-english_20260916-143543.csv) |
| Qwen | 권장 설정 비추론, 생성 32768 | 영문 | 워밍업 | 1 | 0 | 미채점 | [CSV](qwen3.5-9b_recommended-nonthinking-english-warmup_20260916-143432.csv) |
| Qwen | 권장 설정 추론, 생성 32768 | 영문 | 본 시험 | 10 | 10 | 27/30 | [CSV](qwen3.5-9b_recommended-thinking-english_20260916-145628.csv) |
| Qwen | 권장 설정 추론, 생성 32768 | 영문 | 대표 문항 점검 | 1 | 1 | 3/3 | [CSV](qwen3.5-9b_recommended-thinking-english-completion-check_20260916-135803.csv) |
| Qwen | 권장 설정 추론, 생성 32768 | 영문 | 워밍업 | 1 | 0 | 미채점 | [CSV](qwen3.5-9b_recommended-thinking-english-warmup_20260916-144806.csv) |

## 보존 범위

이 목록의 파일은 기존 SQLite 자료를 변환한 결과다. 실행 날짜와 응답 원문, 관측값 및 판정 근거는 그대로 유지했다. 원본 자료와 전수 대조했으며 SQLite 파일은 변경하지 않았다. 이후 새 시험은 같은 구조의 CSV에 직접 기록한다.

신규 호출 명령과 설정 설명은 [프로젝트 README](../README.md)에 있다.
