# 후보 모델 선정 근거와 설치 정보 확인 보고서

공개 평가 점수와 노트북 실행 조건을 함께 검토해 **Qwen/Qwen3.5-9B와 google/gemma-4-12B-it을 비교 후보로 유지했습니다.** 이 보고서는 후보를 고른 이유를 설명합니다. 실제 과제의 정확성은 이후 시험으로 따로 확인했으며, 최종 도입 판단은 [종합 보고서](2026-09-15_KAN-205_poc-conclusion.md)에 정리했습니다.

[보고서 목록과 읽는 순서](README.md)

- 관련 작업: [KAN-199 — 로컬 후보 2개와 비교 모델 확인](https://limuxmaster.atlassian.net/browse/KAN-199)
- 작성일: 2026-09-15
- 설치 정보 확인 시각: 2026-09-15T10:59:57.076296+00:00 / 2026-09-15 19:59:57 KST
- 성격: 후보 선정 근거의 재정리, 공식 출처 확인, 설치 정보 조회와 기존 결과 재집계
- 이번 작업의 모델 추론 호출: **0회**
- 범위: 후보 선정 근거 확인 완료. 이후 본 시험과 최종 판단은 KAN-202 및 KAN-205에 별도 기록.

## S. 왜 후보 선정 근거를 다시 정리했는가

이 프로젝트는 기존 비트코인 자동매매 실습에서 시작했습니다. 현재는 로컬 모델이 시장과 가상 계좌 자료를 정확히 읽고, 지정한 JSON 형식으로 의견과 회고를 작성할 수 있는지 확인하는 PoC입니다.

노트북의 GPU 메모리에서 실행할 수 있을 만한 크기의 모델을 먼저 추린 뒤, Artificial Analysis(AA)의 공개 지능 점수와 세부 평가 점수를 참고해 Qwen과 Gemma를 골랐습니다. 종합 점수가 비슷하더라도 지시 준수와 긴 자료 활용 점수에 차이가 있어, 서로 다른 계열의 두 모델을 같은 과제로 비교할 이유가 있었습니다.

이번 보고서는 **당시 선택의 근거**, **이후 확인한 노트북 실행 기록**, **조회 당시 설치 정보**를 구분하여 정리합니다. 실제 매매의 수익성이나 새 영문 조건의 성능을 검증하는 보고서는 아닙니다.

## T. 이번 확인의 목표와 완료 기준

1. 당시 비교했던 점수와 확인 날짜 및 기록 출처의 복원.
2. 두 후보가 다른 계열이며, 어떤 평가 항목 때문에 비교할 가치가 있었는지 설명.
3. 공식 모델명, 라이선스, 문맥 길이와 조회 당시 설치 태그 및 식별값 확인.
4. 노트북에서 실행 가능했다는 근거를 모델 파일 크기와 구분하여 제시.
5. 공개 점수에서 말할 수 있는 범위와 이번 PoC에서 새로 확인할 범위의 구분.

후보 조사 완료는 최종 사용 모델 선정 완료를 뜻하지 않습니다. 최종 선정에는 새 영문 본 비교와 요구사항에 따른 판정이 필요합니다.

## A. 확인 방법

### 1. 당시 선정 기록 확인

[보관된 KAN-211](https://limuxmaster.atlassian.net/browse/KAN-211)의 2026-09-14 공개 점수 기록을 확인했습니다. 아래 표는 그 기록을 옮긴 것이며, 모든 세부 점수를 2026-09-15에 다시 조회한 표가 아닙니다.

| 평가 항목 | Qwen/Qwen3.5-9B | google/gemma-4-12B-it | 후보 선정에서 본 의미 |
| --- | ---: | ---: | --- |
| AA 지능 점수 | 14 | 14 | 종합 점수만으로 한 후보를 우위로 정하기 어려운 상태 |
| IFBench | 67% | 74% | 지정한 지시와 출력 규칙을 따르는 능력의 참고 |
| AA-LCR v1.1 | 70% | 64% | 긴 입력의 정보를 활용하는 능력의 참고 |
| AA-Omniscience Accuracy | 16% | 16% | 지식 질문의 정확성 참고 |
| AA-Omniscience Non-Hallucination | 16% | 19% | 해당 평가에서의 허위 답변 억제 관련 참고 |
| Humanity’s Last Exam | 15% | 16% | 어려운 지식과 추론 질문에 대한 참고 |

당시 출처: [Qwen 평가 페이지](https://artificialanalysis.ai/models/qwen3-5-9b), [Gemma 평가 페이지](https://artificialanalysis.ai/models/gemma-4-12b). 세부 숫자의 보존 근거는 KAN-211의 당시 기록입니다. 당시 전체 후보 목록의 동일 시점 점수와 순위표를 이번 작업에서 완전히 복원한 것은 아니므로, 두 모델이 모든 가용 후보 중 정확히 1위와 2위였다고 표현하지 않습니다.

2026-09-15 재조회에서는 두 페이지 모두 지능 점수 14를 표시했습니다. 조회 당시 페이지는 추론 사용(Reasoning) 조건이었으며, FAQ에는 추정치(estimated)라는 안내도 있었습니다. 지능 지수(Intelligence Index)는 v4.3으로 표시되었습니다. 세부 그래프의 전체 숫자와 과거 지수 버전까지 같은 것으로 확인한 것은 아닙니다. 따라서 당시 기록과 최신 공개 평가를 하나의 새 실측 표로 합치지 않습니다. [Qwen 현재 페이지](https://artificialanalysis.ai/models/qwen3-5-9b), [Gemma 현재 페이지](https://artificialanalysis.ai/models/gemma-4-12b)

### 2. 공식 모델 정보와 사용 조건 확인

| 항목 | Qwen | Gemma |
| --- | --- | --- |
| 공식 이름 | Qwen/Qwen3.5-9B | google/gemma-4-12B-it |
| 계열 | Qwen | Google Gemma |
| 공식 문맥 길이 안내 | 기본 262,144토큰 | 최대 256K토큰 |
| 공식 라이선스 표기 | Apache 2.0 | Apache 2.0 |
| 이번에 사용하는 형태 | Ollama의 GGUF Q4_K_M | Ollama의 GGUF Q4_K_M |
| 새 PoC 계획의 문맥 창 | 8,192토큰 | 8,192토큰 |

출처: [Qwen 모델 카드](https://huggingface.co/Qwen/Qwen3.5-9B), [Qwen 라이선스 원문](https://huggingface.co/Qwen/Qwen3.5-9B/blob/main/LICENSE), [Gemma 모델 카드](https://huggingface.co/google/gemma-4-12B-it), [Gemma 4 Apache 2.0 원문](https://ai.google.dev/gemma/apache_2).

문서의 최대 문맥 길이는 노트북에서 해당 길이를 처리할 수 있다는 실측값이 아닙니다. Qwen 공식 안내는 긴 추론을 위한 문맥 확보도 강조하고 있습니다. 이번의 짧은 비추론 PoC는 그 최대 추론 성능을 재현하는 시험으로 표현하지 않습니다.

라이선스는 해당 버전의 공식 표기를 확인한 결과입니다. 이전 Gemma 버전이나 다른 모델의 조건을 이름만 보고 대입하지 않습니다. 모델 가중치를 제출 저장소에 넣지 않으며, 이후 별도 배포가 필요하면 해당 원문과 공지사항을 적용합니다.

### 3. 노트북과 설치 정보 조회

- GPU: NVIDIA GeForce RTX 5070 Ti Laptop GPU, 12227 MiB, 610.88
- Ollama: 0.33.3
- Python: 3.12.13 (main, Jul 18 2026, 17:08:38) [MSC v.1944 64 bit (AMD64)]
- 기존 사용자 제공 시스템 정보: ASUS ROG Zephyrus G14, Ryzen AI 9 HX 370, 설치 RAM 64GB, Windows 11 Home.
- 조회 방법: Ollama의 `/api/version`, `/api/tags`, `/api/show`, `/api/ps` 및 `nvidia-smi`.
- 조회 종료 시 적재 모델: 없음.
- 다운로드, 모델 교체, 생성 API 호출과 거래 API 호출: 없음.

| 설치 태그 | 설치 메타데이터의 파라미터 규모 | 양자화 | 모델 묶음 크기 | 메타데이터 문맥 길이 |
| --- | ---: | --- | --- | ---: |
| qwen3.5:9b | 9.7B | Q4_K_M | 6.594 GB / 6.142 GiB | 262,144 |
| gemma4:12b | 11.9B | Q4_K_M | 7.557 GB / 7.038 GiB | 262,144 |

공식 이름의 9B와 12B는 모델명입니다. 설치 메타데이터의 실제 개수는 각각 9,653,104,368개와 11,907,350,576개입니다. 이름과 정확한 파라미터 개수를 구분해 기록합니다.

파일 크기에는 적재 이후의 문맥 캐시와 작업 메모리 및 다른 앱의 GPU 사용량을 반영할 수 없습니다. 따라서 위 크기가 VRAM보다 작다는 이유만으로 모든 입력 길이를 실행 가능하다고 판정하지 않습니다.

`/api/show`가 반환한 template 문자열은 두 모델 모두 `{{ .Prompt }}`였습니다. 아래 원본의 template 해시가 같다는 사실은 실제 대화 처리 방식이 같다는 증거가 아닙니다. 실행 중 선택된 템플릿과 적용 경로는 환경 확인 단계에서 별도로 점검하도록 정했습니다.

### 4. 사용한 조회와 재집계 코드

설치 정보 조회는 Python 표준 라이브러리의 `urllib.request`로 위 API를 읽는 방식으로 수행했습니다. 재확인할 수 있는 같은 조회 절차는 다음과 같습니다. 모델에 응답 생성을 요청하는 코드가 아닙니다.

<details>
<summary>지시문 또는 실행 기록 원문 펼치기</summary>

```python
import datetime
import hashlib
import json
import subprocess
import sys
import urllib.request

def api(path, payload=None):
    # 생성 요청 없이 설치 정보만 조회하는 HTTP 요청 구성
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/" + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)

# 조회 당시 버전과 설치 목록 확보
tags = {row["name"]: row for row in api("tags")["models"]}
result = {
    "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "python": sys.version,
    "ollama": api("version"),
    "models": [],
}

for tag in ("qwen3.5:9b", "gemma4:12b"):
    shown = api("show", {"model": tag})
    info = shown.get("model_info", {})
    # 모델 전체 식별값과 기본 설정, 문맥 길이 및 원문 해시의 보존
    result["models"].append({
        "tag": tag,
        "digest": tags[tag]["digest"],
        "file_size_bytes": tags[tag]["size"],
        "details": shown.get("details"),
        "capabilities": shown.get("capabilities"),
        "parameters": shown.get("parameters"),
        "model_info": {
            key: value for key, value in info.items()
            if key in ("general.architecture", "general.parameter_count",
                       "general.file_type")
            or key.endswith(".context_length")
        },
        "template_sha256": hashlib.sha256(
            shown.get("template", "").encode()).hexdigest(),
        "license_sha256": hashlib.sha256(
            shown.get("license", "").encode()).hexdigest(),
        "license_first_line": shown.get("license", "").strip().splitlines()[:1],
    })

# 모델 적재 여부와 GPU 이름, 전체 VRAM 및 드라이버 확인
result["loaded_models"] = api("ps")["models"]
result["gpu"] = subprocess.run(
    ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
     "--format=csv,noheader"],
    text=True, capture_output=True,
).stdout.strip()
print(json.dumps(result, ensure_ascii=False, indent=2))
```

</details>

이전 한국어 시험을 재집계할 때는 당시 `evaluation_report.py`의 `summarize_experiment` 함수를 사용했습니다. 아래 두 코드는 당시 수행한 작업의 기록입니다. 현재 제출 코드에서는 이 함수와 이전 실험을 제거했으므로, 현재 저장소에서 그대로 실행하는 명령으로 사용하지 않습니다.

```python
from modules.evaluation_report import summarize_experiment

report = summarize_experiment("schema-mode-20260915T060154Z")
```

당시 같은 집계를 조회한 명령:

```powershell
.\.venv\Scripts\python.exe -m modules.evaluation_report schema-mode-20260915T060154Z
```

이전 한국어 시험에는 당시의 `evaluation_runner.py`와 `evaluation_local.py`를 사용했습니다. 실행 당시 코드 원문과 해시는 이전 SQLite의 `evaluation_experiments.config_json`에 저장했습니다. 해당 코드와 DB는 제출 전 백업 후 정리했으며, 이번 제출용 SQLite에는 포함하지 않았습니다. 부록에는 조회 당시 코드 해시와 이전 실험의 식별 정보를 보존했습니다. 현재 PoC를 다시 실행하는 방법은 [저장소 README](../README.md)를 기준으로 합니다.

## R. 확인한 결과

### 1. 노트북 실행 가능성의 기존 증거

아래 수치는 **2026-09-15의 이전 한국어 시험을 다시 집계한 결과**입니다. 최초 후보를 고를 때 이미 알고 있던 근거가 아니라, 후보 선정 후 확보한 실행 증거입니다.

- 실험 ID: `schema-mode-20260915T060154Z`
- 입력: 고정 가상 10사례, 모델당 2회.
- 조건: 한국어 지시문, 문맥 창 8,192, 생성 한도 2,048, 비추론, JSON Schema.
- 당시 원본 위치(백업 후 정리한 이전 DB): `data/evaluation.db`의 `evaluation_experiments`, `evaluation_attempts`, `evaluation_calls`.
- 시간 범위: 2026-09-15 15:29:15~15:55:10 KST.
- 저장된 입력 JSON 복원 대조: 40/40 일치.

| 항목 | Qwen | Gemma |
| --- | ---: | ---: |
| 완료 / 실제 시도 | 20/20 | 20/20 |
| GPU 전체 적재 보고 | 20/20 | 20/20 |
| 전체 응답 평균, 초 | 19.16 (n=20) | 25.80 (n=20) |
| 로딩 평균, 초 | 4.69 (n=20) | 4.88 (n=20) |
| 생성 평균 속도, tokens/s | 30.08 (n=20) | 19.86 (n=20) |
| 응답 직후 모델 VRAM, MiB | 5463.76 (n=20) | 7985.04 (n=20) |
| 입력 토큰 범위 | 1547~2128 (n=20) | 1700~2323 (n=20) |
| 생성 토큰 범위 | 170~430 (n=20) | 194~484 (n=20) |
| GPU 전체 최고 관측값, MiB | 8,107 (n=20) | 10,076 (n=20) |

전체 응답 시간에는 모델 적재와 응답 수신이 포함됩니다. 모델 VRAM은 응답 직후 Ollama 적재 정보이고 GPU 최고 관측값은 다른 앱까지 포함한 시스템 전체 관측값입니다. 두 값의 차이를 전부 다른 프로그램 메모리로 단정하지 않습니다.

두 모델은 이 짧은 조건에서 GPU 전체 적재와 최종 JSON 반환이 가능했습니다. 그러나 이전 필수 설명 항목의 완전 충족은 Qwen 18/48, Gemma 34/48이었습니다. 당시의 48항목은 이전 채점표의 수량이며 새 PoC의 모델당 60항목과 다릅니다. JSON 반환을 정확한 판단이나 매매 능력으로 해석하지 않습니다.

### 2. 조회 당시 설치 조건에서 주의할 차이

Qwen 태그에는 `presence_penalty=1.5`가 내장되어 있고, 두 모델의 `top_k` 기본값도 다릅니다. 같은 입력과 온도만 지정해도 모든 생성 설정이 같아지는 것은 아닙니다. 새 PoC에서는 [KAN-201](https://limuxmaster.atlassian.net/browse/KAN-201)에 정한 공통 설정을 요청에 명시하고 적용 여부를 확인하도록 설계했습니다. 실제 적용 결과는 KAN-200과 KAN-202에 기록했습니다.

공개 AA 페이지의 추론 조건, 원래 모델의 정밀도와 서비스 환경, 이번 Q4_K_M 및 비추론 조건은 다릅니다. 이 PoC는 AA 점수의 재현 시험이 아니며, 공개 점수는 후보를 좁히는 출발점으로 사용합니다.

### 3. 이번 단계의 판정

| 확인 사항 | 결과 | 근거 |
| --- | --- | --- |
| 당시 선정 이유 복원 | 완료 | 2026-09-14 점수 기록과 비교 항목 |
| 공식 모델명과 라이선스 출처 | 완료 | 두 공식 모델 카드 및 버전별 라이선스 원문 |
| 설치 태그와 식별값 | 완료 | 현재 Ollama 메타데이터 |
| 이 노트북의 실행 근거 | 확보 | 이전 8K 조건에서 모델당 20회 완료 및 GPU 적재 기록 |
| 실제 대화 템플릿 적용 경로 | 후속 확인 | 표시용 template 문자열만으로 검증 불가 |
| 새 영문 PoC 품질 | 미실행 | 새 조건의 응답 0회 |
| 최종 도입 판단 | 이후 단계에서 완료 | KAN-205에 본 비교 결과와 선정 이유 기록 |

## 결론

**Qwen과 Gemma를 비교 후보로 유지할 이유가 있습니다.** 초기에는 노트북의 메모리 제약을 고려하고 공개 점수를 참고했으며, 종합 점수가 비슷한 두 모델이 서로 다른 세부 강점을 보였습니다. 이후 실제 노트북에서 짧은 입력의 실행 가능성도 확인했습니다.

Qwen은 당시 AA-LCR 수치가 상대적으로 높았고, Gemma는 IFBench 수치가 상대적으로 높았습니다. 이 차이는 긴 자료 활용과 지시 준수라는 서로 다른 관점에서 비교할 이유가 됩니다. 다만 점수 차이만으로 이 시장 자료 과제의 우위나 통계적 유의성을 확정할 수 없습니다.

이 단계에서는 PoC의 비교 대상 2개를 확정했습니다. 추가 모델을 늘리지 않고, 영문 본 비교와 같은 사실의 한국어 일부 비교에서 정확성, 회고와 실행 비용을 확인하도록 범위를 정했습니다. 두 후보 모두 사전 기준에 미달할 가능성도 허용했습니다.

후속 [영문 호출 및 저장 점검](2026-09-15_KAN-200_english-warmup.md)과 [로컬 본 시험](2026-09-15_KAN-202_local-poc.md)을 완료했습니다. 공개 점수로 정한 후보가 이 과제에서도 기준을 충족했는지는 [종합 보고서](2026-09-15_KAN-205_poc-conclusion.md)의 결과로 판단합니다.

## 부록 A. 이번 설치 조회 원본

<details>
<summary>JSON 원본 기록 펼치기</summary>

```json
{
  "checked_at": "2026-09-15T10:59:57.076296+00:00",
  "python": "3.12.13 (main, Jul 18 2026, 17:08:38) [MSC v.1944 64 bit (AMD64)]",
  "ollama": {
    "version": "0.33.3"
  },
  "models": [
    {
      "tag": "qwen3.5:9b",
      "digest": "6488c96fa5faab64bb65cbd30d4289e20e6130ef535a93ef9a49f42eda893ea7",
      "file_size_bytes": 6594474711,
      "details": {
        "parent_model": "",
        "format": "gguf",
        "family": "qwen35",
        "families": [
          "qwen35"
        ],
        "parameter_size": "9.7B",
        "quantization_level": "Q4_K_M"
      },
      "capabilities": [
        "completion",
        "vision",
        "tools",
        "thinking"
      ],
      "parameters": "presence_penalty               1.5\ntemperature                    1\ntop_k                          20\ntop_p                          0.95",
      "model_info": {
        "general.architecture": "qwen35",
        "general.file_type": 15,
        "general.parameter_count": 9653104368,
        "qwen35.context_length": 262144
      },
      "template_sha256": "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315",
      "license_sha256": "7339fa418c9ad3e8e12e74ad0fd26a9cc4be8703f9c110728a992b193be85cb2",
      "license_first_line": [
        "Apache License"
      ]
    },
    {
      "tag": "gemma4:12b",
      "digest": "4eb23ef187e2c5462566d6a1d3bbbc2f1346d0b4327cbb66d58fffbcc9b2b05c",
      "file_size_bytes": 7556508396,
      "details": {
        "parent_model": "",
        "format": "gguf",
        "family": "gemma4",
        "families": [
          "gemma4"
        ],
        "parameter_size": "11.9B",
        "quantization_level": "Q4_K_M"
      },
      "capabilities": [
        "completion",
        "vision",
        "audio",
        "tools",
        "thinking"
      ],
      "parameters": "temperature                    1\ntop_k                          64\ntop_p                          0.95",
      "model_info": {
        "gemma4.context_length": 262144,
        "general.architecture": "gemma4",
        "general.file_type": 15,
        "general.parameter_count": 11907350576
      },
      "template_sha256": "b507b9c2f6ca642bffcd06665ea7c91f235fd32daeefdf875a0f938db05fb315",
      "license_sha256": "0d542e0c8804e39aa7f37eb00da5a762149dc682d7829451287e11b938e94594",
      "license_first_line": [
        "Apache License"
      ]
    }
  ],
  "loaded_models": [],
  "gpu": "NVIDIA GeForce RTX 5070 Ti Laptop GPU, 12227 MiB, 610.88"
}
```

</details>

## 부록 B. 코드와 기존 실험의 식별 정보

<details>
<summary>JSON 원본 기록 펼치기</summary>

```json
{
  "git_head": "78515cf9d6b652673dffb09eb96f2f15a194c1d8",
  "uncommitted_changes_present": true,
  "current_code_sha256": {
    "modules/evaluation_report.py": "0320b9064db6ac7079fa21af33553143d3a5b4aac4da8ec19829d4af05034841",
    "modules/evaluation_local.py": "ac7be82075fa9db5557b261da5225cc68da1f126a5565397f063d8c7b60395b4",
    "modules/decision_openai.py": "5e3136bffbcf4a712b34e724b9ce8ed5b9a02474174f78049648dcbc24c1858e",
    "uv.lock": "42cd76c38daa17dae7b0a24a38ff4453769e88455d04a7910e271fb8e5abc78b"
  },
  "prior_experiment_id": "schema-mode-20260915T060154Z",
  "prior_config_hash": "51643081c790c54829538d43c9fad75af960887b0e6a64ec580e0990859a946f",
  "prior_dataset_hash": "059fb83a6b07b7e1945f51506783f44c1f348275899603f5230bc87619bb9dec",
  "prior_execution_source_hashes": {
    "modules/data_evaluation_cases.py": "538efcbee4b106c2b8282cf694df895a95923372815ebc4e64867b8035250eb1",
    "modules/evaluate_response.py": "fe8eb7e18e9a42553f064c5570a397fc91e55918cc679b18c051e2e176238603",
    "modules/evaluation_cases.json": "f9449a31595954ea52b123b96b0b80ff6dab795a5d87063fe459f7465a9ec066",
    "modules/evaluation_cloud.py": "a2c50b76491bc92540c530913e9500c25538fc491d9d1473d49efad211033fa0",
    "modules/evaluation_local.py": "ac7be82075fa9db5557b261da5225cc68da1f126a5565397f063d8c7b60395b4",
    "modules/evaluation_prompt.py": "00a06ca93b75c578e688b9cbb47919e68a72ea5c785ff888e4e301b2a5d75ccd",
    "modules/evaluation_runner.py": "ea501acc90bb40fc3b646e586356709600e24c2ada1a7a2faa8d50d15cdd820f",
    "modules/evaluation_storage.py": "c7b433a94cbdc573c1447bcee81ea1a7f65181a66e7bda00ca0a7d8f684548ce",
    "modules/evaluation_variants.py": "c3b2a706fbb28d6e5fb3b292b4174d4e31ffd152f857998533dbf13d17ffef03",
    "uv.lock": "42cd76c38daa17dae7b0a24a38ff4453769e88455d04a7910e271fb8e5abc78b"
  }
}
```

</details>

이 보고서 작성 과정에서 생성 API를 호출하지 않았으며, 기존 DB의 응답을 수정하거나 새 PoC 결과로 이동하지 않았습니다. 기존 원본을 이용한 재집계와 문서 작성의 결과입니다.
