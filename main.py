"""
가상 비트코인 자료를 이용한 LLM 비교 실험의 실행 파일

이 파일의 역할
    시험할 모델과 실행 조건을 선택하고, 문항 준비부터 결과 저장까지 전체 실행 순서를 관리하는 역할.

    실제 모델 통신, 응답 검사와 CSV 저장은 각 기능을 담당하는 modules의 함수를 호출하여 수행.

이 파일에 정의된 함수
    1. load_cases()
       cases.json에서 가상 문항과 정답 및 채점 기준을 읽는 함수.
       중복 문항, 필수 설명 항목 수와 채점 근거의 입력 경로 검사.
       모델에 전달할 입력과 검토에 사용할 정답을 포함한 자료 반환.

    2. trial_settings()
       선택한 로컬 모델과 시험 조건에 맞는 실행 설정을 만드는 함수.
       기본 설정 또는 권장 설정, 추론 사용 여부에 따라
       문맥 크기, 생성 한도, 토큰 선택 설정과 호출 제한 시간 구성.

    3. _prepare_trial()
       실제 호출에 앞서 선택한 문항과 실행 환경을 확인하는 함수.
       입력 언어, 문항 목록, 반복 횟수와 사용자 지정 설정 검사.
       로컬 모델의 설치 및 적재 상태 또는 외부 API 키 설정 여부 확인.
       이후 호출과 저장에 사용할 시험 정보 반환.

    4. _call_arguments()
       현재 문항과 시험 설정을 모델 호출 함수의 인자로 구성하는 함수.
       공통 지시문, 모델 입력, 출력 형식과 생성 설정 연결.
       정답과 채점 기준은 모델에 전달하는 메시지에서 제외.

    5. run_trial()
       준비된 문항을 순서대로 호출하고 결과를 저장하는 핵심 함수.
       모델 호출 후 JSON 형식과 거래 규칙 검사.
       입력, 응답, 측정값과 검사 결과를 호출마다 CSV 한 행으로 저장.
       현재 결과의 저장을 마친 뒤 다음 문항 진행.

    6. main()
       터미널에서 전달한 실행 옵션을 읽는 시작 함수.
       모델, 시험 조건, 언어, 반복 횟수와 문항 등의 옵션 해석.
       해석한 값을 run_trial()에 전달하여 실험 시작.

실행 순서
    이 파일을 직접 실행한 경우 main() 호출.
    main()에서 실행 옵션을 읽은 뒤 run_trial() 호출.
    run_trial()에서 시험 준비와 CSV 생성 후 다음 과정을 반복.

        문항 입력 구성 → 모델 호출 → 응답 검사 → 결과 한 행 저장
"""

# --model 같은 명령행 옵션을 읽고 도움말을 만드는 argparse 표준 모듈 가져오기
import argparse
# JSON 문자열과 Python 딕셔너리 사이의 변환을 위한 json 모듈 가져오기
import json
# 호출 전후의 경과 시간을 초 단위로 측정하기 위한 time 모듈 가져오기
import time
# 실행 날짜와 UTC 시간대를 함께 기록하기 위한 날짜 및 시간대 도구 가져오기
from datetime import datetime, timezone
# 문자열 경로를 파일과 폴더 경로 객체로 다루기 위한 Path 가져오기
from pathlib import Path
# 언어별 공통 지시문과 모델에 보낼 메시지를 만드는 함수 가져오기
from modules.prompt import INSTRUCTIONS, build_messages
# 모델에 요구할 JSON 구조와 실제 반환값을 검사하는 함수 가져오기
from modules.validate_response import get_response_schema, validate_response
# Ollama 요청 구성, 실제 호출, 설치 정보 조회를 각각 담당하는 함수 가져오기
from modules.call_ollama import build_ollama_request, call_ollama, get_ollama_info
# CSV 파일 생성기와 파일명, JSON 출력, 호출 한 행 구성 도구 가져오기
from modules.save_csv import TrialCsv, csv_filename, json_text, result_row
# 외부 API 호출 모듈을 cloud라는 짧은 이름으로 사용하기 위한 별칭 지정
from modules import call_openai as cloud
# 변경하지 않는 모델 이름 두 개를 튜플에 보관, 첫 항목은 Qwen이고 둘째 항목은 Gemma인 구성
MODELS = ("qwen3.5:9b", "gemma4:12b")
# 생성 옵션을 이름과 값의 쌍인 딕셔너리로 보관, 등장 및 빈도 기반 반복 억제값을 0으로 설정
OPTIONS = {"presence_penalty": 0.0, "frequency_penalty": 0.0,
           # 반복 페널티 1, 후보 토큰 수 40, 누적 확률 0.95, 상대 확률 제한 0의 기본값 지정
           "repeat_penalty": 1.0, "top_k": 40, "top_p": 0.95, "min_p": 0.0}
# 실행 폴더와 무관하게 현재 main.py가 있는 폴더를 기준으로 cases.json의 절대 경로 구성
CASE_PATH = Path(__file__).resolve().parent / "modules" / "cases.json"


# 문항 파일을 읽고 입력과 채점 기준의 기본 구조를 검사하는 함수 정의
def load_cases():
    """
    가상 문항과 채점 기준 읽기, 문항 및 언어 중복 확인

    입력: 파일 위치가 고정된 cases.json
    반환: 문항별 입력, 평가자 전용 정답과 Cloud 문항 목록
    검사: 문항과 언어 중복, 필수 세 항목과 채점 근거 경로
    예외: 중복 문항 또는 근거 누락 시 실행 전 중단
    """
    # UTF-8 파일 내용을 문자열로 읽은 뒤 json.loads로 Python 딕셔너리로 변환
    data = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    # 이미 확인한 문항과 언어의 조합을 쌓을 빈 리스트 생성
    ids = []
    # cases 목록에서 문항 하나를 case 변수에 넣으며 차례대로 반복
    for case in data["cases"]:
        # 문항 번호와 언어를 한 쌍으로 묶어 같은 문항의 언어별 구분값 생성
        identity = (case["case_id"], case["language"])
        # 현재 문항과 언어 조합이 앞서 처리한 목록에 있는지 확인
        if identity in ids:
            # 중복 문항을 발견한 경우 ValueError 예외를 발생시켜 실행 준비 중단
            raise ValueError("중복 문항과 언어 확인 필요")
        # 검사를 통과한 조합을 리스트 끝에 추가해 이후 중복 검사에 사용
        ids.append(identity)
        # 모델에 요구하는 필수 설명 목록의 길이가 정확히 3인지 확인
        if len(case["input"]["required_observations"]) != 3:
            # 설명 항목 수가 맞지 않는 경우 문항 구성 오류로 처리
            raise ValueError("문항별 필수 확인 사항 세 개 필요")
        # 연결 점검 문항 W00을 제외한 본 시험 문항에 채점 기준 검사 적용
        if case["case_id"] != "W00":
            # rubric 키의 채점 기준 목록을 조회, 키가 없으면 빈 리스트 사용
            rubric = case.get("rubric", [])
            # 리스트 내포로 항목 번호만 뽑아 R01, R02, R03 순서와 일치하는지 확인
            if [r["id"] for r in rubric] != ["R01", "R02", "R03"]:
                # 채점 항목 번호 또는 순서가 다른 경우 준비 중단
                raise ValueError("세 항목의 순서와 채점 근거 필요")
            # 문항의 세 채점 항목을 하나씩 item 변수로 조회
            for item in rubric:
                # 기대 설명이나 근거 입력 경로가 비어 있는지 확인
                if not item["expected"] or not item["source_paths"]:
                    # 정답 설명 또는 근거 경로가 없는 경우 오류 발생
                    raise ValueError("정답 또는 원본 경로 누락")
                # 한 채점 항목에 연결된 원본 입력 경로를 하나씩 확인
                for path in item["source_paths"]:
                    # 경로 탐색의 출발점을 현재 문항의 input 딕셔너리로 설정
                    value = case["input"]
                    # market_input.ohlcv.day.0처럼 점으로 연결한 경로를 나누어 한 단계씩 탐색
                    for part in path.split("."):
                        # 현재 값이 리스트이면 경로를 정수 인덱스로 변환, 딕셔너리이면 문자열 키로 접근
                        value = value[int(part)] if isinstance(value, list) else value[part]
    # 모든 검사를 통과한 문항 파일 전체를 호출한 곳에 반환
    return data


# 모델명과 시험 조건에 맞는 호출 설정 딕셔너리를 만드는 함수 정의
def trial_settings(model, condition):
    """기존 시험에서 사용한 조건의 명시적 선택, 최신 권장값의 자동 갱신 없음.

    기본 조건: 문맥 8,192, 생성 2,048, temperature 0, 제한 300초.
    권장 조건: 2026-09-16 시험의 모델별 샘플링과 자체 실행 한도 유지.
    권장이라는 이름은 무제한 실행이나 노트북 최대 용량을 뜻하지 않는 구분.
    """
    # 지원하는 모델 목록과 실행 조건 목록에 각각 포함되는지 확인
    if model not in MODELS or condition not in CONDITIONS:
        # 알 수 없는 모델이나 조건을 받은 경우 설정 생성 중단
        raise ValueError("지원하는 모델과 실행 조건 필요")
    # 조건 이름 끝의 -thinking 여부를 True 또는 False로 계산, nonthinking과 구분
    thinking = condition.endswith("-thinking")
    # 로컬 공급자와 기본 문맥 창 8,192, 생성 한도 2,048을 결과 사전에 지정
    result = {"provider": "ollama", "num_ctx": 8192, "num_predict": 2048,
              # 추론 여부와 온도 및 300초 제한 지정, dict 복사로 공통 OPTIONS의 직접 변경 방지
              "thinking": thinking, "temperature": 0.0, "timeout": 300, "options": dict(OPTIONS)}
    # 조건 이름이 recommended로 시작하면 모델별 권장 설정 적용
    if condition.startswith("recommended"):
        # 기본 300초를 답변 완료 관찰용 1,800초로 변경
        result.update(timeout=1800)
        # 튜플의 첫 모델인 Qwen에 해당하는지 확인
        if model == MODELS[0]:
            # Qwen의 문맥과 생성 한도 지정, 추론이면 온도 1.0이고 비추론이면 0.7인 조건식 적용
            result.update(num_ctx=131072, num_predict=32768, temperature=1.0 if thinking else 0.7)
            # Qwen의 모드별 top_p와 공통 top_k 및 반복 억제 옵션 변경
            result["options"].update(top_p=0.95 if thinking else 0.8, top_k=20, presence_penalty=1.5)
        # 지원 모델 중 Qwen이 아닌 Gemma에 대한 분기
        else:
            # Gemma의 문맥 32,768, 생성 16,384와 온도 1.0 설정
            result.update(num_ctx=32768, num_predict=16384, temperature=1.0)
            # Gemma의 누적 확률과 후보 토큰 개수 설정
            result["options"].update(top_p=0.95, top_k=64)
    # 모델별 조정이 끝난 설정 사전 반환
    return result


# 명령행과 설정 검사에서 공통으로 허용할 네 가지 실행 조건을 튜플로 정의
CONDITIONS = ("basic-nonthinking", "basic-thinking", "recommended-nonthinking", "recommended-thinking")


# 문항 선택, 설정 검증과 호출 전 준비를 담당하는 내부 함수 정의
def _prepare_trial(model, condition, language, repeat, case_ids, overrides):
    """
    실행 전에 문항, 설정과 설치 모델을 확인하는 함수

    입력: 모델과 조건, 언어, 반복 수, 선택 문항과 실행 한도 변경값
    반환: 한 모델의 설정과 문항을 직접 담은 시험 정보 사전
    검증: 지원 조건, 문항 누락 및 중복, W00 분리, 양의 정수 한도
    효과: 로컬 설치 정보 또는 Cloud 키 설정 여부 확인, 모델 생성 요청 없음
    이유: 문항과 정답을 CSV에 함께 기록하되 정답의 실제 전송은 차단할 필요
    """
    # 선택한 모델이 외부 API 모델인지 비교한 참거짓 값 저장
    is_cloud = model == cloud.LUNA_MODEL
    # 요청 언어가 영문 en 또는 한국어 ko인지 확인
    if language not in ("en", "ko"):
        # 지원하지 않는 언어 코드의 실행 차단
        raise ValueError("en 또는 ko 언어 필요")
    # 가상 문항과 정답 파일을 읽고 구조 검사까지 완료한 자료 확보
    source = load_cases()
    # 외부 API를 사용하는 경우에만 적용할 조건 검사 시작
    if is_cloud:
        # Cloud 전용 조건과 영문 사용 여부 확인, 임의 문항 선택 및 로컬 문맥 옵션 지정 차단
        if condition != "cloud" or language != "en" or case_ids is not None or overrides["num_ctx"] is not None:
            # 외부 API 비교 범위를 벗어난 설정을 받은 경우 오류 발생
            raise ValueError("Cloud는 사전 지정 영문 다섯 문항, 로컬 문맥 옵션 제외")
        # 반복 생략 또는 1회만 허용, Python에서 정수와 유사하게 취급하는 bool 값도 별도 제외
        if repeat not in (None, 1) or type(repeat) is bool:
            # 외부 API를 임의로 반복 호출하지 않도록 준비 단계에서 중단
            raise ValueError("Cloud는 문항별 한 번 호출")
        # 키 내용 대신 API 키 설정 여부만 확인
        if not cloud.luna_key_available():
            # 키가 준비되지 않은 경우 실제 요청 전 오류 발생
            raise ValueError("개인 API 키 설정 필요")
        # 문항 파일에 미리 지정된 Cloud 비교 문항 목록 선택
        case_ids = source["cloud_case_ids"]
        # 외부 공급자, 생성 한도 2,048과 추론 해제 설정 지정
        settings = {"provider": "openai", "num_predict": 2048, "reasoning_effort": "none",
                    # 외부 API의 온도 0과 전체 호출 제한 300초 지정
                    "temperature": 0.0, "timeout": 300}
    # 외부 API가 아닌 로컬 모델에 대한 준비 분기
    else:
        # 선택한 로컬 모델과 조건에 맞는 설정 조회
        settings = trial_settings(model, condition)
    # 문맥, 생성 및 시간 한도의 사용자 지정값을 이름과 값으로 하나씩 조회
    for name, value in overrides.items():
        # None은 미지정 상태이므로 실제 값이 있을 때만 설정 변경
        if value is not None:
            # 사용자 지정 한도가 양의 정수인지 검사, bool과 실수 및 0 이하 값의 제외
            if type(value) is not int or value <= 0:
                # 잘못된 한도의 이름을 오류 메시지에 포함해 실행 중단
                raise ValueError("양의 정수 한도 필요: " + name)
            # 검증된 지정값으로 같은 이름의 기본 설정 덮어쓰기
            settings[name] = value
    # 로컬에서 생성 한도가 문맥 창 이상인지 검사해 입력 공간 부족 방지
    if not is_cloud and settings["num_predict"] >= settings["num_ctx"]:
        # 생성에만 문맥 전체를 사용할 수 있는 설정의 실행 차단
        raise ValueError("입력 공간 확보를 위해 생성 한도보다 큰 문맥 창 필요")

    # 언어와 문항 ID를 함께 확인, 지정하지 않은 W00의 본 시험 혼입 방지
    # 문항 목록이 비었거나 set으로 중복을 제거한 길이가 원래 길이와 다른지 확인
    if case_ids is not None and (not case_ids or len(case_ids) != len(set(case_ids))):
        # 비어 있지 않고 중복도 없는 문항 선택 요구
        raise ValueError("중복 없는 문항 목록 필요")
    # 리스트 내포로 전체 문항 중 요청한 언어와 일치하는 문항 선택
    cases = [case for case in source["cases"] if case["language"] == language
             # 문항 번호를 지정하면 해당 문항만 선택, 생략하면 W00을 제외한 본 시험 선택
             and (case["case_id"] in case_ids if case_ids is not None else case["case_id"] != "W00")]
    # 선택 결과가 없거나 요청한 문항 중 실제 파일에서 찾지 못한 항목이 있는지 검사
    if not cases or (case_ids is not None and {c["case_id"] for c in cases} != set(case_ids)):
        # 일부 문항만 조용히 실행되는 일을 막기 위한 문항 누락 오류 발생
        raise ValueError("선택한 언어에 해당하는 모든 문항 필요")
    # any로 W00 포함 여부 확인, 다른 문항과 함께 선택되면 오류 처리 대상 지정
    if any(c["case_id"] == "W00" for c in cases) and len(cases) != 1:
        # 준비 점검 결과가 본 시험 CSV에 섞이지 않도록 실행 차단
        raise ValueError("W00은 본 시험과 별도 CSV로 실행")
    # 첫 문항이 W00이면 warmup, 그 외에는 main이라는 내부 단계 이름 지정
    phase = "warmup" if cases[0]["case_id"] == "W00" else "main"
    # 사용자가 반복 횟수를 지정하지 않은 경우에만 기본 횟수 계산
    if repeat is None:
        # Cloud, 권장 설정과 W00은 1회, 기본 설정 본 시험은 2회로 설정
        repeat = 1 if is_cloud or condition.startswith("recommended") or phase == "warmup" else 2
    # 최종 반복 횟수가 1 이상의 정수인지 검사
    if type(repeat) is not int or repeat < 1:
        # 0회, 음수와 비정수 반복의 실행 차단
        raise ValueError("양의 반복 횟수 필요")

    # 로컬 실행인 경우에만 Ollama 설치 및 메모리 적재 상태 확인
    if not is_cloud:
        # 설치 목록을 모델 이름으로 조회할 수 있는 딕셔너리로 변환
        installed = {row["name"]: row for row in get_ollama_info("/api/tags")["models"]}
        # 선택한 모델 이름이 Ollama 설치 목록에 존재하는지 확인
        if model not in installed:
            # 미설치 모델 이름을 포함한 오류로 자동 다운로드 대신 준비 중단
            raise ValueError("모델 미설치: " + model)
        # 현재 메모리에 적재된 모델 목록이 비어 있지 않은지 확인
        if get_ollama_info("/api/ps").get("models"):
            # 다른 적재 상태가 측정에 영향을 주지 않도록 호출 전 중단
            raise RuntimeError("시험 시작 전 다른 적재 모델 확인 필요")
    # 파일명에 사용할 시작 시각만 생성, 별도 실험 식별값 생성 없음
    # UTC 현재 시각을 마이크로초까지 포함한 ISO 형식 문자열로 기록
    started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    # 후속 호출과 저장에서 공유할 실행 준비 정보를 딕셔너리로 반환 시작
    return {
        # 시험 시작 시각과 모델 이름 보관
        "started_at": started_at, "model": model,
        # 시험 조건과 언어, 단계 및 반복 횟수 보관
        "condition": condition, "language": language, "phase": phase, "repeat": repeat,
        # 검증된 설정과 선택한 문항 목록 보관
        "settings": settings, "cases": cases,
        # 선택한 언어의 공통 지시문과 새 JSON 출력 스키마 보관
        "instruction": INSTRUCTIONS[language], "response_schema": get_response_schema(),
    # 준비 정보 딕셔너리 구성 종료
    }


# 한 문항의 실제 호출에 전달할 인자 사전을 만드는 내부 함수 정의
def _call_arguments(trial, case):
    """
    요청 사전 구성과 실제 호출에서 공통으로 사용할 인자 생성

    입력: 고정된 시험 설정과 현재 문항
    반환: 해당 공급자의 요청 생성 함수와 호출 함수에 공통 전달할 인자 사전
    제외: expected와 rubric 등 평가자 전용 정답, 이전 호출의 대화 내용
    이유: 같은 설정을 두 군데서 별도로 조립할 때 발생하는 인자 불일치 방지
    구분: timeout은 요청 본문이 아닌 호출 제어 인자이므로 실행 시 별도 전달
    """
    # 여러 번 사용할 실행 설정을 짧은 지역 변수로 조회
    settings = trial["settings"]
    # 두 공급자에 공통으로 전달할 호출 인자 사전 구성 시작
    arguments = {
        # 정답을 제외한 input과 언어로 system 및 user 메시지 생성
        "messages": build_messages(case["input"], trial["language"]),
        # 공통 JSON 출력 구조와 해당 조건의 temperature 전달 준비
        "output_format": trial["response_schema"], "temperature": settings["temperature"],
    # 공통 인자 사전 구성 종료
    }
    # 외부 API의 인자 이름을 사용해야 하는지 확인
    if settings["provider"] == "openai":
        # OpenAI 호출 함수가 받는 생성 한도와 추론 수준 인자 추가
        arguments.update(max_output_tokens=settings["num_predict"], reasoning_effort=settings["reasoning_effort"])
    # Ollama 호출 함수의 인자를 구성하는 분기
    else:
        # 로컬 모델 이름과 문맥 창 및 생성 한도 추가
        arguments.update(model=trial["model"], num_ctx=settings["num_ctx"], num_predict=settings["num_predict"],
                         # 추론 여부와 추가 토큰 선택 옵션 추가, 이전 줄에서 시작한 update 호출 종료
                         thinking=settings["thinking"], extra_options=settings["options"])
    # 요청 구성 함수와 실제 호출 함수가 함께 사용할 인자 반환
    return arguments


# 한 시험 전체를 실행하는 함수 정의, * 뒤의 옵션은 이름을 붙여 전달하는 키워드 전용 인자
def run_trial(model, condition="basic-nonthinking", *, language="en", repeat=None,
              # 문항 목록, 저장 폴더와 한도 변경값의 기본값 지정, None은 미지정 의미
              case_ids=None, output_dir="data", num_ctx=None, num_predict=None, timeout=None):
    """
    모델 하나와 설정 하나의 시험을 실행하고 CSV 한 개로 저장하는 진입 함수

    입력: 모델, 조건, 언어와 문항, 반복 수, 출력 폴더 및 선택적인 실행 한도
    반환: 생성한 CSV 파일의 Path 객체
    순서: CSV 열 준비 → 모델 호출 → 자동 검사 → 호출당 한 행 저장
    저장: 각 결과의 저장을 마친 뒤 다음 문항 호출, 기존 CSV 덮어쓰기 없음
    실패: 처리 가능한 호출 예외도 오류와 측정값 보존, 저장 실패 시 다음 호출 중단
    한계: 프로세스 강제 종료로 반환받지 못한 호출은 결과 행 저장 불가
    채점: 출력 형식과 거래 제한만 자동 확인, 설명의 사실 점수는 CSV에서 작성
    반복: 매 요청에 현재 문항만 전달, 과거 대화 연결과 자동 재시도 없음
    """
    # 파일 생성과 모델 호출에 앞서 공통 준비 검사 수행
    trial = _prepare_trial(model, condition, language, repeat, case_ids,
                           # 선택적인 문맥, 생성 및 시간 한도 변경값을 이름별 사전으로 전달
                           {"num_ctx": num_ctx, "num_predict": num_predict, "timeout": timeout})
    # 완성된 실행 정보에서 실제 적용할 설정 조회
    settings = trial["settings"]
    # 공급자에 따라 요청 구성과 호출 함수를 선택하기 위한 bool 생성
    is_cloud = settings["provider"] == "openai"
    # 공급자별 차이는 인자와 호출 함수 선택으로 한정, 저장 흐름은 동일하게 유지
    # 함수를 실행하지 않고 공급자에 맞는 요청 구성 함수 자체를 변수에 보관
    request_builder = cloud.build_luna_request if is_cloud else build_ollama_request
    # 공급자에 맞는 실제 호출 함수를 caller 변수로 선택
    caller = cloud.call_luna if is_cloud else call_ollama
    # 모델, 조건과 언어 및 단계로 CSV 파일명 생성 시작
    filename = csv_filename(model, condition, language, trial["phase"],
                            # 생성 한도와 시작 시각까지 파일명 함수에 전달
                            settings["num_predict"], trial["started_at"])
    # 출력 폴더와 파일명을 결합하고 CSV를 먼저 생성해 파일명 충돌 검사
    writer = TrialCsv(Path(output_dir) / filename, trial)
    # 저장 위치와 문항 수 곱하기 반복 수를 출력, flush로 화면에 즉시 반영
    print(json_text({"csv": str(writer.path), "planned_calls": len(trial["cases"]) * trial["repeat"]}), flush=True)

    # range의 끝값은 제외되므로 반복 횟수에 1을 더해 1회차부터 순서대로 반복
    for number in range(1, trial["repeat"] + 1):
        # 현재 회차에서 선택한 모든 문항을 하나씩 실행
        for case in trial["cases"]:
            # 현재 문항의 입력과 공통 설정으로 호출 인자 구성
            arguments = _call_arguments(trial, case)
            # **로 사전 항목을 이름 있는 인자로 펼쳐 실제 전송 예정 본문 생성
            request = request_builder(**arguments)
            # 호출 시작 시각을 UTC 문자열로 기록
            started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            # 시스템 시각 변경의 영향을 적게 받는 고해상도 경과 시간 기준 확보
            started = time.perf_counter()
            # 사용자 중단 여부를 나타내는 값을 우선 False로 초기화
            interrupted = False
            # 호출 중 예외가 발생해도 실패 정보를 저장하기 위한 try 구간 시작
            try:
                # 선택한 모델 호출 함수에 인자와 시간 제한을 전달하고 결과 기록 수신
                record = caller(**arguments, timeout=settings["timeout"])
            # 일반 오류와 Ctrl+C 중단을 잡아 error 변수로 확보
            except (Exception, KeyboardInterrupt) as error:
                # 호출 함수가 기록을 반환하지 못한 예외의 구분, 없는 부분 응답의 생성 금지
                # 사용자 중단인 KeyboardInterrupt와 일반 실패 구분
                interrupted = isinstance(error, KeyboardInterrupt)
                # 호출 함수가 결과를 반환하지 못했을 때 사용할 실패 기록 구성 시작
                record = {"started_at": started_at,
                          # 실패 확인 시각과 시도했던 요청 본문 보존
                          "finished_at": datetime.now(timezone.utc).isoformat(), "request": request,
                          # 중단 또는 실패 상태를 구분하고 받지 못한 최종 답변과 추론은 빈 문자열로 표시
                          "status": "interrupted" if interrupted else "failed", "content": "", "thinking": "",
                          # 최종 답변 미완료 표시와 오류 발생까지의 경과 시간 기록
                          "final_response_complete": False, "wall_seconds": time.perf_counter() - started,
                          # 예외 클래스 이름과 오류 내용을 문자열로 기록
                          "errors": [{"type": type(error).__name__, "detail": str(error),
                                      # 호출 함수에서 부분 원문을 돌려받지 못했다는 설명을 오류에 포함
                                      "note": "No response record returned; partial content is unavailable."}]}
            # 공급자 응답에 누락된 호출 시각만 보완, 모델 출력과 측정값의 추정 없음
            # 반환 기록에 시작 시각이 없을 때만 보완, 이미 있는 값은 유지
            record.setdefault("started_at", started_at)
            # 반환 기록에 종료 시각이 없을 때만 현재 시각으로 보완
            record.setdefault("finished_at", datetime.now(timezone.utc).isoformat())
            # 자동 검사 결과와 검사기 자체 오류를 담을 빈 사전 생성
            evaluation = {}
            # 응답 검사 중 발생한 오류도 모델 원문과 함께 저장하기 위한 예외 처리 시작
            try:
                # 입력과 최종 응답 및 평가자 전용 expected로 JSON과 거래 규칙 검사, 설명 의미의 자동 채점 제외
                evaluation["assessment"] = validate_response(case["input"], record.get("content", ""), case["expected"])
            # 검사기에서 예상하지 못한 예외가 발생한 경우 별도 수신
            except Exception as error:
                # 검사기 오류와 모델 응답 실패를 구분, 검사 예외로 인한 원문 유실 방지
                # 검사기의 오류 설명을 저장해 모델 호출 실패와 구분
                evaluation["grading_error"] = str(error)
            # 호출 함수가 기록한 실제 요청과 앞서 만든 요청의 사전 내용 비교
            mismatch = record.get("request") != request
            # 요청이 서로 다른 경우에만 불일치 표시 처리
            if mismatch:
                # CSV 오류 열에 요청 불일치를 남기기 위한 표시 저장
                evaluation["request_mismatch"] = True
            # 실제 호출의 검사 결과를 CSV와 화면에 함께 표시, 별도 검사 실행 불필요
            # 실제 입력, 응답, 측정값과 검사 결과를 CSV 한 행의 사전으로 변환
            row = result_row(record, case, trial, evaluation)
            # 다음 호출 전에 현재 결과를 CSV에 저장, 저장 실패 시 예외로 중단
            writer.append(row)
            # 처리한 문항, 반복 회차와 호출 상태를 화면에 출력하는 사전 구성
            print(json_text({"case": case["case_id"], "repeat": number, "status": record.get("status"),
                             # 최종 응답 완료 여부를 형식 검사와 구분해 화면에 표시
                             "final_response_complete": record.get("final_response_complete"),
                             # 다섯 필드 형식 검사와 거래 제한 검사 결과 표시
                             "json_valid": row["json_valid"], "trading_valid": row["trading_valid"],
                             # 실패 또는 형식 오류의 구체적인 이유 표시
                             "errors": row["error_message"],
                             # 응답 시간과 저장 파일 위치 표시, flush로 출력 지연 방지
                             "seconds": record.get("wall_seconds"), "saved": str(writer.path)}), flush=True)
            # 사용자가 중단한 호출이었다면 저장 후 중단 신호 다시 전달
            if interrupted:
                # KeyboardInterrupt를 다시 발생시켜 다음 문항의 실행 방지
                raise KeyboardInterrupt
            # 실제 요청 불일치가 확인된 경우 다음 호출 차단
            if mismatch:
                # 현재 기록은 보존한 상태에서 요청 불일치 오류 발생
                raise RuntimeError("실제 요청과 계획 불일치, 원본 저장 후 다음 호출 중단")
            # 모델 해제 실패나 인증, 결제, 권한 또는 호출 제한 오류가 하나라도 있는지 확인
            if any(e.get("type") == "unload" or e.get("status") in (401, 402, 403, 429) for e in record.get("errors", [])):
                # 실행 환경 또는 API 접근 문제를 해결하기 전 추가 호출 중단
                raise RuntimeError("모델 해제 또는 API 접근 오류, 다음 호출 중단")
    # 모든 문항 저장을 마친 결과 CSV의 Path 객체 반환
    return writer.path


# 명령행 입력을 읽고 전체 시험 함수에 전달하는 진입 함수 정의
def main(argv=None):
    """
    명령행 인자를 읽고 한 시험을 실행하는 진입점

    필수: --model과 --condition으로 대상 모델과 설정 선택
    선택: 언어, 반복 수, 문항 목록, 문맥 및 생성 한도와 호출 제한
    저장: --output-dir 미지정 시 현재 실행 폴더의 data 하위 CSV 생성
    처리: 실제 검증과 호출은 run_trial에 위임, 실패 시 오류를 호출자에 전달
    """
    # 사용법 안내와 옵션 해석을 담당할 ArgumentParser 객체 생성
    parser = argparse.ArgumentParser(description="모델과 조건별 CSV 한 개에 입력, 응답, 측정값 저장")
    # 모델 선택을 필수 옵션으로 등록, *MODELS로 로컬 두 모델과 Luna를 허용값에 결합
    parser.add_argument("--model", required=True, choices=(*MODELS, "gpt-5.6-luna"))
    # 실행 조건을 필수 옵션으로 등록, 로컬 네 조건과 cloud만 허용
    parser.add_argument("--condition", required=True, choices=(*CONDITIONS, "cloud"))
    # 언어 옵션은 생략 시 영문 사용, en과 ko만 허용
    parser.add_argument("--language", default="en", choices=("en", "ko"))
    # 반복 횟수 입력을 정수로 변환하도록 등록
    parser.add_argument("--repeat", type=int)
    # --cases 뒤에 공백으로 구분한 문항 번호를 하나 이상 받을 목록 옵션 등록
    parser.add_argument("--cases", nargs="+")
    # 문맥 창 변경 옵션을 정수로 등록, 미지정 시 None 유지
    parser.add_argument("--num-ctx", type=int)
    # 생성 한도 변경 옵션을 정수로 등록
    parser.add_argument("--num-predict", type=int)
    # 전체 호출 제한 시간을 초 단위 정수로 등록
    parser.add_argument("--timeout", type=int)
    # 저장 폴더를 선택하는 옵션 등록, 기본 위치는 현재 실행 폴더의 data
    parser.add_argument("--output-dir", default="data")
    # 옵션을 해석해 속성으로 조회 가능한 args 객체 생성, argv가 None이면 실제 명령행 사용
    args = parser.parse_args(argv)
    # 모델, 조건, 언어와 반복 횟수를 전체 실행 함수에 전달
    run_trial(args.model, args.condition, language=args.language, repeat=args.repeat,
              # 선택 문항과 문맥 및 생성 한도 변경값 전달
              case_ids=args.cases, num_ctx=args.num_ctx, num_predict=args.num_predict,
              # 호출 제한과 저장 폴더를 전달하며 전체 실행 함수 호출 완료
              timeout=args.timeout, output_dir=args.output_dir)


# 다른 모듈에서 import할 때는 실행하지 않고 직접 실행한 경우에만 진입하는 보호 조건
if __name__ == "__main__":
    # 명령행 처리 시작, Windows 자식 프로세스의 무한 재실행 방지에 필요한 진입점 보호 적용
    main()
