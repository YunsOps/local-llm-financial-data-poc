"""
로컬 PoC의 계획 등록, 실행과 재실행

처리 순서:
    1. 고정 사례와 설치 모델 확인
    2. 지시문, 모델 설정, 순서와 코드 원문의 사전 저장
    3. 시도 등록 후 단일 모델 호출 및 원본 저장
    4. 응답 해제와 저장 원문의 재조회

실행 단위: 워밍업 2회, 본 비교 52회, 별도 재실행 1회
"""

import argparse
import copy
import ctypes
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .data_evaluation_cases import _to_json
from .evaluate_response import evaluate_case_response
from .evaluation_local import build_ollama_request, call_ollama, get_ollama_info
from .evaluation_prompt import get_response_schema
from .evaluation_storage import begin_attempt, finish_attempt, load_attempts, load_experiment, save_experiment


# 기존 한국어 시험과 별도 등록하는 PoC, 모든 모델에 같은 명시적 설정 적용
MODELS = ("qwen3.5:9b", "gemma4:12b")
OPTIONS = {"presence_penalty": 0.0, "frequency_penalty": 0.0,
           "repeat_penalty": 1.0, "top_k": 40, "top_p": 0.95, "min_p": 0.0}
ROOT = Path(__file__).resolve().parent.parent
CASE_PATH = Path(__file__).with_name("poc_cases.json")


def _hash(value):
    """
    같은 자료와 설정을 추적하기 위한 정렬 JSON 식별값 반환

    입력: 해시를 계산할 설정 또는 자료 객체
    반환: 정렬 JSON의 SHA-256 16진수 문자열
    목적: 입력과 설정 변경 여부의 재조회 확인
    """
    return hashlib.sha256(_to_json(value).encode("utf-8")).hexdigest()


def load_cases():
    """
    PoC 자료의 입력 해시와 문항 및 언어 중복 확인, 모델 호출 없음

    입력: 파일 위치가 고정된 poc_cases.json
    반환: 지시문, 문항별 입력과 평가자 전용 정답의 전체 사전
    검사: 문항과 언어 중복, 입력 해시, 필수 세 항목과 채점 근거 경로
    예외: 중복 문항, 해시 불일치 또는 근거 누락 시 실행 전 중단
    """
    data = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    ids = []
    for case in data["cases"]:
        identity = (case["case_id"], case["language"])
        if identity in ids or _hash(case["input"]) != case["input_hash"]:
            raise ValueError("중복 문항 또는 입력 해시 불일치")
        ids.append(identity)
        if len(case["input"]["required_observations"]) != 3:
            raise ValueError("문항별 필수 확인 사항 세 개 필요")
        if case["case_id"] != "W00":
            rubric = case.get("rubric", [])
            if [r["id"] for r in rubric] != ["R01", "R02", "R03"]:
                raise ValueError("세 항목의 순서와 채점 근거 필요")
            for item in rubric:
                if not item["expected"] or not item["source_paths"]:
                    raise ValueError("정답 또는 원본 경로 누락")
                for path in item["source_paths"]:
                    value = case["input"]
                    for part in path.split("."):
                        value = value[int(part)] if isinstance(value, list) else value[part]
    return data


def environment_snapshot():
    """
    키와 개인 계좌를 읽지 않고 재현에 필요한 환경만 조회

    반환: Python과 패키지, GPU, 전원 상태 및 통제 여부의 사전
    조회: nvidia-smi, powercfg와 Windows 전원 API
    기준: 조회 실패는 None 유지, 키와 개인 계좌의 읽기 없음
    """
    packages = {}
    for name in ("httpx", "openai", "python-dotenv", "jsonschema"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    commands = {
        "gpu": ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                "--format=csv,noheader"],
        "power_scheme": ["powercfg", "/getactivescheme"],
    }
    result = {"python": sys.version, "platform": platform.platform(),
              "packages": packages, "other_apps_controlled": False,
              "thermal_state_controlled": False}
    for name, command in commands.items():
        try:
            process = subprocess.run(command, capture_output=True, timeout=10,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result[name] = process.stdout.decode("mbcs" if sys.platform == "win32" else "utf-8", errors="replace").strip()
        except (OSError, subprocess.TimeoutExpired):
            result[name] = None
    # Windows 전원 연결 상태의 숫자 원문 보존, 조회 불가 상태와 구분
    class PowerStatus(ctypes.Structure):
        _fields_ = [("ac_line", ctypes.c_byte), ("battery_flag", ctypes.c_byte),
                    ("battery_percent", ctypes.c_byte), ("reserved", ctypes.c_byte),
                    ("battery_life", ctypes.c_ulong), ("battery_full_life", ctypes.c_ulong)]
    try:
        power = PowerStatus()
        result["ac_line_status"] = (power.ac_line
            if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)) else None)
    except (AttributeError, OSError):
        result["ac_line_status"] = None
    return result


def prepare(kind, db_path="data/evaluation.db"):
    """
    입력과 지시문, 실행 순서 및 소스 원문을 첫 호출 전에 SQLite에 고정

    종류:
        warmup: W00 영문을 모델별 한 번 실행, 본 평가에서 제외
        main: 영문 10문항 두 번과 한국어 Q03, Q06, Q10 두 번, 총 52회

    원칙:
        - 모델별로 같은 의미의 자료와 같은 출력 언어 사용
        - 반복별 모델 순서와 언어 순서 교대
        - 과거 실험의 이어 실행이나 실패한 시도의 자동 재시도 없음

    입력: warmup 또는 main, 작업 DB 경로
    반환: 새 실험 ID
    저장: 고정 지시문, 별도 정답, 모델 digest, 공통 설정과 전체 순서
    효과: 설치 정보 조회와 계획 저장만 수행, 실제 모델 생성 없음
    """
    if kind not in ("warmup", "main"):
        raise ValueError("warmup 또는 main 필요")
    source = load_cases()
    cases = [copy.deepcopy(c) for c in source["cases"]
             if (c["case_id"] == "W00") == (kind == "warmup")]
    if kind == "warmup":
        if [(c["case_id"], c["language"]) for c in cases] != [("W00", "en")]:
            raise ValueError("W00 영문 한 문항 필요")
    else:
        expected = {(f"Q{i:02}", "en") for i in range(1, 11)}
        expected |= {(name, "ko") for name in ("Q03", "Q06", "Q10")}
        if {(c["case_id"], c["language"]) for c in cases} != expected:
            raise ValueError("영문 10문항과 한국어 3문항 필요")
    # ------------------------------ * 설치 모델과 실제 식별값 고정 * ------------------------------
    tags = {row["name"]: row for row in get_ollama_info("/api/tags")["models"]}
    models = {}
    for name in MODELS:
        if name not in tags:
            raise ValueError("필요 모델 미설치: " + name)
        models[name] = {"provider": "ollama", "digest": tags[name]["digest"],
                        "num_ctx": 8192, "num_predict": 2048, "thinking": False,
                        "temperature": 0.0, "timeout": 300, "options": dict(OPTIONS)}
    # ------------------------------ * 문항과 언어 및 모델 실행 순서 확정 * ------------------------------
    schedule = []
    for repeat in range(1, 2 if kind == "warmup" else 3):
        for case_id in sorted({c["case_id"] for c in cases}):
            languages = [lang for lang in ("en", "ko")
                         if any(c["case_id"] == case_id and c["language"] == lang for c in cases)]
            if repeat == 2:
                languages.reverse()
            for language in languages:
                for model in (MODELS if repeat == 1 else tuple(reversed(MODELS))):
                    schedule.append({"slot_id": f"{kind}/{case_id}/{language}/{repeat}/{model}",
                                     "case_id": case_id, "language": language,
                                     "model": model, "repeat": repeat,
                                     "phase": "warmup" if kind == "warmup" else "main"})
    # ------------------------------ * 실행 당시 코드 원문과 설정의 보존 * ------------------------------
    files = ["modules/evaluation_poc.py", "modules/poc_cases.json",
             "modules/evaluation_local.py", "modules/evaluation_storage.py",
             "modules/evaluation_prompt.py", "modules/evaluate_response.py",
             "modules/data_evaluation_cases.py", "modules/validate_evaluation_poc.py", "uv.lock"]
    config = {"evaluation_kind": "poc_" + kind, "models": models,
              "instructions": source["instructions"], "response_schema": get_response_schema(),
              "schedule": schedule, "automatic_retries": 0,
              "cloud_case_ids": source.get("cloud_case_ids", []),
              "scoring_policy": source.get("scoring_policy", {}),
              "acceptance": {"english_completed_valid_json_minimum": 19,
                             "english_attempts_per_model": 20,
                             "english_quality_points_minimum": 54,
                             "english_quality_points_total": 60,
                             "critical_order_violations_maximum": 0,
                             "preferred_median_seconds": 60},

              "ollama_version": get_ollama_info("/api/version")["version"],
              "environment": environment_snapshot(),
              "source_hashes": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files},
              "source_contents": {p: (ROOT / p).read_text(encoding="utf-8") for p in files}}
    experiment_id = "poc-" + kind + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_experiment(experiment_id, config, {"cases": cases}, db_path)
    return experiment_id


def _case(experiment, slot):
    """
    문항 ID와 언어를 함께 사용하여 고정 입력 선택

    입력: 저장된 실험과 실행 위치 정보
    반환: case_id와 language가 모두 일치하는 사례
    목적: 같은 문항의 영문과 한국어 원본 혼용 방지
    """
    return next(c for c in experiment["dataset"]["cases"]
                if c["case_id"] == slot["case_id"] and c["language"] == slot["language"])


def build_request(experiment, slot):
    """
    정답을 제외한 지시문과 입력만 실제 전송 본문으로 구성

    입력: 저장된 실험과 실행 위치
    반환: 모델 API에 실제 전달할 본문
    제외: expected와 rubric 등 평가자 전용 정답
    보존: 저장된 system 지시문과 input의 내용 및 자료형
    """
    config = experiment["config"]
    case = _case(experiment, slot)
    model = config["models"][slot["model"]]
    messages = [{"role": "system", "content": config["instructions"][slot["language"]]},
                {"role": "user", "content": _to_json(case["input"])}]
    return build_ollama_request(
        slot["model"], messages, num_ctx=model["num_ctx"], num_predict=model["num_predict"],
        thinking=model["thinking"], output_format=config["response_schema"],
        temperature=model["temperature"], extra_options=model["options"])


def run(experiment_id, db_path="data/evaluation.db", limit=None):
    """
    확정한 계획의 미실행 위치만 순서대로 호출하고 원문과 측정값 저장

    주의사항:
        - 실행 전 코드, Ollama 버전, 모델 digest와 단독 적재 여부 확인
        - 호출 전 시도 등록, 예정 본문과 실제 전송 본문의 저장 시 일치 검사
        - 성공 여부와 관계없이 원문 보존, 실패한 응답을 hold로 대체하지 않는 처리
        - 마지막 SQLite 재조회까지 수행, 이미 등록된 시도의 자동 재호출 금지
        - 실제 주문과 Cloud 호출을 포함하지 않는 로컬 실행 경로

    입력: 고정 실험 ID, DB 경로, 선택적인 이번 구간 호출 한도
    반환: 이번 실행에서 새로 종료한 호출 수
    실행: 이미 등록한 위치 건너뛰기, 호출 전 등록과 종료 후 저장
    중단: 코드나 모델 식별값 변경, 다른 모델 적재 또는 모델 해제 실패
    """
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("실행 횟수 제한은 양의 정수 필요")
    experiment = load_experiment(experiment_id, db_path)
    config = experiment["config"]
    if config["evaluation_kind"] not in ("poc_warmup", "poc_main", "poc_selfcheck", "poc_completion_check"):
        raise ValueError("새 PoC 실험만 실행 가능")
    for relative, expected in config["source_hashes"].items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("확정 후 코드 변경: " + relative)
    # ------------------------------ * 등록된 시도 제외와 단일 모델 실행 * ------------------------------
    existing = {row["slot_id"] for row in load_attempts(experiment_id, db_path)}
    count = 0
    for slot in config["schedule"]:
        if slot["slot_id"] in existing:
            continue
        if limit is not None and count >= limit:
            break
        if get_ollama_info("/api/ps").get("models"):
            raise RuntimeError("다른 적재 모델 확인 필요")
        if get_ollama_info("/api/version")["version"] != config["ollama_version"]:
            raise RuntimeError("Ollama 버전 변경")
        tags = {row["name"]: row for row in get_ollama_info("/api/tags")["models"]}
        model = config["models"][slot["model"]]
        if tags.get(slot["model"], {}).get("digest") != model["digest"]:
            raise RuntimeError("설치 모델 식별값 변경")
        request = build_request(experiment, slot)
        attempt = begin_attempt(experiment_id, slot["slot_id"], slot["case_id"],
                                slot["model"], slot["repeat"], request,
                                phase=slot["phase"], db_path=db_path)
        if attempt is None:
            continue
        try:
            record = call_ollama(
                slot["model"], request["messages"], num_ctx=model["num_ctx"],
                num_predict=model["num_predict"], thinking=model["thinking"],
                output_format=request["format"], timeout=model["timeout"],
                temperature=model["temperature"], extra_options=model["options"])
        except Exception as error:
            record = {"run_id": str(uuid4()), "started_at": datetime.now(timezone.utc).isoformat(),
                      "request": request, "status": "failed", "content": "", "thinking": "",
                      "raw_lines": [], "response_metadata": None, "final_response_complete": False,
                      "errors": [{"type": "runner_exception", "detail": str(error)}]}
        record.update({"attempt_id": attempt, "experiment_id": experiment_id,
                       "case_id": slot["case_id"], "language": slot["language"],
                       "repeat_number": slot["repeat"], "phase": slot["phase"]})
        # 검사기 예외도 원본 응답의 저장을 방해하지 않도록 별도 오류로 보존
        evaluation = {"api_success": record["status"] == "completed",
                      "final_completed": record.get("final_response_complete", False),
                      "input_status": "valid" if record.get("final_response_complete") else "unknown"}
        try:
            assessment = evaluate_case_response(_case(experiment, slot), record["content"],
                                                experiment["dataset_hash"])
            evaluation["assessment"] = assessment
        except Exception as error:
            assessment = {"schema_valid": None, "trading_valid": None}
            evaluation["grading_error"] = str(error)
        finish_attempt(attempt, record, evaluation, db_path)
        # DB에 저장된 요청 해시와 원본 연결을 재조회하여 저장 경로 확인
        saved = next(row for row in load_attempts(experiment_id, db_path)
                     if row["attempt_id"] == attempt)
        if saved["record"]["content"] != record["content"]:
            raise RuntimeError("응답 저장과 재조회 불일치")
        count += 1
        print(json.dumps({"slot_id": slot["slot_id"], "attempt_id": attempt,
                          "status": record["status"], "seconds": record.get("wall_seconds"),
                          "schema_valid": assessment["schema_valid"],
                          "trading_valid": assessment["trading_valid"],
                          "saved_content_matches": True}, ensure_ascii=False), flush=True)
        if any(e["type"] == "unload" for e in record.get("errors", [])):
            raise RuntimeError("해제 실패, 다음 모델 실행 중지")
    return count


def prepare_mode(local_experiment_id, *, warmup=False, num_predict=None, repeat_count=2,
                 db_path="data/evaluation.db"):
    """
    동일 본 실험의 추론 사용 조건 등록

    입력: 기존 비추론 실험 ID, 준비 호출 여부, 생성 길이 요청값, 반복 수, SQLite 경로
    반환: 현재 실행 날짜가 포함된 조건별 기록 ID
    비교: 기존 영문 입력과 지시문, 모델 digest, 생성 설정의 보존
    변경: thinking=True와 지정한 num_predict만 변경, 나머지 실제 요청 본문 대조
    구성: 영문 10문항, 두 모델, 지정한 반복 수 적용; 준비 호출은 W00 모델별 1회
    제한: 기존 문맥 창과 호출 시간 유지, 기존 입력 토큰과 요청 생성량의 합 확인
    해석: 반복 1회는 모델당 30점 비교이며 원래 20회 통과 판정과 별도 표시
    원칙: 과거 원본 덮어쓰기와 실행 날짜 소급 없음, 조건별 점수 분리
    """
    if repeat_count not in (1, 2):
        raise ValueError("반복 수는 1회 또는 2회 필요")
    if num_predict is not None and (type(num_predict) is not int or num_predict <= 0):
        raise ValueError("생성 길이 요청값은 양의 정수 필요")
    original = load_experiment(local_experiment_id, db_path)
    if original["config"]["evaluation_kind"] != "poc_main":
        raise ValueError("기존 본 비교 기록 필요")
    if any(m["thinking"] is not False for m in original["config"]["models"].values()):
        raise ValueError("비추론 기준 조건 필요")
    config = copy.deepcopy(original["config"])
    kind = "warmup" if warmup else "main"
    config.update(evaluation_kind="poc_" + kind, comparison_mode="reasoning",
                  baseline_experiment_id=local_experiment_id,
                  baseline_config_hash=original["config_hash"],
                  environment=environment_snapshot())
    for model in config["models"].values():
        model["thinking"] = True
        if num_predict is not None:
            model["num_predict"] = num_predict
    config["generation_limit_comparison"] = {"num_predict": num_predict, "repeat_count": repeat_count,
        "scope": "동일 영문 문항과 설정의 생성 길이별 비교, 원본의 20회 통과 판정과 분리"}
    if warmup:
        cases = [copy.deepcopy(c) for c in load_cases()["cases"] if c["case_id"] == "W00"]
        schedule = [{"slot_id": f"warmup/W00/en/1/{m}", "case_id": "W00",
                     "language": "en", "model": m, "repeat": 1, "phase": "warmup"}
                    for m in MODELS]
    else:
        cases = [copy.deepcopy(c) for c in original["dataset"]["cases"] if c["language"] == "en"]
        schedule = [copy.deepcopy(s) for s in config["schedule"]
                    if s["language"] == "en" and s["repeat"] <= repeat_count]
        if len(cases) != 10 or len(schedule) != 20 * repeat_count:
            raise ValueError("영문 10문항, 두 모델과 지정 반복 수의 구성 필요")
        # 측정된 기준 입력의 최댓값과 생성 요청량을 이용한 문맥 창 초과 방지
        observations = load_attempts(local_experiment_id, db_path)
        for model_name, model in config["models"].items():
            tokens = [(a["record"].get("response_metadata") or {}).get("prompt_eval_count")
                      for a in observations if a["model"] == model_name and a.get("record")
                      and a["record"].get("language") == "en"]
            tokens = [value for value in tokens if isinstance(value, int)]
            if not tokens or max(tokens) + model["num_predict"] > model["num_ctx"]:
                raise ValueError("기준 입력과 생성 요청량의 합이 기존 문맥 창을 넘거나 입력 실측 누락")
        if repeat_count == 1:
            config["acceptance"] = {"reference_twenty_call_criteria": original["config"].get("acceptance"),
                "status": "10문항 1회 비교, 20회 통과 기준의 최종 판정 없음"}
    config["schedule"] = schedule
    files = list(config["source_hashes"])
    config["source_hashes"] = {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in files}
    config["source_contents"] = {p: (ROOT/p).read_text(encoding="utf-8") for p in files}
    # 추론 플래그와 생성 길이 이외의 지시문, 자료, 모델과 옵션 변경 방지
    if not warmup:
        proposed = {"config": config, "dataset": {"cases": cases}}
        for slot in schedule:
            before, after = build_request(original, slot), build_request(proposed, slot)
            before["think"] = True
            if num_predict is not None:
                before["options"]["num_predict"] = num_predict
            if before != after:
                raise ValueError("추론과 생성 길이 이외의 요청 변경 확인")
    budget_name = str(num_predict) + "-" if num_predict is not None else ""
    eid = "poc-" + kind + "-reasoning-" + budget_name + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_experiment(eid, config, {"cases": cases}, db_path)
    return eid


def prepare_completion_check(local_experiment_id, db_path="data/evaluation.db"):
    """
    공식 권장 샘플링을 적용한 대표 문항의 응답 완료 확인 계획 등록

    목적: Q08의 추론 종료와 최종 JSON 반환 여부, 실제 생성량과 시간 확인
    구성: 같은 영문 Q08을 설치된 두 모델에 각각 한 번 적용, 자동 반복 없음
    보존: 기존 입력과 영문 지시문, 출력 스키마, 양자화 모델 및 설치 식별값
    변경: 모델별 공식 샘플링, 충분한 문맥과 생성 여유, 1,800초 실행 제한
    구분: Qwen의 공식 문맥 및 출력 권고와 Gemma에 자체 지정한 실행 한도
    제한: 모델별 조건이 다른 실행 가능성 점검, 전체 품질 순위나 최소 필요량 추정 금지
    반환: 실행 전 자료와 코드 및 출처가 저장된 새 기록 ID
    """
    original = load_experiment(local_experiment_id, db_path)
    if original["config"]["evaluation_kind"] != "poc_main":
        raise ValueError("기존 본 비교 기록 필요")
    case = copy.deepcopy(next(c for c in original["dataset"]["cases"]
                              if c["case_id"] == "Q08" and c["language"] == "en"))
    config = copy.deepcopy(original["config"])
    config.update(
        evaluation_kind="poc_completion_check", baseline_experiment_id=local_experiment_id,
        baseline_config_hash=original["config_hash"], environment=environment_snapshot(),
        acceptance={}, cloud_case_ids=[], automatic_retries=0,
        scope="공식 권장 샘플링에서 대표 Q08의 추론과 최종 답변 완료 확인",
        case_selection_reason="체결 수량과 가격, 수수료를 반영한 손익 및 회고를 한 문항에서 확인 가능한 기존 사례",
        success_definition="API 정상 종료, done_reason=stop, 비어 있지 않은 최종 답변과 지정 JSON 준수",
        interpretation_limit="문항과 모델별 한 번의 관측, 전체 10문항의 정확성 또는 안정성 및 최소 생성 한도 판정 없음",
        schedule=[{"slot_id": f"completion/Q08/en/1/{m}", "case_id": "Q08",
                   "language": "en", "model": m, "repeat": 1, "phase": "preflight"}
                  for m in MODELS])
    config.pop("generation_limit_comparison", None)
    for name, model in config["models"].items():
        model.update(thinking=True, temperature=1.0, timeout=1800,
                     options={"top_p": 0.95, "top_k": 20 if name == MODELS[0] else 64,
                              "min_p": 0.0, "presence_penalty": 1.5 if name == MODELS[0] else 0.0,
                              "frequency_penalty": 0.0, "repeat_penalty": 1.0})
        # Qwen 문서의 일반 추론 권고와 Gemma의 노트북 실행 한도 구분
        model["num_ctx"] = 131072 if name == MODELS[0] else 32768
        model["num_predict"] = 32768 if name == MODELS[0] else 16384
    config["settings_provenance"] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "qwen": {
            "url": "https://huggingface.co/Qwen/Qwen3.5-9B",
            "sections": ["Best Practices", "Serving Qwen3.5"],
            "official_sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20,
                                  "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.0},
            "official_general_output_recommendation": 32768,
            "official_advised_context_minimum": 131072,
            "mapping": "repetition_penalty를 Ollama의 repeat_penalty로 지정",
        },
        "gemma": {
            "url": "https://huggingface.co/google/gemma-4-12B-it",
            "ollama_url": "https://ollama.com/library/gemma4",
            "section": "Best Practices",
            "official_sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 64},
            "operator_limits": {"num_ctx": 32768, "num_predict": 16384},
            "limit_reason": "기존 짧은 입력과 충분한 추론 및 답변 여유를 수용하는 노트북용 실행 한도, 공식 권장 수치로 표시하지 않는 기준",
        },
        "shared_operator_settings": {"seed": 42, "timeout_seconds": 1800,
            "frequency_penalty": 0.0, "min_p_if_unspecified": 0.0,
            "presence_penalty_if_unspecified": 0.0, "repeat_penalty_if_unspecified": 1.0},
        "retained_constraints": "기존 JSON Schema 강제, 원본 영문 지시문과 입력, Q4_K_M, Ollama 버전 유지",
        "causal_limit": "샘플링과 실행 한도를 함께 변경하므로 개선 원인을 temperature 하나로 단정하지 않는 기준",
    }
    tags = {row["name"]: row for row in get_ollama_info("/api/tags")["models"]}
    for name, model in config["models"].items():
        if tags.get(name, {}).get("digest") != model["digest"]:
            raise ValueError("기준 실험과 설치 모델 식별값 불일치")
    config["ollama_version"] = get_ollama_info("/api/version")["version"]
    files = list(config["source_hashes"])
    config["source_hashes"] = {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in files}
    config["source_contents"] = {p: (ROOT/p).read_text(encoding="utf-8") for p in files}
    proposed = {"config": config, "dataset": {"cases": [case]}}
    for slot in config["schedule"]:
        before, after = build_request(original, slot), build_request(proposed, slot)
        if before["messages"] != after["messages"] or before["format"] != after["format"]:
            raise ValueError("대표 문항 또는 지시문과 응답 형식 변경")
    eid = "poc-completion-check-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_experiment(eid, config, {"cases": [case]}, db_path)
    return eid


def prepare_recommended_main(completion_experiment_id, db_path="data/evaluation.db"):
    """
    대표 문항에서 정상 종료한 설정을 영문 Q01~Q10 전체에 고정하여 등록

    입력: 완료한 대표 문항 시험 ID와 원본 SQLite 경로
    구성: 두 모델, 영문 10문항, 문항별 한 번의 새 호출로 총 20회
    보존: 대표 시험의 모델별 샘플링, 문맥 창, 생성 한도와 호출 제한
    원본: 기존 비추론의 입력과 영문 지시문 및 JSON Schema, 동일 채점 기준
    구분: Q08도 이번 배치에서 새로 실행, 대표 시험 응답의 중복 합산 없음
    제한: 실행 도중 설정 조정과 자동 재시도 없음, 전체 종료 뒤 품질 검토 및 비교
    반환: 현재 실행 날짜가 포함된 본 시험 ID, 실제 모델 호출 없는 사전 등록
    """
    reference = load_experiment(completion_experiment_id, db_path)
    if reference["config"]["evaluation_kind"] != "poc_completion_check":
        raise ValueError("대표 문항의 응답 완료 확인 기록 필요")
    observed = load_attempts(completion_experiment_id, db_path)
    for name in MODELS:
        successes = [a for a in observed if a["model"] == name
                     and a["status"] == "completed" and a.get("record")
                     and a["record"].get("content")
                     and (a["record"].get("response_metadata") or {}).get("done_reason") == "stop"
                     and ((a.get("evaluation") or {}).get("assessment") or {}).get("schema_valid")]
        if len(successes) != 1:
            raise ValueError("두 모델의 대표 문항 정상 JSON 확인 필요")
    baseline_id = reference["config"]["baseline_experiment_id"]
    baseline = load_experiment(baseline_id, db_path)
    cases = [copy.deepcopy(c) for c in baseline["dataset"]["cases"] if c["language"] == "en"]
    if {c["case_id"] for c in cases} != {f"Q{i:02d}" for i in range(1, 11)} or len(cases) != 10:
        raise ValueError("기존 영문 Q01~Q10 전체 필요")
    config = copy.deepcopy(reference["config"])
    config.update(
        evaluation_kind="poc_main", comparison_mode="recommended_reasoning",
        representative_experiment_id=completion_experiment_id,
        representative_config_hash=reference["config_hash"],
        scope="대표 문항의 성공 설정을 유지한 영문 10문항 추론 실행, 전체 종료 후 기존 비추론과 비교",
        environment=environment_snapshot(),
        schedule=[{"slot_id": f"main/{case_id}/en/1/{model}", "case_id": case_id,
                   "language": "en", "model": model, "repeat": 1, "phase": "main"}
                  for case_id in sorted(c["case_id"] for c in cases) for model in MODELS],
        acceptance={"reference_twenty_call_criteria": baseline["config"].get("acceptance"),
                    "status": "모델별 10응답 30점, 기존 20응답 60점 통과 기준의 임의 축소 적용 없음"},
        interpretation_limit="공식 권장 설정의 추론과 기존 비추론 설정 비교, 추론 여부만의 인과 효과 및 반복 안정성 판정 없음")
    config.pop("case_selection_reason", None)
    proposed = {"config": config, "dataset": {"cases": cases}}
    for slot in config["schedule"]:
        before, after = build_request(baseline, slot), build_request(proposed, slot)
        if before["messages"] != after["messages"] or before["format"] != after["format"]:
            raise ValueError("기존 입력과 지시문 및 JSON Schema 변경")
        if slot["case_id"] == "Q08" and build_request(reference, slot) != after:
            raise ValueError("대표 문항에서 확인한 실제 요청 설정 변경")
    if config["models"] != reference["config"]["models"]:
        raise ValueError("대표 시험의 모델별 실행 설정 변경")
    files = list(config["source_hashes"])
    config["source_hashes"] = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in files}
    config["source_contents"] = {p: (ROOT / p).read_text(encoding="utf-8") for p in files}
    eid = "poc-main-recommended-reasoning-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_experiment(eid, config, {"cases": cases}, db_path)
    return eid



def prepare_best_practices(completion_experiment_id, *, thinking, warmup=False,
                           db_path="data/evaluation.db"):
    """
    공식 권장 설정의 비추론 및 추론 비교 계획 등록

    입력: 대표 문항 실행 기록, 추론 사용 여부, 워밍업 구분과 SQLite 경로
    본 시험: 기존 영문 10문항을 모델별 한 번 실행, 모드별 20회
    워밍업: 같은 설정의 W00을 모델별 한 번 실행, 품질 집계에서 제외
    보존: 원본 입력, 영문 지시문, JSON Schema, 모델 식별값과 세 항목 채점 기준
    설정: Qwen 일반 과제의 모드별 권장값, Gemma 공통 권장값 적용
    통제: 같은 모델의 문맥 창, 생성 한도, seed와 호출 제한 동일 유지
    제한: 모드별 샘플링 차이가 있는 설정 묶음 비교, 추론만의 인과 효과 주장 제외
    저장: 실제 호출 전 입력과 설정, 출처, 환경 및 실행 코드 원문 보존
    """
    if type(thinking) is not bool:
        raise ValueError("추론 사용 여부는 bool 필요")
    reference = load_experiment(completion_experiment_id, db_path)
    if reference["config"]["evaluation_kind"] != "poc_completion_check":
        raise ValueError("대표 문항 실행 설정 기록 필요")
    baseline_id = reference["config"]["baseline_experiment_id"]
    baseline = load_experiment(baseline_id, db_path)
    if warmup:
        cases = [copy.deepcopy(c) for c in load_cases()["cases"]
                 if c["case_id"] == "W00" and c["language"] == "en"]
    else:
        cases = [copy.deepcopy(c) for c in baseline["dataset"]["cases"]
                 if c["language"] == "en"]
        if len(cases) != 10 or {c["case_id"] for c in cases} != {f"Q{i:02d}" for i in range(1, 11)}:
            raise ValueError("기존 영문 10문항 전체 필요")
    mode = "thinking" if thinking else "nonthinking"
    phase = "warmup" if warmup else "main"
    config = copy.deepcopy(reference["config"])
    config.update(
        evaluation_kind="poc_" + phase, comparison_mode="best_practices_" + mode,
        representative_experiment_id=completion_experiment_id,
        scope="공식 권장 설정의 영문 10문항 비추론 및 추론 비교, 모드별 20회",
        environment=environment_snapshot(), automatic_retries=0,
        interpretation_limit="모델별 모드별 10응답 30항목, 반복 안정성과 추론만의 인과 효과 판정 제외",
        acceptance={
            "reference_twenty_call_criteria": baseline["config"].get("acceptance"),
            "status": "기존 20응답 60점 기준 보존, 이번 10응답 30점에 기존 도입 통과 판정 직접 적용 없음",
            "operational_deadline_seconds": 300,
            "measurement_timeout_seconds": 1800},
        schedule=[{"slot_id": f"best-practices/{mode}/{phase}/{case_id}/en/1/{model}",
                   "case_id": case_id, "language": "en", "model": model,
                   "repeat": 1, "phase": phase}
                  for case_id in sorted(c["case_id"] for c in cases)
                  for model in MODELS])
    config.pop("case_selection_reason", None)
    # 자료 설명을 주목적으로 하는 일반 과제 설정의 사전 선택
    # 수치 검산 포함 사실과 별개로 논리 추론 전용 설정의 사후 선택 방지
    for name, model in config["models"].items():
        model["thinking"] = thinking
        if name == MODELS[0] and not thinking:
            model["temperature"] = 0.7
            model["options"]["top_p"] = 0.8
    provenance = config["settings_provenance"]
    provenance["checked_at"] = datetime.now(timezone.utc).isoformat()
    provenance["qwen"]["official_sampling"].update(
        temperature=1.0 if thinking else 0.7, top_p=0.95 if thinking else 0.8)
    provenance["qwen"]["task_category"] = "general tasks"
    provenance["qwen"]["task_category_reason"] = "시장 및 계좌 자료의 설명과 사실 확인이 주목적인 기존 과제, 코딩 및 수학 경진대회 과제와 구분"
    provenance["gemma"]["url"] = "https://ai.google.dev/gemma/docs/core/model_card_4#best-practices"
    provenance["causal_limit"] = "Qwen 모드별 권장 temperature와 top_p 차이 포함, 추론 기능 하나의 효과와 구분"
    provenance["history_policy"] = "각 문항을 독립된 system/user 요청으로 구성, 이전 문항의 추론과 최종 답변 전달 없음"
    provenance["operator_limits_reason"] = "대표 문항에서 실행을 확인한 모델별 문맥 및 생성 한도를 두 모드에 동일 적용"
    tags = {row["name"]: row for row in get_ollama_info("/api/tags")["models"]}
    for name, model in config["models"].items():
        if tags.get(name, {}).get("digest") != model["digest"]:
            raise ValueError("기준 실험과 설치 모델 식별값 불일치")
    config["ollama_version"] = get_ollama_info("/api/version")["version"]
    proposed = {"config": config, "dataset": {"cases": cases}}
    for slot in config["schedule"]:
        request = build_request(proposed, slot)
        if not warmup:
            old = build_request(baseline, slot)
            if request["messages"] != old["messages"] or request["format"] != old["format"]:
                raise ValueError("기존 입력, 지시문 또는 응답 형식 변경")
    files = list(config["source_hashes"])
    config["source_hashes"] = {f: hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}
    config["source_contents"] = {f: (ROOT/f).read_text(encoding="utf-8") for f in files}
    eid = f"poc-best-practices-{mode}-{phase}-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_experiment(eid, config, {"cases": cases}, db_path)
    return eid


def prepare_selfcheck(local_experiment_id, model="gemma4:12b", db_path="data/evaluation.db"):
    """
    완료한 로컬 계획에서 Q06 영문 한 문항을 복사하는 별도 재실행 계획 등록

    목적:
        - 선정 후 실행 경로와 저장 및 원본 재조회의 재현 확인
        - 본 평가 52회와 별도 1회로 집계, 모델의 품질 점수 보충 금지
        - 원래 문항과 지시문, 모델 식별값 및 생성 설정 유지
        - 재실행 시점 환경과 현재 실행 코드의 원문 및 해시를 별도 보존

    입력: 종료된 본 비교 ID, 등록된 모델 태그와 작업 DB
    반환: Q06 영문 한 문항의 새 재실행 ID
    보존: 기존 지시문과 입력 및 모델 설정, 현재 코드 원문의 별도 기록
    집계: 본 비교 점수에 합산하지 않는 extension 구분
    """
    original = load_experiment(local_experiment_id, db_path)
    if original["config"]["evaluation_kind"] != "poc_main" or model not in original["config"]["models"]:
        raise ValueError("완료한 로컬 PoC와 등록된 후보 모델 필요")
    attempts = load_attempts(local_experiment_id, db_path)
    if len(attempts) != len(original["config"]["schedule"]) or any(a["status"] == "running" for a in attempts):
        raise ValueError("본 비교 종료 후 재실행 필요")
    case = copy.deepcopy(next(c for c in original["dataset"]["cases"]
                              if c["case_id"] == "Q06" and c["language"] == "en"))
    config = copy.deepcopy(original["config"])
    config.update(evaluation_kind="poc_selfcheck", parent_local_experiment_id=local_experiment_id,
                  parent_config_hash=original["config_hash"], parent_dataset_hash=original["dataset_hash"],
                  models={model: config["models"][model]}, environment=environment_snapshot(),
                  acceptance={}, cloud_case_ids=[],
                  schedule=[{"slot_id": f"selfcheck/Q06/en/1/{model}", "case_id": "Q06",
                             "language": "en", "model": model, "repeat": 1, "phase": "extension"}])
    files = list(config["source_hashes"])
    config["source_hashes"] = {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in files}
    config["source_contents"] = {p: (ROOT/p).read_text(encoding="utf-8") for p in files}
    eid = "poc-selfcheck-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_experiment(eid, config, {"cases": [case]}, db_path)
    return eid


def main():
    """
    계획 등록과 실제 호출을 명시적으로 구분하는 실행 진입점

    입력: 명령행의 실험 ID와 경로 및 선택 옵션
    처리: 명시한 준비, 실행 또는 조회 기능으로 분기
    반환: 결과 또는 생성된 실험 ID의 JSON 출력
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment_id", nargs="?")
    parser.add_argument("--prepare", choices=("warmup", "main"))
    parser.add_argument("--db", default="data/evaluation.db")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--prepare-selfcheck-from")
    parser.add_argument("--prepare-mode-from")
    parser.add_argument("--prepare-completion-from")
    parser.add_argument("--prepare-recommended-main-from")
    parser.add_argument("--prepare-best-practices-from")
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--mode-warmup", action="store_true")
    parser.add_argument("--mode-num-predict", type=int)
    parser.add_argument("--mode-repeat-count", type=int, default=2, choices=(1, 2))
    parser.add_argument("--model", choices=MODELS, default="gemma4:12b")
    args = parser.parse_args()
    if args.prepare_best_practices_from:
        print(json.dumps({"experiment_id": prepare_best_practices(
            args.prepare_best_practices_from, thinking=args.thinking, warmup=args.mode_warmup, db_path=args.db)}))
    elif args.prepare_recommended_main_from:
        print(json.dumps({"experiment_id": prepare_recommended_main(args.prepare_recommended_main_from, args.db)}))
    elif args.prepare_completion_from:
        print(json.dumps({"experiment_id": prepare_completion_check(args.prepare_completion_from, args.db)}))
    elif args.prepare_mode_from:
        print(json.dumps({"experiment_id": prepare_mode(args.prepare_mode_from, warmup=args.mode_warmup,
            num_predict=args.mode_num_predict, repeat_count=args.mode_repeat_count, db_path=args.db)}))
    elif args.prepare_selfcheck_from:
        print(json.dumps({"experiment_id": prepare_selfcheck(args.prepare_selfcheck_from, args.model, args.db)}))
    elif args.prepare:
        print(json.dumps({"experiment_id": prepare(args.prepare, args.db)}))
    elif args.experiment_id:
        run(args.experiment_id, args.db, args.limit)
    else:
        parser.error("--prepare 또는 experiment_id 필요")


if __name__ == "__main__":
    main()
