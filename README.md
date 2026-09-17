# local-llm-financial-data-poc

**개인 노트북에서 실행한 로컬 LLM이 비트코인 시장과 가상 계좌 자료를 정확하게 설명할 수 있는지 확인하는 PoC이다.**

외부 LLM API가 담당하던 자료 해석을 로컬 모델로 옮기려 했으나, 최종 답변을 완성하지 못하거나 수치와 출처를 잘못 설명하는 문제가 나타났다. 이에 실제 매매 구현보다 먼저 **같은 자료를 받은 모델의 설명, 출력 형식, 과거 거래 검토와 실행 자원**을 평가했다. 현재 코드는 자료 해석 능력을 확인하는 검증 프로그램이다.

- 로컬 후보: **Qwen/Qwen3.5-9B**, **google/gemma-4-12B-it**
- Cloud 비교: **OpenAI GPT-5.6 Luna**, `gpt-5.6-luna`, 비추론 5문항
- 평가 대상: 입력 수치와 관측 시각, 잔액, 주문 및 체결, 손익에 대한 설명
- 제외 범위: 미래 가격 예측, 실제 주문과 매매 수익률, 한국어 문체 품질

[상세 보고서 목록](reports/README.md) · [문항과 채점 기준](modules/poc_cases.json) · [응답 원본 SQLite](data/poc-submission-20260915.sqlite)

## 핵심 결과

같은 영문 10문항의 필수 사실 충족률을 비교했다. 점수는 요구한 설명을 완전하게 포함했는지 나타내며, 모델의 모든 문장에 대한 정확도나 입력 전체의 정보 손실률은 아니다.

| 실행 조건 | Qwen | Gemma | 해석 |
| --- | --- | --- | --- |
| 기본 설정 비추론 | 36/60점, 60.0% · 중앙값 12.7초 | 50/60점, 83.3% · 중앙값 14.7초 | 빠르게 답했으나 두 후보 모두 사실 설명 90% 기준 미달 |
| 기본 설정 추론 | 생성 한도 2,048과 4,096에서 최종 답변 미완료 | 생성 한도 2,048과 4,096에서 최종 답변 미완료 | 저장된 본 시험 43회 모두 길이 제한으로 종료 |
| 권장 설정 비추론 | 17/30점, 56.7% · 중앙값 19.0초 | 22/30점, 73.3% · 중앙값 21.2초 | 권장 설정 적용만으로 오류와 누락이 해결되지는 않음 |
| 권장 설정 추론 | **27/30점, 90.0% · 중앙값 186.8초** | **28/30점, 93.3% · 중앙값 257.7초** | 설명 충족률 개선과 함께 응답 시간 증가 |

기본 비추론은 모델당 20응답, 권장 설정은 모델과 모드별 10응답이다. 분모가 다른 결과를 합산하지 않았다. 기본 추론의 최종 답변 미완료는 설명을 전부 틀렸다는 의미가 아니다.

**300초 이내 응답이 필요하다면 Qwen 권장 설정 추론을 우선 검토한다.** 10회 모두 제한 내 지정 JSON을 반환했다. Gemma는 설명 점수가 1점 높지만 300초 내 반환은 6/10회였으므로, 응답 지연을 허용하는 경우의 비교 후보이다. 권장 설정 추론의 회고 점수는 Qwen 10/12, Gemma 11/12이며 전체 30점에 이미 포함된다.

두 모델 모두 설명 누락과 부가 주장 오류가 남았다. 권장 설정의 문항별 한 번 실행을 기존의 두 번 반복 통과 조건 충족으로 처리하지 않았으며, 실제 자동매매 도입을 승인한 결과도 아니다. 자세한 응답 인용과 감점 근거는 [로컬 비교 보고서](reports/2026-09-15_KAN-202_local-poc.md)와 [응답 검토 보고서](reports/2026-09-15_KAN-203_review.md)에 정리했다.

## 모델 선정과 실행 환경

공개 점수만으로 모델을 선정하지 않았다. 양자화 배포 크기와 Ollama 지원을 먼저 확인한 뒤, Artificial Analysis의 종합 지능 점수와 지시 준수, 긴 자료 활용 등의 지표를 참고했다. 당시 비교에서 Qwen은 긴 자료 활용, Gemma는 지시 준수에서 상대 강점을 보여 서로 다른 계열의 후보로 선정했다.

약 12GB라는 GPU 용량은 초기 선별 조건이며, 모델 파일 크기와 실제 실행 메모리는 다르다. 큰 문맥 창에서는 추가 메모리가 필요하며, 이번 권장 설정의 Qwen 실행에는 일부 CPU 분산 적재가 포함됐다. 공개 평가와 로컬 양자화 환경도 같지 않다. 출처와 당시 점수, 라이선스, 전체 모델 식별값은 [후보 선정 보고서](reports/2026-09-15_KAN-199_model-selection.md)에 있다.

| 항목 | 시험 환경 |
| --- | --- |
| 장비 | ASUS ROG Zephyrus G14 GA403WR, Windows 11 Home 빌드 26200 |
| CPU와 RAM | AMD Ryzen AI 9 HX 370, 12코어 24스레드, 설치 RAM 64GB |
| GPU | NVIDIA GeForce RTX 5070 Ti Laptop GPU, 조회 용량 12,227MiB |
| 실행 도구 | Python 3.12.13, Ollama 0.33.3 |
| Python 주요 의존성 | ollama 0.6.2, openai 3.8.0, python-dotenv. 전체 버전은 uv.lock |
| 설치 태그 | qwen3.5:9b, gemma4:12b |
| 양자화 | 두 모델 모두 Q4_K_M |
| 실행 방식 | 모델 하나씩 적재, 호출 후 해제, 자동 재시도 없음 |

## 입력과 출력

실시간 시세 전체를 나열하는 대신 **정답을 직접 검산할 수 있는 가상 사례**를 구성했다. 예를 들어 손익 문항에는 체결량, 매수가, 평가 가격과 지급한 수수료를 제공한다. 자료 누락 문항은 일부 필드를 의도적으로 비워 판단 보류 여부를 확인한다.

| 입력 | 제공하는 내용 |
| --- | --- |
| as_of, data_notice | 기준 시점과 자료가 가상으로 구성됐다는 설명 |
| market_input | 필요한 봉의 종가와 EMA, 거래량, 호가 및 각각의 시간 |
| strategy_state | 가용 및 묶인 잔액, 이전 결정, 주문, 실제 체결과 손익 자료 |
| trading_rules | 가상 수수료율 0.05%, 최소 주문금액 5,000원, 자료 누락 시 보류 규칙 |
| required_observations | 해당 문항에서 반드시 설명해야 하는 세 가지 질문 |

EMA는 비교를 위해 주어진 값이며 모델에게 지표 재계산을 요구하지 않는다. 공포탐욕지수 68, Greed는 고정 보조 정보로 포함했다. 해당 지표의 예측력은 평가하지 않았다. 모델에는 공통 지시문과 input만 전달하고, expected와 rubric 등 정답 및 채점 자료는 전달하지 않는다.

| 문항 | 검증할 상황 | 별도 비교 |
| --- | --- | --- |
| Q01 | 현금만 보유한 계좌, 봉 가격 비교와 과거 기록 없음 | Cloud |
| Q02 | BTC만 보유한 계좌, 거래 가능 여부와 전량 매도금액 | — |
| Q03 | 서로 다른 봉의 가격과 거래량, 자료별 시각 구분 | 한국어, Cloud |
| Q04 | 가용 4,999원과 묶인 현금, 수수료 포함 최소 주문 조건 | — |
| Q05 | UTC와 KST, 봉 종가와 이후 호가 구분 | — |
| Q06 | 필수 시장 자료 누락, 남은 자료와 판단 제한 | 한국어, Cloud |
| Q07 | 이전 모델 호출 실패와 실제 결정 및 거래의 구분 | — |
| Q08 | 매수 체결 후 평가이익, 수수료와 미실현손익 | Cloud |
| Q09 | 체결 없이 취소된 주문, 시장 상승률과 계좌 수익률 | — |
| Q10 | 과거 설명의 숫자 비교 오류 정정, 체결과 손실 검산 | 한국어, Cloud |

출력은 다음 다섯 필드의 JSON 객체 하나이다. 아래는 구조를 보여주는 예시이며 실제 응답이나 정답이 아니다.

```json
{
  "decision": "hold",
  "buy_allocation_percentage": 0,
  "sell_allocation_percentage": 0,
  "reason": "Explain the required facts using the supplied sources.",
  "reflection_log": "Check prior decisions against orders, fills and valuation."
}
```

- decision: buy, sell, hold 중 하나
- 두 allocation 필드: 0~100 숫자. 보류는 모두 0, 매수와 매도는 해당 방향만 양수
- reason: 현재 의견과 필수 사실의 근거를 설명하는 영어 문자열
- reflection_log: 과거 결정과 주문 및 체결 검토, 또는 기록이 없다는 설명을 담은 영어 문자열

필수 세 항목은 reason 또는 reflection_log의 최종 답변을 기준으로 평가한다. 추론 원문에만 있는 설명으로 최종 답변의 누락 점수를 보충하지 않는다. 요청에는 JSON Schema도 제공했으므로 형식 준수 결과는 자유 생성 조건의 성능과 구분한다.

## 실험 조건과 평가 방법

### 네 가지 실행 조건

| 조건 | 문맥 창 | 생성 한도 | temperature / top_p / top_k | 관측 제한 |
| --- | ---: | ---: | --- | ---: |
| 기본 비추론, 두 모델 | 8,192 | 2,048 | 0 / 0.95 / 40 | 300초 |
| 기본 추론, 두 모델 | 8,192 | 2,048 또는 4,096 | 0 / 0.95 / 40 | 300초 |
| 권장 비추론, Qwen | 131,072 | 32,768 | 0.7 / 0.8 / 20 | 1,800초 |
| 권장 추론, Qwen | 131,072 | 32,768 | 1.0 / 0.95 / 20 | 1,800초 |
| 권장 비추론 및 추론, Gemma | 32,768 | 16,384 | 1.0 / 0.95 / 64 | 1,800초 |

공통 seed 42, min_p 0, repeat_penalty 1, frequency_penalty 0이다. presence_penalty는 기본 설정과 Gemma에서 0, 권장 설정 Qwen에서 1.5이다. “기본 설정”은 이번 시험의 공통 설정을 뜻하며 Ollama 기본값과 같다는 의미는 아니다.

Qwen은 일반 과제의 공식 권장 샘플링과 생성 한도, Serving 절의 문맥 안내를 적용했다. Gemma의 공식 샘플링을 적용하되 문맥 32,768과 생성 16,384는 실행자가 정한 한도이다. 1,800초는 답변 완료를 관찰하기 위한 제한이며 **300초 내 반환이라는 운영 요구는 별도로 평가**했다.

공식 출처: [Qwen Best Practices 및 Serving](https://huggingface.co/Qwen/Qwen3.5-9B#best-practices), [Gemma Best Practices](https://ai.google.dev/gemma/docs/core/model_card_4#best-practices). 실제 요청과 확인 당시 설정은 SQLite에 보존했다.

### 점수와 통과 조건

평가는 다음 세 가지로 나눴다.

1. **형식 검사:** 코드로 다섯 JSON 필드, 자료형, 허용값과 중복 키 확인
2. **거래 제한 검사:** 코드로 의견과 비율, 가용 잔액, 수수료와 최소 주문, 누락 시 보류 규칙 확인
3. **설명 평가:** Codex가 최종 답변을 입력 수치와 계산식에 대조하고 인용문, 입력 위치와 판정 이유 기록

의미에 대한 판정은 자동 채점 코드가 수행한 것이 아니다. 검토 코드는 인용과 입력 경로를 확인해 판정을 저장한다. 독립된 사람의 교차 검토로 표현하지 않는다.

문항별 필수 세 항목은 완전 충족 1점, 부분 누락 또는 오류 0점이다. 같은 값에 대한 모순도 미충족으로 처리한다. 최종 답변 미완료는 0/3점으로 처리하되 내용 오류와 구분하며, 판정할 결정이 없으면 거래 제한은 판정 불가이다. 미실행과 미검토를 채점 완료로 처리하지 않는다. 필수 항목 밖의 잘못된 주장도 별도로 기록했다.

기본 비추론의 사전 기준은 모델별 20회 중 19회 이상 300초 내 지정 JSON, 사실 54/60점 이상, 중대한 거래 제한 위반 0건이다. 회고는 Q07~Q10의 점수로 전체 점수에 포함된다. 후보 간 우선순위는 필수 조건 충족 여부, 사실 설명, 회고, 시간, GPU 사용량 순서이며 선호 시간은 중앙값 60초 이내다. 권장 설정은 모델별 10응답으로 반복 수가 달라 기존 20회 통과 판정을 직접 적용하지 않았다.

### 측정과 해석 범위

전체 응답 시간은 로딩과 입력 처리, 추론 및 최종 답변 생성을 포함한다. 입력과 생성 토큰은 서버 보고값을 사용했다. GPU 전체 사용량은 nvidia-smi로 약 1초마다 관측하고, 응답 후 Ollama에서 조회한 모델 적재량과 구분했다. 다른 앱과 전원 및 열 상태를 완전히 통제한 성능 시험은 아니다.

여러 설정을 함께 바꿨고 Qwen은 권장 모드별 샘플링도 다르므로, 점수 변화가 추론 기능이나 단일 설정의 효과라고 단정하지 않는다. 기본 설정의 반복 응답이 같았다는 관찰도 새로운 시장 상황을 여러 번 검증했다는 뜻은 아니다.

## 한국어 입력과 Cloud 비교

기본 비추론의 Q03, Q06, Q10을 한국어로 모델별 두 번씩 제공했다. 모델당 한국어 6응답의 필수 사실은 둘 다 6/18점, 같은 문항의 영문은 Qwen 8/18점, Gemma 12/18점이다. 영문에도 오류가 남아 한국어만이 원인이라고 결론 내리지 않았다. 한국어 문체는 평가하지 않았으며 추론의 한국어 시험을 수행한 것으로 합산하지 않는다.

Luna는 Q01, Q03, Q06, Q08, Q10을 각 한 번씩 실행해 지정 JSON 5/5, 필수 사실 15/15, 평균 8.539초를 기록했다. 추론은 none, temperature 0, 출력 한도 2,048이다. 로컬의 공통 5문항 기본 비추론은 두 번씩 실행해 Qwen 18/30, Gemma 24/30이었다. 문항 수와 반복 수가 다른 전체 모델 순위로 확대하지 않는다.

사용량과 당시 단가로 계산한 5회 비용은 **USD 0.00375315**, 평균 **USD 0.00075063/회**이다. 같은 입력과 출력 분량, 24시간 운영, 30일을 가정하면 다음과 같다.

| 호출 간격 | 하루 / 30일 호출 수 | 하루 예상 비용 | 30일 예상 비용 |
| --- | ---: | ---: | ---: |
| 30분마다 한 번 | 48 / 1,440 | 약 $0.0360 | 약 **$1.08** |
| 1시간마다 한 번 | 24 / 720 | 약 $0.0180 | 약 **$0.54** |

이는 **비추론 시험 사용량을 환산한 비용**이다. 실제 비용은 입력과 출력 길이, 캐시, 추론량과 재시도에 따라 달라진다. 세금 및 환율은 포함하지 않았으며 청구서 확정 금액도 아니다. [Luna 상세 보고서](reports/2026-09-15_KAN-204_cloud-poc.md)에 사용량과 단가 출처를 보존했다. 로컬 전기요금은 측정하지 않았다.

## 코드 구조와 정리된 범위

```text
local-llm-financial-data-poc/
├── modules/
│   ├── poc_cases.json                # 공통 지시문, 고정 입력, 별도 채점 기준
│   ├── evaluation_poc.py             # 로컬 실험 계획 등록과 실행
│   ├── evaluation_local.py           # Ollama 호출, 시간과 GPU 측정, 모델 해제
│   ├── evaluation_cloud.py           # Luna 호출과 사용량 및 비용 기록
│   ├── response_schema.py            # 공통 JSON Schema
│   ├── evaluate_response.py          # 형식 및 거래 제한 검사
│   ├── evaluation_review.py          # 최종 답변과 추론의 근거를 연결한 검토 저장
│   ├── evaluation_storage.py         # 실험, 호출과 평가 기록의 SQLite 저장
│   ├── evaluation_report.py          # 집계와 조건별 보고서 생성
│   ├── evaluation_export.py          # 선택한 실험을 별도 SQLite로 내보내기
│   └── json_utils.py                 # JSON 직렬화와 엄격한 파싱
├── tests/
│   ├── __init__.py                   # 검사 패키지와 공통 대체 객체의 가져오기 지원
│   └── test_evaluation_*.py          # 8개 영역의 자동 검사, 실제 모델 및 유료 API 호출 없음
├── data/
│   └── poc-submission-20260915.sqlite # 9월 15~16일 원본을 보존한 제출 DB
├── reports/                         # 상세 보고서 8개와 읽는 순서
├── pyproject.toml
├── uv.lock
└── README.md
```

현재 실행 경로는 **문항 선택 → 계획과 설정 저장 → 모델 호출 및 측정 → 형식과 거래 제한 검사 → 응답 저장 → 근거를 대조한 검토 → 보고서 집계**이다.

`modules/`에는 실행과 평가에 필요한 Python 파일 10개와 문항 JSON 1개를 두고, 자동 검사 8개 파일은 `tests/`에서 관리한다. 실제 모델 호출은 `evaluation_poc.py` 또는 `evaluation_cloud.py`에서 시작한다. `tests/`의 대체 응답은 검사에만 사용하며, 모델 요청의 입력으로 전달하지 않는다.

`poc_cases.json`은 모델에 전달할 지시문과 입력, 평가자 전용 정답을 함께 보관한다. 실행기는 이 중 지시문과 입력만 요청에 넣는다. `response_schema.py`는 반환할 다섯 필드의 구조를 정의하고, `evaluate_response.py`는 실제 응답의 형식과 거래 규칙을 검사한다. 설명의 옳고 그름은 검토자가 판정하고, `evaluation_review.py`는 인용문과 원본 경로를 확인해 그 판정을 저장한다.

과거 보고서와 SQLite의 실행 코드 원문에는 당시 파일 이름을 유지했다. 현재 대응 경로는 다음과 같다.

| 과거 경로 | 현재 경로 |
| --- | --- |
| `modules/data_evaluation_cases.py` | `modules/json_utils.py` |
| `modules/evaluation_prompt.py` | `modules/response_schema.py` |
| `modules/validate_evaluation_*.py` | `tests/test_evaluation_*.py` |

과거 계획을 바탕으로 **새 계획을 만들면** 당시 입력과 설정을 가져오고 현재 코드 원문과 해시를 새로 저장한다. 기존 계획을 그대로 실행할 때 적용되는 코드 변경 검사는 유지하므로, 이전 실험을 재현할 때에는 아래 준비 명령으로 새 ID를 생성한다.

- 유지한 기능: 고정 문항, 로컬 및 Luna 호출, 조건별 실행 계획, 측정, 검토와 보고서
- 이후 반영한 기능: 기본 추론의 생성 한도별 계획, 대표 문항 완료 점검, 권장 설정 전체 문항 비교, 추론 원문 검토 및 비교 보고서
- 정리한 기능: 실시간 Upbit 수집 및 주문, Notion 응답 저장, 네 계좌 운영, 순차 대화와 토론, 추가 후보 확장 코드
- 정리한 파일: 이전 main.py, data_ohlcv 계열과 trade_upbit.py 등 자동매매 모듈, 이전 trading.db와 당시 evaluation.db. 구체적인 목록은 [제출 정리 기록](reports/2026-09-15_KAN-206_reexecution-and-submission.md)
- Git 제외: .env, test.py, 가상환경, 모델 가중치와 새 실험 DB. 제출용 SQLite 한 개만 예외로 포함

삭제된 코드는 현재 README의 실행 과정에서 사용하지 않는다. 현재의 `data/evaluation.db`는 재실행 시 새로 만들거나 원본에서 복사하는 작업용 DB이며, 과거에 정리한 DB와 구분한다.

## 원본 기록

2026-09-17 확인 기준 제출 DB는 **실험 계획 16개, 호출 시도 153개, 저장된 호출 원본 151개**이다. 153개 시도에는 중단 2개가 포함된다. 저장된 호출에는 길이 제한으로 끝난 응답도 있으므로 151개 모두 최종 답변을 완성했다는 의미는 아니다.

| 기록 범위 | 저장된 호출 원본 수 |
| --- | ---: |
| 9월 15일: 기본 비추론 영문 40, 한국어 12, Luna 5, 워밍업 2, 재실행 1 | 60 |
| 9월 16일: 기본 추론 워밍업 | 2 |
| 기본 추론 생성 한도 2,048 / 4,096 | 24 / 19 |
| 권장 설정 대표 Q08 완료 점검 | 2 |
| 권장 설정 비추론 / 추론 본 시험 | 20 / 20 |
| 권장 설정 모드별 워밍업 | 4 |
| 합계 | **151** |

계획만 만들고 호출하지 않은 항목도 16개 계획에 포함되어 있다. 호출 없는 계획, 중단, 길이 제한 종료와 정상 답변을 별도로 조회한다.

| SQLite 테이블 | 보존 내용 |
| --- | --- |
| evaluation_experiments | 계획, 입력과 채점 기준, 설정, 당시 코드 원문과 해시 |
| evaluation_attempts | 실제 요청, 실행 상태, 원본 연결, 형식 검사와 검토 기록 |
| evaluation_calls | 최종 응답과 추론, 토큰, 시간, 자원 관측과 종료 사유 |
| evaluation_export_manifest | 최초 9월 15일 60회 내보내기 당시 범위. 현재 전체 DB의 목록은 아님 |

파일명의 20260915는 최초 제출 시점이다. 이후 9월 16일 실험도 같은 파일에 보존했고 과거 원본을 유지했다. 최초 내보내기 명세와 예전 보고서의 60회는 당시 기록이며 현재 총수와 구분한다.

현재 제출 DB SHA-256:
`81ce73b1e404fe776cbd0a22f09c85b8e6e9c371ab94f1994f22eebd27b79a51`

## 설치와 결과 조회

저장소 루트의 PowerShell 기준이다. Python 3.12와 uv가 필요하다. **저장된 결과 조회에는 실행 중인 모델이나 API 키가 필요 없다.**

```powershell
git clone https://github.com/YunsOps/local-llm-financial-data-poc.git
cd local-llm-financial-data-poc
uv sync --frozen --python 3.12

# 기본 비추론 결과 조회
uv run --frozen python -m modules.evaluation_report poc-main-20260915T113044Z --db data/poc-submission-20260915.sqlite

# 권장 설정 추론 결과 조회
uv run --frozen python -m modules.evaluation_report poc-best-practices-thinking-main-20260916T053346Z --db data/poc-submission-20260915.sqlite

# Luna 사용량과 평가 결과 조회
uv run --frozen python -m modules.evaluation_report poc-cloud-20260915T114757Z --db data/poc-submission-20260915.sqlite
```

원본 ID를 확인할 때에는 위 CLI 또는 SQLite의 evaluation_experiments를 사용한다. 기본 추론 ID는 `poc-main-reasoning-20260916T025114Z`, `poc-main-reasoning-4096-20260916T035031Z`이다. 권장 비추론 ID는 `poc-best-practices-nonthinking-main-20260916T053337Z`이다.

## 새 실험 재실행

모델을 실제 호출하려면 Ollama 서버를 먼저 실행하고 두 모델을 설치한다.

```powershell
ollama pull qwen3.5:9b
ollama pull gemma4:12b
ollama --version
ollama ls
```

동일 태그라도 가중치가 바뀔 수 있다. 실행 전 설치 digest와 기준 실험의 값을 비교하며 다르면 중단한다. 변경된 가중치를 사용한 결과를 기존 모델 파일의 재현으로 취급하지 않는다.

### 권장 설정의 두 모드 비교

기존 계획과 입력을 사용하되, **제출 원본을 작업용 DB로 복사**한 뒤 새 실험 ID로 실행한다. 기존 작업 DB를 덮어쓰지 않는다.

```powershell
if (!(Test-Path data/evaluation.db)) {
    Copy-Item data/poc-submission-20260915.sqlite data/evaluation.db
}

# 모델을 호출하지 않고 새 비추론 계획 등록
uv run --frozen python -m modules.evaluation_poc --prepare-best-practices-from poc-completion-check-20260916T045745Z

# 모델을 호출하지 않고 새 추론 계획 등록
uv run --frozen python -m modules.evaluation_poc --prepare-best-practices-from poc-completion-check-20260916T045745Z --thinking

# 각 준비 명령이 출력한 experiment_id를 붙여 넣어 실행, 계획당 로컬 20회
uv run --frozen python -m modules.evaluation_poc <new_nonthinking_experiment_id>
uv run --frozen python -m modules.evaluation_poc <new_thinking_experiment_id>
```

모드별 W00 워밍업 계획은 같은 준비 명령에 `--mode-warmup`을 붙여 별도로 생성한다. 워밍업을 본 문항 점수에 합산하지 않는다. 매 호출 뒤 모델을 해제하므로 본 시험 전체가 항상 메모리에 미리 적재된 상태로 실행되는 것은 아니다.

새 응답의 의미 평가는 자동으로 복사되지 않는다. `record_required_review()`로 응답 인용, 입력 경로와 판정 이유를 다시 기록해야 하며, 미검토 응답은 과거 점수를 가져오거나 0점 확정으로 표시하지 않는다.

### 기본 설정과 한 문항 재실행

```powershell
# 기본 비추론: 영문 40회와 한국어 12회의 새 계획 등록
uv run --frozen python -m modules.evaluation_poc --prepare main
uv run --frozen python -m modules.evaluation_poc <new_main_experiment_id>

# 기본 추론: 기존 영문 입력, 문항당 한 번, 생성 한도 4,096
uv run --frozen python -m modules.evaluation_poc --prepare-mode-from poc-main-20260915T113044Z --mode-num-predict 4096 --mode-repeat-count 1
uv run --frozen python -m modules.evaluation_poc <new_reasoning_experiment_id>

# Gemma Q06 영문 한 문항만 별도 재실행
uv run --frozen python -m modules.evaluation_poc --prepare-selfcheck-from poc-main-20260915T113044Z --model gemma4:12b
uv run --frozen python -m modules.evaluation_poc <new_selfcheck_experiment_id>
```

준비 명령은 모델 정보를 조회하고 계획을 저장하며, 답변을 생성하지 않는다. 같은 계획에 이미 등록한 시도는 성공 여부와 관계없이 자동 재호출하지 않는다. 입력이나 설정을 바꾸면 새 계획으로 실행한다.

### Luna 호출, 보고서와 내보내기

Cloud 비교에만 프로젝트 .env의 `OPENAI_API_KEY`가 필요하다. 키를 Git에 올리지 않는다. 다음 두 번째 명령은 **유료 5회 호출**이다. 거래소나 Notion 키는 사용하지 않는다.

```powershell
uv run --frozen python -m modules.evaluation_cloud --prepare-from-local poc-main-20260915T113044Z
uv run --frozen python -m modules.evaluation_cloud <new_cloud_experiment_id>

# 새 결과 보고서 생성, 기존 상세 보고서 덮어쓰기 방지
uv run --frozen python -m modules.evaluation_report <new_experiment_id> --markdown reports/new-result.md

# 두 권장 모드의 비교 보고서 생성
uv run --frozen python -m modules.evaluation_report <new_nonthinking_experiment_id> --best-practices-thinking-id <new_thinking_experiment_id> --markdown reports/new-mode-comparison.md

# 선택한 실험을 별도 SQLite로 내보내기
uv run --frozen python -m modules.evaluation_export <new_experiment_id> --output data/new-export.sqlite
```

## 코드 검사와 보존 원칙

실제 모델과 유료 API를 호출하지 않고 요청 구성, 검산, 중복 실행 방지, 저장, 검토 근거, 집계와 내보내기를 검사한다. 2026-09-17 기준 **68개 검사 통과**, 제출 SQLite 무결성 검사 통과를 확인했다.

```powershell
uv run --frozen python -m unittest discover -s tests -t .
```

실행 당시의 코드 원문과 설정은 SQLite에 저장되어 있으며 현재 코드와 구분한다. 문장을 다듬거나 보고서를 정리할 때도 실제 입력, 응답, 판정과 측정값은 유지한다. 새 조건의 결과는 새 실험 ID로 남기고 과거 결과와 합산하지 않는다.
