"""
개인 API 키를 이용한 Luna 비교 실행

구성:
    - 저장된 로컬 계획에서 영문 다섯 문항의 동일 입력 선택
    - 공식 Responses API의 단일 스트림 호출과 원본 이벤트 보존
    - 반환된 토큰 사용량과 고정 단가의 비용 계산
    - 호출 전 시도 등록과 자동 유료 재시도 차단

보관 범위: 키를 제외한 요청 본문, 응답, 시간과 사용량
"""

import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values

from .json_utils import _to_json


LUNA_MODEL = "gpt-5.6-luna"
# 시험 당시 확인한 단가의 보존, 현재 청구 금액이나 최신 가격의 자동 조회 없음
LUNA_PRICING = {
    "checked_at": "2026-09-15",
    "source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
    "currency": "USD", "per_tokens": 1000000,
    "input": "0.20", "cached_input": "0.02", "output": "1.20",
    "cache_write_multiplier": "1.25", "long_input_threshold": 272000,
    "long_input_multiplier": "2", "long_output_multiplier": "1.5",
}


def _api_key():
    """
    현재 프로세스 또는 프로젝트 .env에서 개인 키 조회, 반환값의 기록 금지

    입력: OPENAI_API_KEY 환경 변수 또는 프로젝트 .env
    반환: 공백을 제거한 키 문자열, 미설정 시 빈 문자열
    우선순위: 프로세스 환경 변수 우선, 키 원문의 출력과 기록 금지
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    return (os.environ.get("OPENAI_API_KEY") or dotenv_values(path).get("OPENAI_API_KEY") or "").strip()


def luna_key_available():
    """
    키 원문을 노출하지 않는 설정 여부 확인

    반환: 키 설정 여부의 bool
    목적: 유료 호출 전 설정 확인, 키 내용의 노출 방지
    """
    return bool(_api_key())


def build_luna_request(messages, *, max_output_tokens=2048, reasoning_effort="none",
                       output_format=None, temperature=0.0):
    """
    동일한 대화 내용을 Luna Responses API 본문으로 변환

    조건:
        - 외부 도구, 이전 응답 ID와 서버 대화 상태의 사용 없음
        - 입력 자동 잘림 비활성, 응답 저장 옵션 비활성
        - 기본 비교는 추론 none, 추론 변경은 별도 확장 조건
        - seed와 로컬 context 크기는 지원이 확인된 공통 API 인자로 취급하지 않는 기준

    입력: 공통 대화, 출력 한도와 추론 및 스키마 설정
    반환: 인증 정보가 없는 Responses API 요청 사전
    기준: 입력 자동 잘림과 서버 응답 저장 비활성, 도구 호출 없음
    """
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError("양의 생성 한도 필요")
    if reasoning_effort not in ("none", "low", "medium", "high", "xhigh", "max"):
        raise ValueError("지원하지 않는 Luna 추론 조건")
    request = {"model": LUNA_MODEL, "input": messages, "stream": True, "store": False,
               "truncation": "disabled", "max_output_tokens": max_output_tokens,
               "reasoning": {"effort": reasoning_effort}, "service_tier": "default"}
    if reasoning_effort == "none":
        request["temperature"] = temperature
    if output_format is not None and output_format != "json" and not isinstance(output_format, dict):
        raise ValueError("JSON 또는 JSON Schema 형식 필요")
    if output_format is not None:
        form = ({"type": "json_object"} if output_format == "json" else
                {"type": "json_schema", "name": "evaluation_response", "strict": True,
                 "schema": output_format})
        request["text"] = {"format": form}
    return request


def calculate_luna_cost(usage, *, service_tier="default"):
    """
    응답의 실제 사용량과 확인한 단가에 따른 달러 비용 계산

    주의사항:
        - 추론 토큰은 output_tokens에 포함되므로 추가 중복 합산 없음
        - 캐시 읽기와 쓰기 토큰은 전체 입력의 일부로 계산
        - 필요한 사용량이 없으면 0원 대신 미확인 처리
        - 표준 단가 외 등급과 도구 비용은 이번 계산 범위에서 제외
        - 세금과 환율 및 실제 청구서의 반올림은 별도 항목

    입력: API의 실제 usage와 서비스 등급
    반환: USD 계산값 또는 None, 적용 단가와 미확인 사유
    검산: 일반 입력, 캐시 읽기, 캐시 쓰기와 출력을 구분한 Decimal 계산
    """
    result = {"usd": None, "pricing": dict(LUNA_PRICING), "basis": "reported_usage"}
    if service_tier not in ("default", "standard"):
        return dict(result, note="표준 단가 외 서비스 등급")
    if not isinstance(usage, dict):
        return dict(result, note="사용량 미반환")
    details = usage.get("input_tokens_details") or {}
    values = [usage.get("input_tokens"), details.get("cached_tokens"),
              details.get("cache_write_tokens"), usage.get("output_tokens")]
    if any(type(value) is not int or value < 0 for value in values):
        return dict(result, note="입력, 출력 또는 캐시 사용량 미확인")
    input_tokens, cached, written, output_tokens = values
    if cached + written > input_tokens:
        return dict(result, note="입력과 캐시 사용량의 합계 불일치")
    regular = input_tokens - cached - written
    long_input = input_tokens > LUNA_PRICING["long_input_threshold"]
    input_multiplier = Decimal("2") if long_input else Decimal("1")
    output_multiplier = Decimal("1.5") if long_input else Decimal("1")
    price = ((Decimal(regular) * Decimal("0.20")
              + Decimal(cached) * Decimal("0.02")
              + Decimal(written) * Decimal("0.20") * Decimal("1.25")) * input_multiplier
             + Decimal(output_tokens) * Decimal("1.20") * output_multiplier) / Decimal("1000000")
    return dict(result, usd=str(price), long_input_pricing=long_input, note="단가 기준 사용량 계산")


def _receive_luna(request, connection, timeout):
    """
    별도 프로세스에서 개인 API 호출, 자동 재시도 비활성 및 원본 이벤트 전달

    입력: 요청 본문, 부모 연결과 시간 제한
    전달: 원본 API 이벤트 또는 인증값을 제거한 오류
    기준: SDK 자동 재시도 0회, 연결 종료의 finally 처리
    """
    key = _api_key()
    try:
        from openai import OpenAI
        if not key:
            raise ValueError("OPENAI_API_KEY 설정 필요")
        # 환경의 임의 프록시 주소를 API 목적지로 사용하지 않는 고정 공식 경로
        with OpenAI(api_key=key, base_url="https://api.openai.com/v1",
                    max_retries=0, timeout=timeout) as client:
            with client.responses.create(**request) as stream:
                for event in stream:
                    connection.send(("event", event.model_dump(mode="json")))
        connection.send(("end", None))
    except Exception as error:
        detail = str(error)
        if key:
            detail = detail.replace(key, "[REDACTED]")
        detail = re.sub(r"sk-[A-Za-z0-9_*-]+", "[REDACTED]", detail)
        connection.send(("error", {"type": type(error).__name__,
                                  "status": getattr(error, "status_code", None), "detail": detail}))
    finally:
        connection.close()


def call_luna(messages, *, max_output_tokens=2048, reasoning_effort="none",
              output_format=None, temperature=0.0, timeout=300):
    """
    Luna 한 번 호출 및 응답 원문, 시간과 실제 사용량 보존

    측정 기준:
        - 네트워크와 서버 처리 시간을 포함한 사용자 관측 지연
        - 원격 서버 GPU와 RAM은 미확인, 로컬 GPU 수치로 대체 없음
        - SDK 자동 재시도 0회, 실패 후 임의 추가 결제 호출 없음
        - 부모 프로세스에서 전체 호출 제한 적용
        - 시간 초과 후 제공되지 않은 사용량과 비용은 미확인 유지

    입력: 대화, 출력 형식과 생성 및 시간 제한
    반환: 원본 이벤트와 최종 응답, 사용량 및 비용 사전
    구분: API 종료 이벤트, 완전한 최종 텍스트와 형식 검사는 별개
    효과: 유료 요청 1회, 서버 내부 시간과 GPU 사용량은 미측정
    """
    if timeout <= 0:
        raise ValueError("양의 호출 시간 제한 필요")
    if not luna_key_available():
        raise ValueError("OPENAI_API_KEY 설정 필요")
    request = build_luna_request(messages, max_output_tokens=max_output_tokens,
                                 reasoning_effort=reasoning_effort,
                                 output_format=output_format, temperature=temperature)
    record = {"run_id": str(uuid4()), "started_at": datetime.now(timezone.utc).isoformat(),
              "request": request, "request_hash": hashlib.sha256(_to_json(request).encode()).hexdigest(),
              "messages_hash": hashlib.sha256(_to_json(messages).encode()).hexdigest(),
              "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "sdk_version": importlib.metadata.version("openai"), "sdk_max_retries": 0,
              "status": "running", "raw_lines": [], "content": "", "refusal": "",
              "errors": [], "response_metadata": None, "provider_response": None,
              "resource_samples": [], "usage": None, "final_response_complete": False}
    # ------------------------------ * 별도 수신 프로세스와 전체 제한 시간 준비 * ------------------------------
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_receive_luna, args=(request, writer, timeout))
    started = time.perf_counter()
    first_content = None
    process.start()
    writer.close()
    try:
        while True:
            if time.perf_counter() - started >= timeout:
                record["status"] = "timeout"
                break
            if reader.poll(0.05):
                try:
                    event, value = reader.recv()
                except EOFError:
                    break
                if event == "event":
                    record["raw_lines"].append(_to_json(value))
                    event_type = value.get("type")
                    if event_type == "response.output_text.delta":
                        record["content"] += value.get("delta", "")
                        if first_content is None:
                            first_content = time.perf_counter() - started
                    elif event_type == "response.refusal.delta":
                        record["refusal"] += value.get("delta", "")
                    elif event_type in ("response.completed", "response.incomplete", "response.failed"):
                        response = value["response"]
                        record["provider_response"] = response
                        final_text = "".join(
                            part.get("text", "") for item in response.get("output", [])
                            if item.get("type") == "message" for part in item.get("content", [])
                            if part.get("type") == "output_text"
                        )
                        record["streamed_content"] = record["content"]
                        if final_text:
                            if record["content"] and final_text != record["content"]:
                                record["errors"].append({"type": "stream_content_mismatch",
                                                         "detail": "중간 문자열과 최종 원문 불일치"})
                            record["content"] = final_text
                        record["usage"] = response.get("usage")
                        record["status"] = "failed" if event_type == "response.failed" else "completed"
                        reason = ("stop" if event_type == "response.completed" else
                                  (response.get("incomplete_details") or {}).get("reason", "failed"))
                        record["response_metadata"] = {"model": response.get("model"),
                                                       "response_id": response.get("id"),
                                                       "done_reason": reason,
                                                       "service_tier": response.get("service_tier")}
                        break
                    elif event_type == "error":
                        record["errors"].append({"type": "server", "detail": value.get("message")})
                elif event == "error":
                    record["errors"].append(value)
                    break
                else:
                    break
            elif not process.is_alive():
                break
        if record["status"] == "running":
            record["status"] = "failed"
            if not record["errors"]:
                record["errors"].append({"type": "incomplete_stream", "detail": "최종 이벤트 없이 종료"})
    finally:
        record["wall_seconds"] = time.perf_counter() - started
        record["first_content_seconds"] = first_content
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        reader.close()
    # ------------------------------ * 정상 완료 여부와 실제 사용량 비용 확정 * ------------------------------
    response = record["provider_response"] or {}
    record["final_response_complete"] = (
        record["status"] == "completed" and response.get("status") == "completed"
        and bool(record["content"].strip()) and not record["errors"] and not record["refusal"]
    )
    record["cost"] = calculate_luna_cost(record["usage"],
                                          service_tier=response.get("service_tier", "default"))
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    return record


def prepare_poc_cloud(local_experiment_id, db_path="data/evaluation.db"):
    """
    고정한 로컬 PoC에서 사전 지정 영문 다섯 문항을 그대로 가져오는 Cloud 계획 등록

    조건:
        - 로컬 결과나 현재 파일에서 문항을 다시 선택하지 않고 저장된 계획 사용
        - 영문 지시문, 입력 해시, 출력 스키마와 항목별 정답의 동일성 유지
        - Cloud에서 지원하지 않는 seed, 로컬 문맥 크기와 GPU 옵션의 전송 제외
        - 유료 호출 없는 준비 단계, API 키 원문의 저장 금지

    입력: 고정 로컬 본 비교 ID와 작업 DB
    반환: 사전 지정 다섯 문항의 Cloud 실험 ID
    제약: 같은 지시문과 자료 사용, 각 문항 한 번의 고정 순서
    효과: 계획 등록만 수행, 유료 API 호출 없음
    """
    import copy
    from .evaluation_storage import load_experiment, save_experiment

    local = load_experiment(local_experiment_id, db_path)
    if local["config"]["evaluation_kind"] != "poc_main":
        raise ValueError("고정된 로컬 PoC 계획 필요")
    ids = local["config"]["cloud_case_ids"]
    if ids != ["Q01", "Q03", "Q06", "Q08", "Q10"]:
        raise ValueError("사전 지정 Cloud 문항 불일치")
    cases = [copy.deepcopy(c) for c in local["dataset"]["cases"]
             if c["case_id"] in ids and c["language"] == "en"]
    if len(cases) != 5:
        raise ValueError("영문 다섯 문항 필요")
    schedule = [{"slot_id": f"main/{c['case_id']}/en/1/{LUNA_MODEL}",
                 "case_id": c["case_id"], "language": "en", "model": LUNA_MODEL,
                 "repeat": 1, "phase": "main"} for c in cases]
    root = Path(__file__).resolve().parent.parent
    sources = ["modules/evaluation_cloud.py", "modules/evaluation_storage.py",
               "modules/evaluate_response.py", "modules/json_utils.py",
               "tests/__init__.py", "tests/test_evaluation_cloud.py",
               "tests/test_evaluation_local.py", "uv.lock"]
    config = {
        "evaluation_kind": "poc_cloud", "parent_local_experiment_id": local_experiment_id,
        "parent_config_hash": local["config_hash"], "parent_dataset_hash": local["dataset_hash"],
        "instructions": {"en": local["config"]["instructions"]["en"]},
        "response_schema": local["config"]["response_schema"],
        "models": {LUNA_MODEL: {"provider": "openai", "num_predict": 2048,
                               "reasoning_effort": "none", "temperature": 0.0, "timeout": 300}},
        "schedule": schedule, "automatic_retries": 0, "cloud_case_ids": ids,
        "scoring_policy": local["config"]["scoring_policy"], "pricing": dict(LUNA_PRICING),
        "sdk_version": importlib.metadata.version("openai"),
        "source_hashes": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
        "source_contents": {p: (root / p).read_text(encoding="utf-8") for p in sources},
        "comparison_limits": "Local has two repeats per question, Cloud has one. Provider tokens, sampling and remote hardware are not identical."
    }
    eid = "poc-cloud-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    save_experiment(eid, config, {"cases": cases}, db_path)
    return eid


def run_poc_cloud(experiment_id, db_path="data/evaluation.db"):
    """
    등록한 Luna 다섯 문항을 한 번씩 실행하고 원문, 비용과 검사 결과 저장

    처리:
        - 실행 전 고정 코드 해시 및 개인 키 설정 확인
        - 호출 전에 시도 등록, 실패한 시도의 자동 재호출 제외
        - 공개 최종 응답과 원본 이벤트의 보존, 실패 결과의 임의 hold 대체 금지
        - 사용량이 없으면 비용 미확인 유지, 실제 청구서와 단가 계산값의 구분

    입력: 고정 Cloud 계획 ID와 작업 DB
    반환: 이번 실행에서 새로 종료한 호출 수
    보존: 실패와 미완료 원문, 실제 토큰과 비용 및 요청 해시
    제한: 등록된 위치의 자동 재호출 없음
    """
    from .evaluation_storage import load_experiment, load_attempts, begin_attempt, finish_attempt
    from .evaluate_response import evaluate_case_response

    experiment = load_experiment(experiment_id, db_path)
    config = experiment["config"]
    if config["evaluation_kind"] != "poc_cloud":
        raise ValueError("Cloud PoC 계획 필요")
    root = Path(__file__).resolve().parent.parent
    for relative, expected in config["source_hashes"].items():
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("확정 후 코드 변경: " + relative)
    if not luna_key_available():
        raise ValueError("OPENAI_API_KEY 설정 필요")
    existing = {a["slot_id"] for a in load_attempts(experiment_id, db_path)}
    count = 0
    for slot in config["schedule"]:
        if slot["slot_id"] in existing:
            continue
        case = next(c for c in experiment["dataset"]["cases"] if c["case_id"] == slot["case_id"])
        messages = [{"role": "system", "content": config["instructions"]["en"]},
                    {"role": "user", "content": _to_json(case["input"])}]
        model = config["models"][LUNA_MODEL]
        request = build_luna_request(messages, max_output_tokens=model["num_predict"],
                                     reasoning_effort=model["reasoning_effort"],
                                     output_format=config["response_schema"],
                                     temperature=model["temperature"])
        attempt = begin_attempt(experiment_id, slot["slot_id"], slot["case_id"],
                                LUNA_MODEL, 1, request, phase="main", db_path=db_path)
        if attempt is None:
            continue
        record = call_luna(messages, max_output_tokens=model["num_predict"],
                           reasoning_effort=model["reasoning_effort"],
                           output_format=config["response_schema"],
                           temperature=model["temperature"], timeout=model["timeout"])
        record.update({"attempt_id": attempt, "experiment_id": experiment_id,
                       "case_id": slot["case_id"], "language": "en",
                       "repeat_number": 1, "phase": "main"})
        evaluation = {"api_success": record["status"] == "completed",
                      "final_completed": record["final_response_complete"],
                      "input_status": "valid" if record["final_response_complete"] else "unknown"}
        try:
            evaluation["assessment"] = evaluate_case_response(case, record["content"], experiment["dataset_hash"])
        except Exception as error:
            evaluation["grading_error"] = str(error)
        finish_attempt(attempt, record, evaluation, db_path)
        saved = next(a for a in load_attempts(experiment_id, db_path) if a["attempt_id"] == attempt)
        if saved["record"]["content"] != record["content"]:
            raise RuntimeError("Cloud 원문 저장 불일치")
        count += 1
        print(json.dumps({"slot_id": slot["slot_id"], "attempt_id": attempt,
                          "status": record["status"], "complete": record["final_response_complete"],
                          "seconds": record["wall_seconds"], "cost": record["cost"],
                          "errors": record["errors"]}, ensure_ascii=False), flush=True)
        # 인증 또는 결제 조건 오류는 같은 실패를 다섯 번 반복하지 않고 즉시 중지
        if any(error.get("status") in (401, 402, 403, 429) for error in record["errors"]):
            raise RuntimeError("Cloud 접근 또는 사용 한도 확인 필요, 남은 문항 미실행")
    return count


def main():
    """
    Cloud 계획 등록과 유료 실행을 별도 명령으로 구분하는 진입점

    입력: 기준 로컬 실험 ID 또는 실행할 Cloud 실험 ID, 작업 DB 경로
    처리: 공통 다섯 문항의 계획 등록 또는 등록된 계획의 유료 API 호출
    출력: 새 계획 ID 또는 이번 실행에서 종료한 호출 수의 JSON 출력
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment_id", nargs="?")
    parser.add_argument("--prepare-from-local")
    parser.add_argument("--db", default="data/evaluation.db")
    args = parser.parse_args()
    if args.prepare_from_local:
        print(json.dumps({"experiment_id": prepare_poc_cloud(args.prepare_from_local, args.db)}))
    elif args.experiment_id:
        run_poc_cloud(args.experiment_id, args.db)
    else:
        parser.error("--prepare-from-local 또는 experiment_id 필요")


if __name__ == "__main__":
    main()
