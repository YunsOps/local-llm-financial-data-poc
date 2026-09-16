"""
SQLite 원본의 PoC 집계와 Markdown 보고서 작성

집계 기준:
    - 모델과 입력 언어별 분리
    - 미실행, 실행 중, 실패와 완료의 구분
    - 미측정값과 미검토 항목을 0으로 대체하지 않는 처리
    - 필수 사실 충족률과 추가 오류, 형식 준수의 별도 표시

입력: 보존된 실험 ID와 DB 경로, 추가 모델 호출 없음
"""

import argparse
import copy
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from decimal import Decimal
from datetime import datetime
from pathlib import Path

from .evaluation_storage import load_attempts, load_experiment


def _distribution(values):
    """
    실측값이 있는 표본만 집계, 누락값의 0 치환 방지

    입력: 측정값 또는 None의 목록
    반환: 유효 표본 수 n, 평균, 중앙값과 최솟값 및 최댓값
    기준: bool과 비정상 실수 제외, 유효 표본이 없으면 통계값 None
    """
    values = [value for value in values
              if type(value) in (int, float) and math.isfinite(value)]
    if not values:
        return {"n": 0, "min": None, "median": None, "mean": None, "max": None}
    return {"n": len(values), "min": min(values), "median": statistics.median(values),
            "mean": statistics.mean(values), "max": max(values)}


def _ratio(numerator, denominator):
    """
    분자와 분모의 동시 보존, 빈 집단의 비율은 미정 처리

    입력: 분자와 분모
    반환: numerator, denominator와 rate 사전
    기준: 분모 0인 경우 rate=None, 0%와 미확인 상태의 구분
    """
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def _tokens(record):
    """
    제공자가 실제로 반환한 토큰 수 조회, 문자 수를 토큰으로 추정하지 않는 기준

    입력: 로컬 또는 Cloud 원본 기록
    반환: 입력 토큰과 출력 토큰의 쌍
    우선: Cloud usage가 존재하면 해당 값, 그 외 Ollama 통계 사용
    """
    metadata = record.get("response_metadata") or {}
    usage = record.get("usage") or {}
    if usage:
        return usage.get("input_tokens"), usage.get("output_tokens")
    return metadata.get("prompt_eval_count"), metadata.get("eval_count")


def _elapsed_seconds(start, finish):
    """
    저장된 UTC 시각의 차이 계산, 시각 누락 또는 역전 시 미정 유지

    입력: 시간대가 있는 시작 및 종료 ISO 시각
    반환: 0 이상의 경과 초 또는 None
    제외: 시각 누락, 시간대 누락, 잘못된 문자열과 역전된 구간
    """
    if not start or not finish:
        return None
    try:
        first = datetime.fromisoformat(start)
        last = datetime.fromisoformat(finish)
        if first.tzinfo is None or last.tzinfo is None:
            return None
        seconds = (last - first).total_seconds()
        return seconds if seconds >= 0 else None
    except (ValueError, TypeError):
        return None

def _server_seconds(record, name):
    """
    Ollama가 반환한 나노초 통계만 초로 변환, 제공하지 않은 값은 미정 유지

    입력: 호출 기록과 서버 통계 필드명
    반환: 나노초를 변환한 초 또는 None
    범위: 서버가 실제로 제공한 0 이상의 수치
    """
    value = (record.get("response_metadata") or {}).get(name)
    return value / 1000000000 if type(value) in (int, float) and value >= 0 else None


def _server_speed(record, count_name, duration_name):
    """
    서버의 처리 토큰 수와 해당 처리 시간으로 속도 계산, 로딩 시간 포함 전체 속도와 구분

    입력: 호출 기록, 토큰 필드명과 처리 시간 필드명
    반환: 해당 단계의 tokens/s 또는 None
    기준: 처리 시간이 0이거나 누락된 경우 계산 생략
    """
    duration = _server_seconds(record, duration_name)
    count = (record.get("response_metadata") or {}).get(count_name)
    return count / duration if duration and type(count) is int else None


def _loaded_model(record):
    """
    응답 직후 적재 목록에서 실제 요청한 모델의 항목만 선택

    입력: 호출 기록의 요청과 응답 직후 적재 목록
    반환: 요청한 모델 태그에 대응하는 항목 또는 None
    목적: 다른 모델의 메모리 수치를 잘못 합산하는 오류 방지
    """
    models = (record.get("loaded_models") or {}).get("models") or []
    name = record.get("request", {}).get("model")
    return next((m for m in models if m.get("name") == name or m.get("model") == name), None)


def _loaded_mib(record, field):
    """
    Ollama 적재 크기를 바이트에서 MiB로 변환, 미측정 값의 0 치환 금지

    입력: 호출 기록과 size 또는 size_vram 필드명
    반환: 바이트를 1,048,576으로 나눈 MiB 또는 None
    """
    value = (_loaded_model(record) or {}).get(field)
    return value / 1048576 if type(value) is int and value >= 0 else None


def _offload(record):
    """
    적재 모델의 전체 크기와 GPU 적재 크기 비교, 측정 누락을 CPU 적재 없음으로 간주하지 않는 기준

    입력: 모델 적재 정보가 있는 호출 기록
    반환: full_gpu, partial_cpu 또는 unknown
    한계: 메타데이터의 크기 비교 결과이며 토큰별 CPU 실행량의 실측과 구분
    """
    model = _loaded_model(record)
    if model is None:
        return "unknown"
    total, gpu = model.get("size"), model.get("size_vram")
    if type(total) is not int or type(gpu) is not int or total <= 0 or gpu < 0:
        return "unknown"
    return "partial_cpu" if gpu < total else "full_gpu"

def _summarize_group(slots, attempts):
    """
    같은 모델과 단계에 속한 실행 결과의 집계

    분모 기준:
        - 계획 수, 등록 수, 종료 수, 최종 응답 수의 별도 표시
        - 완료하지 않은 위치와 실행 중인 위치의 실패 판정 금지
        - 형식 통과율은 종료된 모든 시도를 분모로 사용
        - 거래 규칙은 형식이 유효하여 검사 가능한 응답에서 별도 집계
        - 사실 검토는 등록된 검토 항목만 집계, 전체 문장의 자동 정답 판정 없음

    입력: 한 조건의 계획 위치 목록과 실제 시도 목록
    반환: 완료 상태, 형식과 거래 제한, 시간과 자원 및 검토 통계
    기준: 지표마다 사용 가능한 원본 수와 분모를 별도 표시
    """
    finished = [a for a in attempts if a["status"] != "running"]
    records = [a["record"] for a in finished if a.get("record")]
    assessments = [(a.get("evaluation") or {}).get("assessment") or {} for a in finished]
    available_rules = [a["trading_valid"] for a in assessments
                       if type(a.get("trading_valid")) is bool]
    costs = [(r.get("cost") or {}).get("usd") for r in records]
    costs = [Decimal(str(value)) for value in costs if value is not None]
    reviews = []
    completion_verdicts = {"completed_response": Counter(), "incomplete_response": Counter()}
    reviewed_attempts = 0
    for attempt in finished:
        entries = (attempt.get("evaluation") or {}).get("reviews", [])
        # 같은 검토 방식의 정정은 마지막 항목 적용, 이전 검토 원문은 DB에 유지
        latest = {}
        for entry in entries:
            review = entry["review"]
            latest[review.get("method", "unspecified")] = review
        reviews.extend(latest.values())
        required = [item for review in latest.values() for item in review.get("required_items", [])]
        if required:
            reviewed_attempts += 1
            # 생성 한도로 끝난 일부 응답의 사실 확인을 정상 완료 응답의 정확성과 혼합하지 않는 구분
            state = ("completed_response" if (attempt.get("evaluation") or {}).get("final_completed") is True
                     else "incomplete_response")
            completion_verdicts[state].update(item["verdict"] for item in required)
    verdicts = Counter(item["verdict"] for review in reviews
                       for item in review.get("required_items", []))
    result = {
        "planned": len(slots), "registered": len(attempts), "finished": len(finished),
        "unstarted": len(slots) - len(attempts),
        "statuses": dict(Counter(a["status"] for a in attempts)),
        "api_success": _ratio(sum((a.get("evaluation") or {}).get("api_success") is True
                                 for a in finished), len(finished)),
        "final_completed": _ratio(sum((a.get("evaluation") or {}).get("final_completed") is True
                                     for a in finished), len(finished)),
        "strict_json": _ratio(sum(a.get("json_valid") is True for a in assessments), len(finished)),
        "strict_schema": _ratio(sum(a.get("schema_valid") is True for a in assessments), len(finished)),
        "trading_rules_among_parseable": _ratio(sum(available_rules), len(available_rules)),
        "input_status": dict(Counter((a.get("evaluation") or {}).get("input_status", "unknown")
                                    for a in finished)),
        "format_errors": dict(Counter(error["code"] for a in assessments
                                     for error in a.get("errors", []))),
        "wall_seconds": _distribution([r.get("wall_seconds") for r in records]),
        "server_load_seconds": _distribution([_server_seconds(r, "load_duration") for r in records]),
        "server_input_seconds": _distribution([_server_seconds(r, "prompt_eval_duration") for r in records]),
        "server_generation_seconds": _distribution([_server_seconds(r, "eval_duration") for r in records]),
        "server_input_tokens_per_second": _distribution([_server_speed(r, "prompt_eval_count", "prompt_eval_duration") for r in records]),
        "server_generation_tokens_per_second": _distribution([_server_speed(r, "eval_count", "eval_duration") for r in records]),
        "local_offload": dict(Counter(_offload(r) for r in records)),
        "model_vram_mib_after_response": _distribution([_loaded_mib(r, "size_vram") for r in records]),
        "model_total_size_mib_after_response": _distribution([_loaded_mib(r, "size") for r in records]),
        "loaded_context_tokens": _distribution([(_loaded_model(r) or {}).get("context_length") for r in records]),
        "loaded_quantization": dict(Counter(((_loaded_model(r) or {}).get("details") or {}).get("quantization_level", "unknown") for r in records)),
        "done_reasons": dict(Counter((r.get("response_metadata") or {}).get("done_reason", "unreported") for r in records)),
        "record_elapsed_seconds": _distribution([_elapsed_seconds(r.get("started_at"), r.get("finished_at")) for r in records]),
        "attempt_elapsed_seconds": _distribution([_elapsed_seconds(a.get("started_at"), a.get("finished_at")) for a in finished]),
        "first_content_seconds": _distribution([r.get("first_content_seconds") for r in records]),
        "input_tokens": _distribution([_tokens(r)[0] for r in records]),
        "output_tokens": _distribution([_tokens(r)[1] for r in records]),
        "peak_whole_gpu_mib": _distribution([r.get("peak_gpu_used_mib") for r in records]),
        "peak_whole_ram_mib": _distribution([r.get("peak_ram_used_mib") for r in records]),
        "peak_whole_cpu_percent": _distribution([r.get("peak_cpu_utilization_percent") for r in records]),
        "cost_usd": {"n": len(costs), "sum": str(sum(costs)) if costs else None},
        "review_count": len(reviews), "required_item_verdicts": dict(verdicts),
        "required_item_pass": _ratio(verdicts["pass"], sum(verdicts.values())),
        "required_review_coverage": _ratio(reviewed_attempts, len(finished)),
        "required_items_by_completion": {
            state: {"verdicts": dict(values), "pass": _ratio(values["pass"], sum(values.values()))}
            for state, values in completion_verdicts.items()},
        "review_warning": "Codex의 원본 대조 검토, 전체 항목 집계에는 미완료 응답의 확인 가능한 부분도 포함, 완료 여부별 집계와 함께 해석, 사람의 독립 재검토 및 전체 문장 정확도와 구분",
    }
    # 같은 시드로 반복한 응답의 재현성 확인, 반복을 독립 사례 수로 해석하지 않는 기준
    by_case = defaultdict(list)
    for a in finished:
        if a.get("record") and a["record"].get("final_response_complete"):
            by_case[a["case_id"]].append(a["record"]["content"])
    pairs = [values for values in by_case.values() if len(values) == 2]
    result["identical_repeat_text"] = _ratio(sum(x[0] == x[1] for x in pairs), len(pairs))
    result["rounds"] = {}
    for repeat in sorted({a["repeat_number"] for a in finished}):
        rows = [a for a in finished if a["repeat_number"] == repeat]
        result["rounds"][str(repeat)] = {
            "finished": len(rows),
            "wall_seconds": _distribution([(a.get("record") or {}).get("wall_seconds") for a in rows]),
            "schema_pass": sum(((a.get("evaluation") or {}).get("assessment") or {}).get("schema_valid") is True
                               for a in rows),
        }
    return result


def _poc_review(attempt):
    """
    같은 검토 방식의 가장 최근 원본 대조 결과 조회, 미검토와 0점 구분

    입력: 하나의 시도 기록
    반환: 같은 검토 방식의 마지막 항목 또는 None
    목적: 수정 전 검토의 중복 합산 방지
    """
    entries = (attempt.get("evaluation") or {}).get("reviews", [])
    return next((entry["review"] for entry in reversed(entries)
                 if entry["review"].get("method") == "source_grounded_required_items"), None)


def summarize_poc(experiment_id, db_path="data/evaluation.db", *, first_repeat_only=False):
    """
    PoC의 모델과 입력 언어별 실측 및 사전 점수 집계

    기준:
        - 영문 40회와 한국어 12회, Cloud 5회의 분모를 섞지 않는 처리
        - 실행 중과 미검토를 실패나 0점으로 확정하지 않는 처리
        - 전체 시도 품질은 미완료 응답을 0점으로 처리, 완료 응답 조건부 점수 별도 표시
        - 같은 숫자를 사용한 언어 비교는 대응 문항과 반복만 따로 집계
        - 필수 항목 점수와 그 밖의 잘못된 설명을 별도 기록

    입력: PoC 실험 ID와 원본 DB
    반환: 모델 및 언어별 품질, 시간과 자원, 대응 문항과 입력 일치 점검
    효과: 원본 조회와 계산만 수행, 새로운 의미 판정과 모델 호출 없음
    """
    experiment = load_experiment(experiment_id, db_path)
    config = copy.deepcopy(experiment["config"])
    if not config["evaluation_kind"].startswith("poc_"):
        raise ValueError("PoC 기록 필요")
    attempts = load_attempts(experiment_id, db_path)
    selected_cases = experiment["dataset"]["cases"]
    if first_repeat_only:
        # 서로 다른 반복 수를 동일한 1회차 영문 10문항으로 맞추는 대응 비교
        config["schedule"] = [s for s in config["schedule"] if s["language"] == "en" and s["repeat"] == 1]
        selected_slots = {s["slot_id"] for s in config["schedule"]}
        attempts = [a for a in attempts if a["slot_id"] in selected_slots]
        selected_cases = [c for c in selected_cases if c["language"] == "en"]
    cases = {(c["case_id"], c["language"]): c for c in selected_cases}
    # ------------------------------ * 모델과 입력 언어별 독립 집계 * ------------------------------
    groups = {}
    for model, language in sorted({(s["model"], s["language"]) for s in config["schedule"]}):
        slots = [s for s in config["schedule"] if s["model"] == model and s["language"] == language]
        group = [a for a in attempts if a["slot_id"] in {s["slot_id"] for s in slots}]
        info = _summarize_group(slots, group)
        info["review_warning"] = "필수 항목 점수, 추가 오류 및 미완료 응답의 구분"
        finished = [a for a in group if a["status"] != "running"]
        # 사용자 중단과 모델 자체의 실패를 구분, 중단 시도의 품질 점수 분모 제외
        quality_finished = [a for a in finished if a["status"] != "interrupted"]
        reviewed = [a for a in quality_finished if _poc_review(a)]
        info["operator_interrupted"] = len(finished) - len(quality_finished)
        completed = [a for a in finished if (a.get("evaluation") or {}).get("final_completed")]
        points = sum(item["verdict"] == "pass" for a in reviewed
                     if (a.get("evaluation") or {}).get("final_completed")
                     for item in _poc_review(a)["required_items"])
        info["quality"] = {"points": points, "denominator": len(quality_finished)*3,
                           "reviewed_calls": len(reviewed), "unreviewed_calls": len(quality_finished)-len(reviewed),
                           "final": len(quality_finished) == len(slots) and len(reviewed) == len(quality_finished),
                           "completed_only": _ratio(points, len(completed)*3)}
        info["additional_findings"] = dict(Counter(f["type"] for a in reviewed
                                                  for f in _poc_review(a).get("findings", [])))
        # 출력 언어는 사람이 읽어 저장한 위반 근거 기준, JSON 자료형 검사와 별도 표시
        language_reviewed = [a for a in reviewed if (a.get("evaluation") or {}).get("final_completed")]
        language_failures = sum(any(f["type"] == "output_language"
                                   for f in _poc_review(a).get("findings", [])) for a in language_reviewed)
        info["english_output_requirement"] = {
            "reviewed_calls": len(language_reviewed), "violations": language_failures,
            "unassessable_incomplete_calls": len(reviewed) - len(language_reviewed),
            "passes": len(language_reviewed)-language_failures,
            "note": "공개 응답의 원본 대조 결과, 한국어 문체 품질 점수 아님"}
        causes = Counter()
        for a in reviewed:
            for item in _poc_review(a)["required_items"]:
                cause = ("incomplete_response" if not (a.get("evaluation") or {}).get("final_completed") else
                         "pass" if item["verdict"] == "pass" else
                         "partial_missing" if item["verdict"] == "partial" else
                         "entirely_missing" if not item["quote"] else "incorrect_or_contradictory")
                causes[cause] += 1
        info["required_item_outcomes"] = dict(causes)

        reflection = [a for a in reviewed if a["case_id"] in config.get("scoring_policy", {}).get("reflection_case_ids", [])]
        info["reflection"] = _ratio(
            sum(i["verdict"] == "pass" for a in reflection if (a.get("evaluation") or {}).get("final_completed")
                for i in _poc_review(a)["required_items"]), len(reflection)*3)
        info["completed_valid_json"] = sum(
            (a.get("evaluation") or {}).get("final_completed") is True
            and ((a.get("evaluation") or {}).get("assessment") or {}).get("schema_valid") is True
            and (a.get("record") or {}).get("wall_seconds", float("inf")) <= config["models"][model]["timeout"]
            for a in finished)
        groups[model+"/"+language] = info
    # ------------------------------ * 실제 전송과 고정 입력의 일치 확인 * ------------------------------
    checks = []
    for a in attempts:
        if not a.get("record"):
            continue
        slot = next(s for s in config["schedule"] if s["slot_id"] == a["slot_id"])
        case = cases[(slot["case_id"], slot["language"])]
        record = a["record"]
        request = a["request"]
        messages = request.get("messages", request.get("input", []))
        wanted = [{"role": "system", "content": config["instructions"][slot["language"]]},
                  {"role": "user", "content": json.dumps(case["input"], ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":"), allow_nan=False)}]
        model = config["models"][slot["model"]]
        tokens = _tokens(record)[0]
        checks.append({"slot_id": a["slot_id"], "same_messages_as_plan": messages == wanted,
                       "input_hash": case["input_hash"],
                       "input_plus_output_budget_fits": tokens + model["num_predict"] <= model["num_ctx"]
                           if tokens is not None and "num_ctx" in model else None,
                       "automatic_truncation_disabled": request.get("truncate") is False
                           or request.get("truncation") == "disabled"})
    paired = {}
    bilingual = {c["case_id"] for c in selected_cases if c["language"] == "ko"}
    if bilingual:
        for model in config["models"]:
            paired[model] = {}
            for lang in ("en", "ko"):
                slots = [s for s in config["schedule"] if s["model"] == model and s["language"] == lang
                         and s["case_id"] in bilingual]
                group = [a for a in attempts if a["slot_id"] in {s["slot_id"] for s in slots}]
                paired[model][lang] = _summarize_group(slots, group)
    return {"experiment_id": experiment_id, "config_hash": experiment["config_hash"],
            "dataset_hash": experiment["dataset_hash"], "groups": groups,
            "paired_language_cases": sorted(bilingual), "paired_language": paired,
            "input_checks": checks, "acceptance": config.get("acceptance"),
            "comparison_scope": "영문 1회차, 모델당 10응답과 30점" if first_repeat_only else "등록된 전체 반복",
            "notice": "사전 필수 항목의 충족률과 전체 문장의 정확도 구분, 10회 비교에서 20회 통과 기준 확정 금지"}


def write_poc_report(experiment_id, output_path, db_path="data/evaluation.db"):
    """
    SQLite 원본을 다시 읽어 모델별 수치와 모든 응답 및 항목별 근거를 Markdown으로 작성

    주의:
        - 새 모델 호출이나 정답 자동 생성 없음
        - 저장된 검토가 없으면 미검토로 표시, 점수 확정 금지
        - 보고서 코드의 해시와 실험 당시 코드 해시를 각각 기록
        - 숨겨진 추론 대신 공개 최종 응답만 수록

    입력: PoC 실험 ID, 출력 Markdown 경로와 원본 DB
    반환: 생성 경로, 바이트 수와 검토 확정 여부
    주의: 지정한 파일의 내용 교체, 수동 부록 보존을 위해 새 경로 사용
    """
    experiment = load_experiment(experiment_id, db_path)
    attempts = load_attempts(experiment_id, db_path)
    summary = summarize_poc(experiment_id, db_path)
    root = Path(__file__).resolve().parent.parent
    destination = (root / output_path).resolve()
    if destination.parent != (root / "reports").resolve() or destination.suffix != ".md":
        raise ValueError("reports 폴더의 Markdown 경로 필요")
    def code(value, lang="json"):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        return "\n\n" + chr(96)*3 + lang + "\n" + text + "\n" + chr(96)*3 + "\n"
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", "<br>")
    def number(value):
        return "미측정" if value is None else f"{value:.3f}"
    cloud = experiment["config"]["evaluation_kind"] == "poc_cloud"
    title = "KAN-204 Luna 동일 문항 비교" if cloud else "KAN-202 및 KAN-203 로컬 영문 및 한국어 비교"
    parts = ["# " + title + " 상세 시험 보고서\n",
        "## S. 배경과 문제\n\n고정된 비트코인 시장과 가상 계좌 자료를 모델이 읽고, 수치와 시각 및 과거 기록을 정확히 설명하는지 확인한 PoC입니다. 실제 주문, 수익률, 한국어 문체 품질은 평가하지 않았습니다. 긴 입력을 처리하는 절대 한계와 새로운 시장에서의 일반화는 이번 작은 기능 시험으로 확정할 수 없습니다.\n",
        "## T. 사전 질문과 기준\n\n[문항과 지시문 및 정답표](2026-09-15_KAN-201_poc-design.md)의 고정 기준을 사용했습니다. 영문 본 비교는 모델별 10문항 두 번, 한국어는 대응 3문항 두 번입니다. Cloud는 사전 지정 영문 5문항 한 번으로 반복 수가 다릅니다. 문항당 세 항목을 각각 1점 또는 0점으로 판정하고 부분 설명과 누락은 0점의 사유로 남겼습니다. 실패와 미완료는 전체 시도 점수에서 0점이며, 완료 응답 조건부 점수도 구분했습니다. 미검토 상태는 확정 점수로 표현하지 않았습니다.\n",
        "## A. 실제 실행과 기록\n\n실험 ID: " + experiment_id + "\n\n원본 DB: " + db_path + "\n\n코드: " +
        ("modules/evaluation_cloud.py의 prepare_poc_cloud(), run_poc_cloud(), call_luna()" if cloud else
         "modules/evaluation_poc.py의 prepare(), build_request(), run() 및 modules/evaluation_local.py의 call_ollama()") +
        ". 입력과 설정 및 소스 원문을 첫 호출 전에 SQLite에 고정했습니다. 호출 전에 시도 ID를 등록하고, 응답 저장 후 원문을 다시 읽어 일치를 확인했습니다. 재시도는 자동 실행하지 않았습니다.\n",
        code(".\\.venv\\Scripts\\python.exe -m modules." + ("evaluation_cloud" if cloud else "evaluation_poc") + " " + experiment_id, "powershell"),
        "실행 당시 소스 해시와 원문은 evaluation_experiments.config_json에 보존했습니다. 집계와 문서 작성 코드는 modules/evaluation_report.py이며 현재 SHA-256은 " + hashlib.sha256(Path(__file__).read_bytes()).hexdigest() + "입니다. 검토 저장 코드는 modules/evaluation_review.py이며 현재 SHA-256은 " + hashlib.sha256((root/"modules/evaluation_review.py").read_bytes()).hexdigest() + "입니다. 커밋 전 작업 버전이므로 Git HEAD만으로 실행 코드를 식별하지 않습니다.\n",
        code({k:v for k,v in experiment["config"].items() if k not in ("source_contents", "instructions", "schedule")}),
        "### 측정 정의\n\n전체 시간은 호출을 수행할 수신 프로세스 시작부터 최종 응답 수신까지의 시간입니다. 로컬은 로딩 포함, 사전 모델 조회와 이후 해제 및 DB 저장 시간은 제외입니다. 서버 로딩 및 입력 처리와 생성 시간은 Ollama의 나노초 통계를 초로 변환했습니다. 생성 속도는 생성 토큰 수를 서버 생성 시간으로 나눈 값입니다. Cloud 시간은 네트워크 포함이며 서버 내부 단계 시간과 VRAM은 미측정입니다. 토큰은 각 제공자가 반환한 값이므로 모델 사이 같은 토큰 수를 같은 작업량으로 간주하지 않았습니다.\n\nGPU 최대 사용량은 약 1초 간격의 GPU 전체 표본 최댓값으로 다른 앱을 포함합니다. 모델 적재량은 /api/ps size_vram으로 별도 조회했습니다. 소비전력과 로컬 전기요금은 미측정입니다. 모델마다 응답 후 해제했으며 운영체제 파일 캐시와 발열, 다른 앱은 통제하지 않았습니다.\n",
        "## R. 결과 집계\n\n확정 여부는 각 그룹의 quality.final로 확인합니다. 필수 항목의 점수와 추가로 발견한 잘못된 설명은 다른 지표입니다. 아래 수치의 n은 실제 기록이 있는 응답 수이며 누락값을 0으로 대체하지 않았습니다.\n"]
    parts.append("| 모델 / 언어 | 계획 | 종료 | 완전한 JSON | 필수 항목 점수 | 검토 완료 / 종료 | 시간 평균 / 중앙값 / 최소 / 최대(초), n | 입력 토큰 범위, n | 생성 토큰 범위, n |\n| --- | ---: | ---: | ---: | --- | --- | --- | --- | --- |\n")
    for name, group in summary["groups"].items():
        quality=group["quality"]; t=group["wall_seconds"]; inp=group["input_tokens"]; out=group["output_tokens"]
        parts.append("| "+name+f" | {group['planned']} | {group['finished']} | {group['completed_valid_json']} | {quality['points']}/{quality['denominator']}"+
            (" 확정" if quality["final"] else " 미확정")+f" | {quality['reviewed_calls']}/{group['finished']} | "+
            " / ".join(number(t[k]) for k in ("mean","median","min","max"))+f", n={t['n']} | {inp['min']}–{inp['max']}, n={inp['n']} | {out['min']}–{out['max']}, n={out['n']} |\n")
    parts.append("\n### 집계 원문과 입력 동일성 점검\n"+code(summary))
    parts.append("\n### 모든 시도의 시간과 토큰 및 원본 식별값\n\n| 실행 위치 | 시도 ID | 상태 | 전체 초 | 로딩 초 | 입력 처리 초 | 생성 초 | 입력/생성 토큰 | 생성 토큰/초 | 모델 VRAM MiB | GPU 전체 최대 MiB |\n| --- | --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |\n")
    for a in attempts:
        r=a.get("record") or {}
        parts.append("| "+a["slot_id"]+" | "+a["attempt_id"]+" | "+a["status"]+" | "+
                     " | ".join(number(v) for v in (r.get("wall_seconds"),_server_seconds(r,"load_duration"),
                         _server_seconds(r,"prompt_eval_duration"),_server_seconds(r,"eval_duration")))+" | "+
                     str(_tokens(r))+" | "+number(_server_speed(r,"eval_count","eval_duration"))+" | "+
                     number(_loaded_mib(r,"size_vram"))+" | "+number(r.get("peak_gpu_used_mib"))+" |\n")
    parts.append("\n### 문항별 공개 응답과 판정 근거\n\n원본과 계산식 대조 검토입니다. 인용과 원본 경로의 존재 여부도 저장 시 확인했습니다. 동일 원문의 반복 검토에는 원래 시도 ID를 표시했습니다.\n")
    for a in attempts:
        parts.append("\n#### "+a["slot_id"]+"\n")
        if not a.get("record"):
            parts.append("응답 기록 미완료.\n");continue
        r=a["record"]
        parts.append(code({"attempt_id":a["attempt_id"],"run_id":r["run_id"],
                           "started_at":r["started_at"],"finished_at":r.get("finished_at"),
                           "request_hash":a["request_hash"],"status":a["status"],
                           "response_metadata":r.get("response_metadata"),"usage":r.get("usage"),
                           "cost":r.get("cost"),"errors":r.get("errors")}))
        parts.append(code(r["content"]))
        review=_poc_review(a)
        if not review:
            parts.append("필수 항목 원본 대조 미검토.\n");continue
        case=next(c for c in experiment["dataset"]["cases"]
                  if c["case_id"]==a["case_id"] and c["language"]==r["language"])
        parts.append("\n| 항목 | 기대 결과와 원본 경로 | 판정 | 응답 인용 | 이유 |\n| --- | --- | --- | --- | --- |\n")
        for item,rubric in zip(review["required_items"],case.get("rubric",[])):
            parts.append("| "+item["id"]+" | "+cell(rubric["expected"]+" / "+", ".join(rubric["source_paths"]))+
                         " | "+item["verdict"]+" | "+cell(item["quote"])+" | "+cell(item["note"])+" |\n")
        if review.get("findings"):parts.append("\n추가 발견 사항:"+code(review["findings"]))
        if review.get("same_text_basis"):parts.append("\n동일 원문 검토의 재사용 근거:"+code(review["same_text_basis"]))
    parts.append("\n## 결론의 적용 범위\n\n이 보고서는 기록된 응답과 사전 기준의 대조 자료입니다. JSON 형식 통과가 올바른 판단을 보장하지 않으며, 필수 항목의 높은 점수도 응답의 모든 추가 주장이 정확하다는 뜻은 아닙니다. 특히 계산과 크기 비교, 소수 단위 주문, 시각과 손익을 혼동한 문장은 별도 오류 근거로 확인해야 합니다. 최종 선정과 도입 가능 여부는 KAN-205의 종합 보고서에서 필수 조건과 관측된 오류를 함께 검토합니다.\n")
    destination.write_text("\n".join(parts),encoding="utf-8")
    return {"report_path":str(destination),"bytes":destination.stat().st_size,
            "quality_final":all(g["quality"]["final"] for g in summary["groups"].values())}



def summarize_modes(baseline_id, reasoning_id, db_path="data/evaluation.db"):
    """
    같은 영문 문항의 비추론 및 추론 조건 대조

    입력: 비추론과 추론 실험 ID, 두 원본이 저장된 SQLite 경로
    반환: 모델과 모드별 집계, 실제 요청의 일치 여부, 완료 상태별 시간
    원칙: 기존 원본과 채점의 수정 없음, 모델 호출과 새로운 의미 판정 없음
    구분: 요청한 생성 길이와 실제 생성량, 전체 시도 시간과 유효 답변 시간
    """
    baseline = load_experiment(baseline_id, db_path)
    reasoning = load_experiment(reasoning_id, db_path)
    summaries = {"non_reasoning": summarize_poc(baseline_id, db_path),
                 "reasoning": summarize_poc(reasoning_id, db_path)}
    records = {"non_reasoning": load_attempts(baseline_id, db_path),
               "reasoning": load_attempts(reasoning_id, db_path)}
    # 동일 문항, 언어, 반복과 모델의 실제 전송 본문 연결
    original = {a["slot_id"]: a for a in records["non_reasoning"]}
    comparison = []
    for attempt in records["reasoning"]:
        if not attempt.get("record"):
            continue
        source = original[attempt["slot_id"]]
        expected = dict(source["request"])
        expected["think"] = True
        comparison.append({"slot_id": attempt["slot_id"],
                           "only_think_changed": expected == attempt["request"],
                           "reasoning_payload_matches_registered": attempt["record"].get("request") == attempt["request"],
                           "baseline_payload_matches_registered": source["record"].get("request") == source["request"]})
    groups = {}
    for mode, summary in summaries.items():
        config = (baseline if mode == "non_reasoning" else reasoning)["config"]
        for name, group in summary["groups"].items():
            if not name.endswith("/en"):
                continue
            model = name.rsplit("/", 1)[0]
            attempts = [a for a in records[mode] if a["model"] == model
                        and (a.get("record") or {}).get("language") == "en"
                        and a["status"] != "running"]
            # 정상 종료, 형식 준수와 제한 시간 충족을 함께 확인한 응답만 시간 선호 판정에 사용
            valid = [a for a in attempts if (a.get("evaluation") or {}).get("final_completed") is True
                     and ((a.get("evaluation") or {}).get("assessment") or {}).get("schema_valid") is True
                     and a["record"].get("wall_seconds", float("inf")) <= config["models"][model]["timeout"]]
            valid_ids = {a["attempt_id"] for a in valid}
            info = dict(group)
            info["valid_json_wall_seconds"] = _distribution([a["record"].get("wall_seconds") for a in valid])
            info["other_wall_seconds"] = _distribution([a["record"].get("wall_seconds") for a in attempts
                                                       if a["attempt_id"] not in valid_ids])
            requested = config["models"][model]["num_predict"]
            info["requested_num_predict"] = requested
            info["observed_generation_above_request"] = [a["attempt_id"] for a in attempts
                if (_tokens(a["record"])[1] or 0) > requested]
            info["empty_final_content"] = sum(not a["record"].get("content") for a in attempts)
            info["thinking_present_calls"] = sum(bool(a["record"].get("thinking")) for a in attempts)
            # 빈 최종 응답의 반복과 성공 답변의 재현성을 구분하기 위한 종료 상태 병기
            paired = defaultdict(list)
            for a in attempts:
                paired[a["case_id"]].append(a)
            info["repeated_outcome"] = {case_id: {
                "count": len(rows),
                "same_final_text": len(rows) == 2 and rows[0]["record"]["content"] == rows[1]["record"]["content"],
                "done_reasons": [(a["record"].get("response_metadata") or {}).get("done_reason") for a in rows]}
                for case_id, rows in paired.items()}
            groups[mode + "/" + model] = info
    return {"baseline_experiment_id": baseline_id, "reasoning_experiment_id": reasoning_id,
            "groups": groups, "actual_request_comparison": comparison,
            "input_checks_by_mode": {mode: value.get("input_checks", []) for mode, value in summaries.items()},
            "all_actual_requests_match": len(comparison) == 40 and all(c["only_think_changed"] and c["reasoning_payload_matches_registered"]
                and c["baseline_payload_matches_registered"] for c in comparison),
            "note": "같은 요청 설정의 결과 비교, 모델별 최적 설정이나 최대 추론 능력의 비교와 구분"}



def write_mode_report(baseline_id, reasoning_id, output_path, db_path="data/evaluation.db"):
    """
    기존 로컬 보고서에 두 실행 조건의 비교와 추론 원본을 통합하는 함수

    입력: 두 조건의 실험 ID, 기존 Markdown 보고서 경로, SQLite 경로
    보존: 기존 비추론 상세 기록을 표시 구간 안에 유지, 반복 작성 시 중복 방지
    출력: 모드별 완료율과 점수, 시간, 토큰, 자원, 실제 요청 대조와 호출별 근거
    제한: 미검토 점수의 확정 금지, 추론 과정 원문 대신 최종 답변과 종료 기록 수록
    """
    root = Path(__file__).resolve().parent.parent
    destination = (root / output_path).resolve()
    if destination.parent != (root / "reports").resolve() or destination.suffix != ".md":
        raise ValueError("reports 폴더의 기존 Markdown 경로 필요")
    original = destination.read_text(encoding="utf-8")
    begin, end = "<!-- NON_REASONING_RECORD_BEGIN -->", "<!-- NON_REASONING_RECORD_END -->"
    # 보고서의 이전 원문을 한 번만 감싸고 재작성 시 같은 구간을 그대로 재사용
    if begin in original:
        original = original.split(begin, 1)[1].split(end, 1)[0].strip()
    summary = summarize_modes(baseline_id, reasoning_id, db_path)
    experiment = load_experiment(reasoning_id, db_path)
    attempts = load_attempts(reasoning_id, db_path)
    def block(value):
        return "\n```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```\n"
    def number(value):
        return "판정 불가" if value is None else f"{value:.3f}"
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", "<br>")
    parts = ["# KAN-202 로컬 모델의 비추론 및 추론 비교 보고서\n",
        "## 상황\n\n노트북에서 실행하는 두 모델이 시장 자료와 가상 계좌 기록을 정확히 설명하는지 확인하는 PoC이다. 같은 자료를 사용하더라도 추론 여부에 따라 응답 완료, 필수 설명과 실행 시간이 달라질 수 있으므로 두 모드를 본 실험의 비교 조건으로 구분했다.\n",
        "## 과제\n\n영문 10문항을 모델별, 모드별 두 번 실행한 80회 비교이다. 평가 단위는 모델 하나와 모드 하나이며 20회 중 지정 JSON 19회 이상, 필수 설명 54/60점 이상, 판정 가능한 결정의 중대한 거래 제한 위반 0건을 요구한다. 회고 24점은 60점에 포함된 부분집합이다. 미완료 호출은 전체 시도 기준 0/3점으로 처리하되, 틀린 설명과 답변 부재를 구분한다. 한국어 12회, Cloud 5회와 준비 호출은 영문 모드 비교에 합산하지 않는다.\n",
        "## 행동\n\n비추론 실행일은 2026-09-15, 추론 실행일은 2026-09-16이다. 원본 날짜를 그대로 보존했다. modules/evaluation_poc.py의 prepare_mode()로 기존 영문 자료와 지시문, JSON 형식 및 모델 설정을 복사하고 think만 True로 변경했다. 같은 run()과 modules/evaluation_local.py의 call_ollama()로 한 모델씩 호출했다. 매 호출 후 모델을 해제했고 자동 재시도는 하지 않았다.\n",
        "두 모델 모두 Q4_K_M, num_ctx=8,192, num_predict=2,048, temperature=0, seed=42, 호출 제한 300초이다. 동일하게 통제한 대상은 요청 설정이며 실제 생성량은 서버 관측값으로 별도 기록한다. 준비 호출에서 Qwen의 eval_count가 2,331이었으므로 2,048을 절대적인 실측 상한으로 표현하지 않는다. 본 비교 중 결과에 맞춰 지시문이나 생성 설정을 바꾸지 않았다.\n",
        "```powershell\n.\\.venv\\Scripts\\python.exe -m modules.evaluation_poc --prepare-mode-from " + baseline_id + " --db " + db_path + "\n.\\.venv\\Scripts\\python.exe -m modules.evaluation_poc " + reasoning_id + " --db " + db_path + "\n.\\.venv\\Scripts\\python.exe -m modules.evaluation_report " + baseline_id + " --reasoning-id " + reasoning_id + " --db " + db_path + " --markdown " + output_path + "\n```\n",
        "채점 근거는 modules/evaluation_review.py로 저장하고, 이 표는 modules/evaluation_report.py의 summarize_modes()와 write_mode_report()로 원본을 다시 읽어 작성했다. 사람이 확인한 것으로 표시하지 않았으며, 검토 주체는 Codex이다.\n",
        "### 측정과 해석 범위\n\n전체 시간에는 모델 로딩, 입력 처리와 생성이 포함되며 호출 전 조회, 호출 후 해제와 DB 저장은 제외한다. 정상 JSON 응답의 시간과 미완료 등 나머지 시도의 시간을 분리한다. GPU 전체 최댓값은 약 1초 간격의 관측값이며 다른 앱도 포함한다. 모델 적재량은 응답 후 /api/ps 조회값이다. 실행 날짜, 발열, 운영체제 파일 캐시와 다른 앱의 영향은 완전히 통제하지 않았다.\n\n추론 조건의 서버 파일 로그는 보존하지 못했다. 따라서 비추론 기록에서 확인한 truncated=0을 추론 조건에 그대로 적용하지 않는다. 추론 조건에서는 실제 전송 본문 일치, truncate=False 및 shift=False 요청, 서버가 보고한 입력 토큰 수와 문맥 요청값의 관계를 확인한다. 설정상 여유와 서버 내부 잘림 여부의 직접 확인은 구분한다.\n",
        "## 결과\n\n| 조건 | 종료 / 계획 | 유효 JSON | 필수 설명 | 회고 | 전체 시간 평균, n | 정상 JSON 중앙값, n | 나머지 시도 중앙값, n |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n"]
    if any(g.get("operator_interrupted") for g in summary["groups"].values()):
        parts.insert(1, "**현재 상태: 사용자 요청에 따른 2,048토큰 조건의 반복 중단.** 완료된 원본만 집계하며, 수동 중단은 모델 실패 점수에 포함하지 않는다. 미실행과 중단이 남아 있어 20회 기준의 최종 점수로 확정하지 않는다.\n")
    for name,g in summary["groups"].items():
        q=g["quality"]; ref=g["reflection"]; valid=g["valid_json_wall_seconds"]; other=g["other_wall_seconds"]
        label=name.replace("non_reasoning/", "비추론 / ").replace("reasoning/", "추론 / ")
        parts.append(f"| {label} | {g['finished'] - g.get('operator_interrupted',0)}/{g['planned']} (수동 중단 {g.get('operator_interrupted',0)}) | {g['completed_valid_json']} | {q['points']}/{q['denominator']} " + ("확정" if q['final'] else "미확정") +
            f" | {ref['numerator']}/{ref['denominator']} | {number(g['wall_seconds']['mean'])}, n={g['wall_seconds']['n']} | {number(valid['median'])}, n={valid['n']} | {number(other['median'])}, n={other['n']} |\n")
    parts.append("\n정상 응답의 표본이 0이면 정확도와 응답 시간 선호 조건은 판정 불가이다. 전체 시도 점수 0점은 최종 설명을 확보하지 못했다는 운영 결과이며, 모델이 모든 사실을 잘못 알고 있음을 뜻하지 않는다. 최종 답변이 없어 거래 제한을 검사할 수 없는 경우에도 통과로 처리하지 않는다.\n\n| 조건 | 입력 토큰 범위 | 서버 집계 생성 토큰 범위 | 모델 적재 최대 MiB | GPU 전체 최대 MiB | 종료 사유 |\n| --- | --- | --- | --- | --- | --- |\n")
    for name,g in summary["groups"].items():
        label=name.replace("non_reasoning/", "비추론 / ").replace("reasoning/", "추론 / ")
        parts.append(f"| {label} | {g['input_tokens']['min']}–{g['input_tokens']['max']}, n={g['input_tokens']['n']} | {g['output_tokens']['min']}–{g['output_tokens']['max']}, n={g['output_tokens']['n']} | {number(g['model_vram_mib_after_response']['max'])} | {number(g['peak_whole_gpu_mib']['max'])} | {cell(g['done_reasons'])} |\n")
    parts.append("\nAPI가 종료 표식을 반환한 completed 상태와, 종료 사유 stop 및 최종 content를 확인한 답변 완성은 다른 값이다. length로 종료되면 추론 중이든 JSON 작성 중이든 응답 미완료로 분류한다. 추론 필드에만 있는 설명은 최종 답변의 충족 점수로 인정하지 않는다. 이 비교는 동일한 요청 설정의 활용 가능성을 보여주며, 더 큰 생성 길이에서의 최대 추론 품질까지 측정한 결과는 아니다.\n")
    parts.append("\n<details>\n<summary>모드별 전체 집계와 실제 요청 일치 점검</summary>\n"+block(summary)+"\n</details>\n")
    parts.append("\n### 추론 조건의 입력 및 실행 식별값\n"+block({"experiment_id":reasoning_id,"config_hash":experiment["config_hash"],"dataset_hash":experiment["dataset_hash"],"source_hashes":experiment["config"]["source_hashes"],"models":experiment["config"]["models"],"aggregation_source_hashes":{relative:hashlib.sha256((root/relative).read_bytes()).hexdigest() for relative in ("modules/evaluation_report.py","modules/evaluation_review.py")}}))
    parts.append("\n### 추론 조건의 문항별 원본과 평가 근거\n")
    cases={c['case_id']:c for c in experiment['dataset']['cases']}
    for a in attempts:
        r=a.get("record")
        if not r:
            parts.append("\n"+a["slot_id"]+": "+a["status"]+". "+a.get("note", "원본 미저장")+"\n")
            continue
        review=_poc_review(a)
        parts.append("\n<details>\n<summary>"+a['slot_id']+"</summary>\n"+block({"attempt_id":a['attempt_id'],"started_at":r.get('started_at'),"finished_at":r.get('finished_at'),"wall_seconds":r.get('wall_seconds'),"response_metadata":r.get('response_metadata'),"final_response_complete":r.get('final_response_complete'),"thinking_present":bool(r.get('thinking')),"content":r.get('content'),"model_vram_mib":_loaded_mib(r,'size_vram'),"peak_gpu_used_mib":r.get('peak_gpu_used_mib'),"errors":r.get('errors')}))
        if review:
            parts.append("\n| 항목 | 기대 설명 | 판정 | 응답 인용 | 판정 근거 |\n| --- | --- | --- | --- | --- |\n")
            for item,rubric in zip(review['required_items'],cases[a['case_id']]['rubric']):
                parts.append("| "+" | ".join(cell(v) for v in (item['id'],rubric['expected'],item['verdict'],item['quote'],item['note']))+" |\n")
            if review.get('findings'): parts.append(block(review['findings']))
        else: parts.append("\n최종 채점 미완료.\n")
        parts.append("\n</details>\n")
    parts.append("\n## 비추론 조건과 입력 언어 비교의 상세 기록\n\n아래 원본은 비추론 52회의 결과이며 추론 조건의 수치와 혼합하지 않는다. 당시 서버 로그와 채점 근거를 포함한다.\n\n<details>\n<summary>비추론 52회 상세 기록</summary>\n\n"+begin+"\n"+original+"\n"+end+"\n\n</details>\n")
    destination.write_text("\n".join(parts),encoding="utf-8")
    return {"report_path": str(destination), "quality_final": all(g['quality']['final'] for g in summary['groups'].values()), "request_match":summary['all_actual_requests_match']}



def write_budget_report(baseline_id, reasoning_ids, output_path, db_path="data/evaluation.db"):
    """
    같은 10문항의 비추론, 추론 및 생성 길이별 기록을 기존 보고서에 통합

    입력: 기준 비추론 ID, 생성 길이별 추론 ID 목록, 기존 보고서와 SQLite 경로
    비교: 모든 조건의 영문 첫 반복만 사용, 모델당 10응답 및 30점
    보존: 기존 상세 기록, 제외한 반복과 중단 내역, 실제 실행 날짜와 원본
    수록: 요청 설정, 추론 및 최종 답변 원문, 시간과 자원, 점수와 실제 인용
    한계: 미완료는 사실 오답과 구분, 미측정은 추정 금지, 20회 통과 여부 미확정
    """
    root = Path(__file__).resolve().parent.parent
    destination = (root/output_path).resolve()
    if destination.parent != (root/"reports").resolve() or destination.suffix != ".md":
        raise ValueError("reports 폴더의 기존 Markdown 경로 필요")
    original = destination.read_text(encoding="utf-8")
    begin, end = "<!-- NON_REASONING_RECORD_BEGIN -->", "<!-- NON_REASONING_RECORD_END -->"
    if begin in original:
        original = original.split(begin,1)[1].split(end,1)[0].strip()
    ids = [baseline_id] + list(reasoning_ids)
    experiments = {eid: load_experiment(eid,db_path) for eid in ids}
    rows = {eid: load_attempts(eid,db_path) for eid in ids}
    summaries = {eid: summarize_poc(eid,db_path,first_repeat_only=True) for eid in ids}
    baseline_rows = {a['slot_id']:a for a in rows[baseline_id]}
    def cell(value):
        return str(value).replace("|","\\|").replace("\n","<br>")
    def number(value):
        return "미측정" if value is None else f"{value:.3f}"
    def raw_block(raw, language="text"):
        fence = "`" * max(3, 1+max((len(x) for x in re.findall(r"`+",raw)), default=0))
        return "\n"+fence+language+"\n"+raw+"\n"+fence+"\n"
    def block(value):
        return raw_block(json.dumps(value,ensure_ascii=False,indent=2),"json")
    parts = ["# KAN-202 비추론, 추론 및 생성 길이별 비교 보고서\n",
        "## 상황\n\nQwen/Qwen3.5-9B와 google/gemma-4-12B-it을 노트북에서 실행해 같은 시장 및 가상 계좌 자료를 읽도록 했다. 비추론에서는 JSON이 반환됐지만 설명 오류가 있었고, 추론 2,048 조건의 첫 반복은 두 모델 모두 답변을 완성하지 못했다. 최종 답변을 완성하는 데 필요한 생성량과 시간, 길이를 늘렸을 때의 설명 정확성을 함께 확인할 필요가 있었다.\n",
        "## 과제\n\n같은 영문 Q01~Q10을 비추론 2,048, 추론 2,048, 추론 4,096, 추론 6,144 조건에서 비교한다. 첫 번째 반복만 맞춰 모델과 조건별 10응답, 필수 설명 30점으로 비교한다. 기존 20응답, 60점의 통과 기준을 10응답에 그대로 적용하지 않는다. 필수 항목은 완전하게 설명한 경우 1점, 부분 누락과 오류는 0점이다. 최종 답변이 없는 호출도 전체 시도 기준 0점이지만 사실을 틀린 답변과 구분한다. 사용자 중단은 모델 실패 점수에서 제외한다.\n",
        "## 행동\n\n모델 파일과 설치 식별값, 문항과 지시문, JSON 형식, 문맥 창 8,192, temperature=0, seed=42, 세부 생성 옵션과 호출 제한 300초를 유지했다. 추론 여부와 num_predict 요청값 외의 변경은 실제 요청 대조로 검사한다. 생성 길이가 6,144이면 기존 최대 입력 1,669를 더한 7,813이 문맥 창 8,192 안에 들어간다. 실제 생성량이 요청값과 반드시 같다고 가정하지 않는다.\n\n기존 비추론과 추론 2,048의 첫 반복을 재호출하지 않고 사용한다. 2,048 조건에서 저장된 두 번째 반복 4회와 사용자 중단 1회, 미실행 15회는 원본으로 보존하되 대응 비교에 합산하지 않는다. 시간 초과로 서버가 마지막 토큰 통계를 반환하지 않으면 토큰 수와 생성 속도를 추정하지 않고 미측정으로 남긴다.\n",
        "실행 코드는 modules/evaluation_poc.py의 prepare_mode(), run(), modules/evaluation_local.py의 call_ollama()이다. 입력과 코드, 환경을 먼저 저장하고 호출 전 시도를 등록하며, 종료 후 스트림 원문과 측정값을 SQLite에 보존한다. API 종료, 최종 답변 유무, 정상 종료와 JSON 형식은 각각 다른 항목으로 기록한다. 강제 중단 때 저장되지 않은 메모리 내 부분 응답은 사후에 재구성하지 않는다.\n",
        "최종 답변은 modules/evaluation_review.py의 record_required_review(), 추론 원문은 record_thinking_review()로 각각 근거를 저장한다. 추론에서의 반복, 숫자 비교나 출처 오류가 최종 답변에서 정정됐는지 구분하며, 중간 오류를 최종 답변 점수에 중복 반영하지 않는다. 검토 주체는 Codex이며 사람의 독립 검토로 표시하지 않는다.\n",
        "전체 시간에는 로딩과 입력 처리 및 생성이 포함되고 호출 전 조회, 호출 후 해제와 DB 저장은 제외된다. GPU 전체 메모리는 약 1초 간격 관측의 최댓값이며 다른 앱도 포함한다. 모델 적재량은 응답 뒤 별도로 조회한다. 날짜, 발열과 파일 캐시, 다른 프로그램까지 완전히 통제하지 않았으므로 작은 시간 차이를 모델 고유 성능으로 단정하지 않는다. 서버 파일 로그를 보존하지 못한 추론 조건에서 내부 입력 잘림 0을 직접 확인했다고 주장하지 않는다.\n",
        "```powershell\n.\\.venv\\Scripts\\python.exe -m modules.evaluation_poc --prepare-mode-from "+baseline_id+" --mode-num-predict 4096 --mode-repeat-count 1 --db "+db_path+"\n# 등록된 ID로 실행, 6144도 동일하게 등록 후 실행\n.\\.venv\\Scripts\\python.exe -m modules.evaluation_report "+baseline_id+" --budget-ids "+" ".join(reasoning_ids)+" --db "+db_path+" --markdown "+output_path+"\n```\n",
        "## 결과\n\n미실행과 미검토가 남은 조건은 미확정으로 표시한다. 최종 답변이 하나도 없으면 완료 답변만의 사실 정확도는 계산할 수 없다. 전체 시도 점수 0점과 정확도 측정 불가를 구분한다.\n\n| 조건 | 모델 | 저장 완료 / 계획 | API 정상 종료 | 정상 JSON | 필수 설명 | 회고 | 종료 사유 |\n| --- | --- | --- | --- | --- | --- | --- | --- |\n"]
    labels={}
    comparisons=[]
    for eid in ids:
        exp=experiments[eid]
        for name,g in summaries[eid]['groups'].items():
            model=name.rsplit('/',1)[0]; conf=exp['config']['models'][model]
            label=('추론' if conf['thinking'] else '비추론')+' '+str(conf['num_predict'])
            labels[(eid,model)]=label
            q=g['quality'];rf=g['reflection']
            parts.append(f"| {label} | {model} | {g['finished']-g.get('operator_interrupted',0)}/{g['planned']} | {g['api_success']['numerator']} | {g['completed_valid_json']} | {q['points']}/{q['denominator']} "+('확정' if q['final'] else '미확정')+f" | {rf['numerator']}/{rf['denominator']} | {cell(g['done_reasons'])} |\n")
        if eid != baseline_id:
            for a in rows[eid]:
                if a['repeat_number']!=1 or not a.get('record'):continue
                expected=copy.deepcopy(baseline_rows[a['slot_id']]['request'])
                expected['think']=True
                expected['options']['num_predict']=exp['config']['models'][a['model']]['num_predict']
                comparisons.append({'experiment_id':eid,'slot_id':a['slot_id'],
                    'only_think_and_num_predict_changed':expected==a['request'],
                    'registered_request_matches_sent_payload':a['request']==a['record'].get('request')})
    parts.append("\n### 시간, 토큰과 GPU 메모리\n\n| 조건 / 모델 | 전체 시간 평균 / 중앙값, n | 입력 토큰 범위, n | 실제 생성 토큰 범위, n | 로딩 평균 초, n | 생성 속도 평균 토큰/초, n | 모델 적재 최대 / GPU 전체 최대 MiB, n |\n| --- | --- | --- | --- | --- | --- | --- |\n")
    for eid in ids:
        for name,g in summaries[eid]['groups'].items():
            model=name.rsplit('/',1)[0]; label=labels[(eid,model)];t=g['wall_seconds'];i=g['input_tokens'];o=g['output_tokens'];l=g['server_load_seconds'];v=g['server_generation_tokens_per_second'];m=g['model_vram_mib_after_response'];gpu=g['peak_whole_gpu_mib']
            parts.append(f"| {label} / {model} | {number(t['mean'])} / {number(t['median'])}, n={t['n']} | {i['min']}–{i['max']}, n={i['n']} | {o['min']}–{o['max']}, n={o['n']} | {number(l['mean'])}, n={l['n']} | {number(v['mean'])}, n={v['n']} | {number(m['max'])}, n={m['n']} / {number(gpu['max'])}, n={gpu['n']} |\n")
    parts.append("\n한도를 늘렸다는 사실만으로 실제 추론량이나 정확성이 증가했다고 결론 내리지 않는다. 일찍 정상 종료한 경우와 길이 제한, 300초 시간 제한, 입력 또는 실행 오류를 구분한다. 최종 답변의 오류와 추론 중 수정된 오류도 구분한다. 이번 조건에서 끝내지 못한 결과를 무제한 생성 환경의 최대 성능으로 일반화하지 않는다.\n")
    parts.append("\n<details>\n<summary>집계 원문과 실제 요청 대조</summary>\n"+block({'summaries':summaries,'request_comparison':comparisons,'aggregation_source_hashes':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in ('modules/evaluation_report.py','modules/evaluation_review.py')}})+"\n</details>\n")
    parts.append("\n## 조건별 실제 설정과 문항 원문, 추론 및 답변 검토\n\n각 조건의 입력 원문은 기존 [설계 보고서](2026-09-15_KAN-201_poc-design.md)에 있다. 호출별 전체 전송 본문은 SQLite에 보존했고, 아래에는 요청 식별값과 실제 옵션을 수록한다.\n")
    for eid in ids:
        exp=experiments[eid]
        parts.append("\n### "+eid+"\n\n<details>\n<summary>모델 식별값과 고정 설정 및 실행 코드</summary>\n"+block({'config_hash':exp['config_hash'],'dataset_hash':exp['dataset_hash'],'models':exp['config']['models'],'source_hashes':exp['config']['source_hashes']})+"\n</details>\n")
        cases={c['case_id']:c for c in exp['dataset']['cases'] if c['language']=='en'}
        for a in rows[eid]:
            if a['repeat_number']!=1 or (a.get('record') or {}).get('language')!='en':continue
            r=a['record'];review=_poc_review(a)
            parts.append("\n<details>\n<summary>"+a['slot_id']+"</summary>\n")
            parts.append(block({'attempt_id':a['attempt_id'],'started_at':r.get('started_at'),'finished_at':r.get('finished_at'),'request_hash':a['request_hash'],'request_without_messages':{k:v for k,v in a['request'].items() if k!='messages'},'messages_hash':r.get('messages_hash'),'status':a['status'],'response_metadata':r.get('response_metadata'),'final_response_complete':r.get('final_response_complete'),'schema_valid':((a.get('evaluation') or {}).get('assessment') or {}).get('schema_valid'),'wall_seconds':r.get('wall_seconds'),'first_content_seconds':r.get('first_content_seconds'),'model_vram_mib':_loaded_mib(r,'size_vram'),'peak_gpu_used_mib':r.get('peak_gpu_used_mib'),'errors':r.get('errors')}))
            parts.append("\n**추론 원문**\n"+(raw_block(r['thinking']) if r.get('thinking') else "\n추론 필드 원문 없음.\n"))
            parts.append("\n**최종 답변 원문**\n"+(raw_block(r['content'],'json') if r.get('content') else "\n빈 문자열, 최종 답변 없음.\n"))
            if review:
                parts.append("\n| 항목 | 기대 설명과 원본 경로 | 판정 | 응답 인용 | 판정 근거 |\n| --- | --- | --- | --- | --- |\n")
                for item,rubric in zip(review['required_items'],cases[a['case_id']].get('rubric',[])):
                    parts.append('| '+' | '.join(cell(v) for v in (item['id'],rubric['expected']+' / '+', '.join(rubric['source_paths']),item['verdict'],item['quote'],item['note']))+' |\n')
                if review.get('findings'):parts.append("\n최종 답변의 그 밖의 오류:"+block(review['findings']))
            else:parts.append("\n최종 답변 채점 미완료.\n")
            thinking_reviews=[e['review'] for e in (a.get('evaluation') or {}).get('reviews',[]) if e['review'].get('method')=='source_grounded_thinking']
            if thinking_reviews:parts.append("\n**추론 원문 검토와 최종 정정 여부**\n"+block(thinking_reviews[-1]))
            else:parts.append("\n추론 원문의 반복 및 사실 오류 검토: "+("미검토" if r.get('thinking') else "추론 필드 없음")+".\n")
            parts.append("\n</details>\n")
    parts.append("\n## 비교표에서 제외한 반복과 중단 이력\n\n비추론의 두 번째 반복과 한국어 결과는 아래 기존 상세 기록에 보존했다. 추론 2,048의 두 번째 반복 Q01, Q02는 두 모델 모두 길이 제한과 빈 최종 답변을 기록했다. 다음 Gemma Q03은 사용자 요청으로 중단했으며 메모리 내 부분 스트림이 저장되지 않았다. 비교 조건마다 첫 반복을 사용하므로 이 기록들을 합산하거나 삭제하지 않는다.\n")
    for eid in reasoning_ids:
        excluded=[{k:a[k] for k in ('attempt_id','slot_id','status','note')} for a in rows[eid] if a['repeat_number']!=1]
        if excluded:parts.append(block({'experiment_id':eid,'excluded_from_matched_table':excluded}))
    parts.append("\n<details>\n<summary>비추론 52회의 기존 상세 기록</summary>\n\n"+begin+"\n"+original+"\n"+end+"\n\n</details>\n")
    # 표의 행 사이 빈 줄 제거, 기존 상세 기록 원문의 변경 방지
    body = '\n'.join(parts[:-1])
    body = re.sub(r'(?m)(^\|[^\n]*\|)\n\n(?=\|)', r'\1\n', body)
    destination.write_text(body + '\n' + parts[-1], encoding='utf-8')
    return {'report_path':str(destination),'selected_calls':sum(g['finished'] for v in summaries.values() for g in v['groups'].values()),'all_final_answers_reviewed':all(g['quality']['final'] for v in summaries.values() for g in v['groups'].values()),'actual_requests_checked':len(comparisons),'request_changes_match_plan':all(c['only_think_and_num_predict_changed'] and c['registered_request_matches_sent_payload'] for c in comparisons)}


def write_completion_report(experiment_id, output_path, db_path="data/evaluation.db"):
    """
    대표 문항의 응답 완료 확인 결과를 기존 로컬 보고서 앞부분에 통합

    구성: 상황, 과제, 행동, 결과와 모델별 실제 설정 및 원본
    보존: 중단한 전체 문항 시험과 기존 비추론 기록의 별도 접기 영역
    판정: 정상 종료와 JSON 형식 및 필수 사실 점수의 분리
    제한: 한 문항 한 번의 관측, 최소 토큰 한도와 전체 문항 성능의 추정 없음
    """
    root = Path(__file__).resolve().parent.parent
    destination = (root / output_path).resolve()
    if destination.parent != (root / "reports").resolve() or destination.suffix != ".md":
        raise ValueError("reports 폴더의 Markdown 경로 필요")
    experiment = load_experiment(experiment_id, db_path)
    config = experiment["config"]
    if config["evaluation_kind"] != "poc_completion_check":
        raise ValueError("대표 문항 응답 완료 확인 기록 필요")
    attempts = load_attempts(experiment_id, db_path)
    original = destination.read_text(encoding="utf-8")
    begin, end = "<!-- PRIOR_LOCAL_RECORD_BEGIN -->", "<!-- PRIOR_LOCAL_RECORD_END -->"
    if begin in original:
        original = original.split(begin, 1)[1].split(end, 1)[0].strip()
    def block(value, language="json"):
        raw = json.dumps(value, ensure_ascii=False, indent=2) if language == "json" else value
        fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", raw)), default=0))
        return "\n" + fence + language + "\n" + raw + "\n" + fence + "\n"
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", "<br>")
    def seconds(value):
        return "미측정" if value is None else f"{value:.3f}"
    lines = [
        "# KAN-202 로컬 모델의 응답 완료와 사실 설명 확인 보고서",
        "",
        "## 상황",
        "",
        "기존 비추론 조건에서는 두 모델이 JSON을 반환했지만 수치와 거래 이력 설명에 오류가 남았다. temperature 0의 추론 조건에서는 생성 한도를 늘려도 최종 답변이 완성되지 않았다. 전체 문항을 계속 반복하기 전에 공식 권장 생성 설정에서 추론이 정상 종료되는지 확인할 필요가 있었다.",
        "",
        "## 과제",
        "",
        "기존 영문 Q08 하나를 Qwen/Qwen3.5-9B와 google/gemma-4-12B-it에 각각 한 번 적용한다. Q08은 실제 체결 수량과 가격, 수수료 및 미실현 손익을 설명하는 사례이므로 계산과 회고를 함께 확인할 수 있다. 목적은 어떤 설정에서 최종 답변이 나오는지 관측하는 것이다. 전체 10문항의 정확성 평가나 최소 필요 생성량을 찾는 시험은 아니다.",
        "",
        "## 행동",
        "",
        "공식 Model Card와 설치된 Ollama 모델의 parameters를 확인한 뒤 모델별 권장 샘플링을 적용했다. 영문 입력과 지시문, JSON Schema, 양자화 모델과 digest는 기존 기록에서 그대로 가져왔다. 호출 제한은 모델별 1,800초, seed는 42이다. 한 모델씩 호출하고 해제하며, 실제 거래와 외부 유료 API 호출은 없다.",
        "",
        "| 적용값 | Qwen3.5-9B | Gemma 4 12B |",
        "| --- | --- | --- |",
        "| temperature / top_p / top_k | 1.0 / 0.95 / 20 | 1.0 / 0.95 / 64 |",
        "| presence_penalty / repeat_penalty | 1.5 / 1.0 | 0.0 / 1.0 |",
        "| min_p / frequency_penalty | 0.0 / 0.0 | 0.0 / 0.0 |",
        "| 문맥 창 요청 | 131,072 | 32,768 |",
        "| 생성 한도 요청 | 32,768 | 16,384 |",
        "",
        "Qwen의 샘플링, 일반 질의 생성 한도 32,768 및 최소 128K 문맥 권고는 [공식 Model Card](https://huggingface.co/Qwen/Qwen3.5-9B)의 Best Practices와 Serving 절에 근거한다. repetition_penalty는 Ollama의 repeat_penalty로 지정했다. Gemma의 공식 권장값은 [Model Card](https://huggingface.co/google/gemma-4-12B-it)의 temperature, top_p, top_k이다. Gemma의 문맥과 생성 한도 및 그 밖의 명시되지 않은 옵션은 이번 노트북 점검을 위한 실행 설정이며 전부 공식 권장값인 것은 아니다.",
        "",
        "등록 코드는 modules/evaluation_poc.py의 prepare_completion_check(), 실행은 run(), 요청과 측정은 modules/evaluation_local.py의 call_ollama()이다. 실제 코드 원문, 해시, 환경과 설정 출처를 호출 전에 SQLite에 저장했다. 모델 호출 전에 시도를 등록하고, 수신 스트림과 최종 메타데이터, 자원 표본을 보존한 뒤 저장 결과를 다시 읽었다.",
        "",
        "실행 준비 과정의 최초 계획은 DB에서 지원하지 않는 단계명을 사용해 모델 호출 전에 중단됐다. 기존 preflight 단계명을 사용하도록 수정하고 가상 응답으로 등록 및 저장 경로를 검사했다. 사용하지 않은 계획 poc-completion-check-20260916T045544Z와 poc-completion-check-20260916T045713Z에는 실제 호출이 없으며 아래 측정 횟수에 포함하지 않는다.",
        "",
        "```powershell",
        ".\\.venv\\Scripts\\python.exe -m modules.evaluation_poc --prepare-completion-from poc-main-20260915T113044Z --db " + db_path,
        ".\\.venv\\Scripts\\python.exe -m modules.evaluation_poc " + experiment_id + " --db " + db_path,
        ".\\.venv\\Scripts\\python.exe -m modules.evaluation_report " + experiment_id + " --completion-check --db " + db_path + " --markdown " + output_path,
        "```",
        "",
        "## 결과",
        "",
        "정상 종료는 API 완료, done_reason=stop, 최종 답변 존재와 지정 JSON 준수로 확인한다. 세 사실의 점수는 답변 완료 여부와 별개이다. 점수 확인 주체는 Codex이며 사람의 독립 검토로 표시하지 않는다.",
        "",
        "| 모델 | 상태 / 종료 사유 | 최종 답변 | JSON | 입력 / 실제 생성 토큰 | 전체 시간 | 사실 설명 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for model in config["models"]:
        a = next((a for a in attempts if a["model"] == model), None)
        r = (a or {}).get("record") or {}
        meta = r.get("response_metadata") or {}
        review = _poc_review(a) if a else None
        score = str(sum(i["verdict"] == "pass" for i in review["required_items"])) + "/3" if review else "미검토"
        assessment = (((a or {}).get("evaluation") or {}).get("assessment") or {})
        lines.append("| " + " | ".join(cell(v) for v in (
            model, ((a or {}).get("status", "미실행") + " / " + str(meta.get("done_reason", "미측정"))),
            "있음" if r.get("content") else "없음 또는 실행 중",
            assessment.get("schema_valid", "미측정"),
            str(meta.get("prompt_eval_count", "미측정")) + " / " + str(meta.get("eval_count", "미측정")),
            seconds(r.get("wall_seconds")), score)) + " |")
    # 한 응답의 로딩과 생성 및 모델 적재량을 전체 시간과 분리한 실측 표시
    lines.extend(["", "| 모델 | 로딩 초 | 생성 속도 토큰/초 | 최종 답변 첫 수신 초 | 모델 전체 / GPU 적재 MiB | GPU 전체 최대 MiB |",
                  "| --- | --- | --- | --- | --- | --- |"])
    completed_checks = 0
    for a in attempts:
        r = a.get("record")
        if not r:
            continue
        meta = r.get("response_metadata") or {}
        duration = meta.get("eval_duration")
        speed = meta.get("eval_count") / (duration / 1e9) if duration and meta.get("eval_count") is not None else None
        loaded = next((m for m in (r.get("loaded_models") or {}).get("models", [])
                       if m.get("name") == a["model"]), {})
        total_mib = loaded.get("size") / 1048576 if loaded.get("size") is not None else None
        gpu_mib = loaded.get("size_vram") / 1048576 if loaded.get("size_vram") is not None else None
        load_s = meta.get("load_duration") / 1e9 if meta.get("load_duration") is not None else None
        valid = ((a.get("evaluation") or {}).get("assessment") or {}).get("schema_valid")
        completed_checks += (a["status"] == "completed" and meta.get("done_reason") == "stop"
                             and bool(r.get("content")) and valid is True)
        lines.append("| " + " | ".join(cell(v) for v in (
            a["model"], seconds(load_s), seconds(speed), seconds(r.get("first_content_seconds")),
            seconds(total_mib) + " / " + seconds(gpu_mib), seconds(r.get("peak_gpu_used_mib")))) + " |")
    lines.extend(["", f"현재 저장 원본에서 정상 종료와 지정 JSON을 함께 확인한 호출은 {completed_checks}/{len(config['schedule'])}회이다."])
    if len(attempts) == len(config["schedule"]) and all(a.get("record") for a in attempts):
        lines.extend(["", "대표 문항 실행 종료. 정상 완료한 모델은 위 설정에서 답변이 나오는 사례를 확보했으므로 전체 문항 반복이나 생성 한도 탐색을 이어가지 않는다. 실패한 모델은 해당 종료 사유와 설정의 한계로 기록한다."])
    lines.extend(["", "### 답변 내용과 실행 결과의 해석", ""])
    for a in attempts:
        review = _poc_review(a)
        if not review:
            continue
        points = sum(i["verdict"] == "pass" for i in review["required_items"])
        lines.append(f"- {a['model']}: 필수 사실 {points}/3항목 충족.")
        for item in review["required_items"]:
            if item["verdict"] != "pass":
                lines.append("  - " + item["id"] + ": " + item["note"])
        for finding in review.get("findings", []):
            lines.append("  - 별도 확인 사항: " + finding["note"])
    lines.extend(["",
        "이번 관측으로 공식 권장 샘플링과 명시한 실행 한도에서 대표 Q08의 최종 답변이 나오는지 확인할 수 있다. 이전 temperature 0, 짧은 생성 한도 조건의 미완료만으로 두 모델이 이 문항에 답할 능력이 없다고 결론 내릴 수는 없다. 응답 완료와 설명의 정확성은 계속 구분해야 한다.",
        "",
        "실행 및 저장 코드의 자동 검사 64개 통과. 가상 응답 검사는 실행 경로의 확인이며 실제 모델 품질 점수와 구분한다."])
    lines.extend(["",
        "이 표의 표본은 모델별 한 문항 한 번이다. 정상 답변이 반환돼도 다른 문항이나 다른 seed에서 안정적으로 종료된다고 확정할 수 없다. 관측된 생성량은 해당 호출의 사용량이며 필요한 최소 한도를 뜻하지 않는다.",
        "",
        "샘플링, 문맥 창과 생성 한도 및 시간 제한을 함께 바꿨으므로 이전 결과와 차이가 나도 원인을 하나의 설정으로 단정하지 않는다. GPU와 CPU에 나누어 적재된 경우도 아래 실제 적재 정보로 확인하며, 서로 다른 실행 한도의 시간으로 모델 순위를 정하지 않는다.",
        "",
        "### 전체 문항 시험의 중단과 원본 보존",
        "",
        "사용자 요청에 따라 추론 2,048은 24회 저장, 사용자 중단 1회, 미실행 15회에서 종료했다. 추론 4,096은 19회 저장, 사용자 중단 1회에서 종료했다. 추론 6,144는 등록만 했으며 실제 호출 없이 전체 문항 실행을 취소했다. 사용자 중단을 모델 자체의 실패로 채점하지 않으며, 이미 저장한 원본과 실제 실행 날짜는 유지한다.",
        "",
        "## 대표 문항의 입력, 설정과 원본",
        "",
        "**Q08 모델 입력 원문**", block(experiment["dataset"]["cases"][0]["input"]),
        "",
        "**실행 식별값과 설정 출처**",
        block({"experiment_id": experiment_id, "config_hash": experiment["config_hash"],
               "dataset_hash": experiment["dataset_hash"], "settings_provenance": config["settings_provenance"],
               "models": config["models"], "environment": config["environment"],
               "source_hashes": config["source_hashes"]}),
    ])
    for a in attempts:
        r = a.get("record")
        if not r:
            continue
        lines.extend(["", "### " + a["model"], "",
                      "**실제 전송 요청**", block(a["request"]), "**호출 결과와 측정값**",
                      block({k:r.get(k) for k in (
                          "attempt_id", "started_at", "finished_at", "status", "response_metadata",
                          "first_token_seconds", "first_content_seconds", "wall_seconds",
                          "loaded_models", "peak_gpu_used_mib", "errors", "final_response_complete")}),
                      "", "<details>", "<summary>추론 원문</summary>",
                      block(r.get("thinking", ""), "text"), "</details>", "",
                      "**최종 답변 원문**", block(r.get("content", ""), "text"),
                      "**최종 답변의 세 사실 대조**", block(_poc_review(a) or {"status": "미검토"})])
    lines.extend(["", "<details>", "<summary>중단 시점의 전체 문항 시험과 기존 비추론 상세 기록</summary>",
                  "", "아래는 범위 변경 이전의 기록 보존본이다. 실행 예정 표현은 현재 진행 계획을 뜻하지 않는다.",
                  "", begin, original, end, "", "</details>", ""])
    destination.write_text("\n".join(lines), encoding="utf-8")
    return {"report_path": str(destination), "stored_calls": sum(bool(a.get("record")) for a in attempts),
            "planned_calls": len(config["schedule"])}



def write_best_practices_report(nonthinking_id, thinking_id, destination, db_path="data/evaluation.db", warmup_ids=()):
    """
    공식 권장 설정의 두 모드 실행 및 품질 기록을 기존 보고서에 통합

    입력: 비추론과 추론 계획 ID, 기존 Markdown 경로와 SQLite 경로
    보존: 기존 실험 보고서의 전체 원문, 새 실행의 요청과 추론 및 최종 답변
    집계: 실제 저장 응답만 계산, 미검토 항목을 0점으로 바꾸지 않는 처리
    구분: 요청 상한과 실제 생성량, 300초 운영 요구와 1,800초 측정 제한
    재실행: 표식 안의 이전 보고서는 유지하고 현재 집계만 재작성
    """
    plans = [(mode, load_experiment(eid, db_path), load_attempts(eid, db_path))
             for mode, eid in (("비추론", nonthinking_id), ("추론", thinking_id))]
    destination = Path(destination)
    begin, end = "<!-- BEFORE_BEST_PRACTICES_BEGIN -->", "<!-- BEFORE_BEST_PRACTICES_END -->"
    prior = destination.read_text(encoding="utf-8") if destination.exists() else ""
    if begin in prior and end in prior:
        prior = prior.split(begin, 1)[1].split(end, 1)[0].strip()
    def block(value, language="json"):
        # 모델 원문의 코드 구분 기호와 충돌하지 않는 긴 Markdown 구분선 사용
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        fence = "`" * max(4, max((len(x) for x in re.findall(r"`+", raw)), default=0) + 1)
        return fence + language + "\n" + raw + "\n" + fence
    def value(number):
        return "미측정" if number is None else f"{number:,.3f}"
    lines = ["# 로컬 모델의 비추론 및 추론 비교 보고서", "",
        "## 상황", "",
        "개인 노트북에서 비트코인 시장과 가상 계좌 자료를 설명하는 Qwen/Qwen3.5-9B와 google/gemma-4-12B-it의 실행 가능성과 사실 정확성을 확인하는 PoC이다. 기존 비추론은 JSON을 반환해도 설명 오류가 남았고, 짧은 생성 한도를 적용한 추론은 최종 답변을 완성하지 못했다. 공식 권장 설정을 적용한 대표 문항에서는 두 모델 모두 답변을 완성하여 같은 10문항 전체를 비교한다.", "",
        "이번 기록은 모델의 성능을 새로운 문제로 평가하는 작업이 아니라 기존 입력과 채점 기준을 유지한 본 비교이다. 실제 실행 날짜와 이전 기록은 하단에 보존한다.", "",
        "## 과제", "",
        "- 영문 Q01~Q10, 두 모델, 비추론 및 추론 각 1회로 총 40회 실행",
        "- 모드별 모델별 W00 한 번, 총 워밍업 4회 별도 기록 및 본 품질 집계 제외",
        "- 각 응답의 지정 JSON과 거래 제한 확인, 필수 사실 세 항목의 원본 대조",
        "- 모델 및 모드별 10응답 30항목의 충족률 비교, 이전 20응답 60점의 도입 통과 기준 직접 적용 제외",
        "- 모든 본 시험 종료 후 정확성과 회고, 응답 시간 및 자원 사용량 비교", "",
        "## 행동", "",
        "### 공식 설정과 선택 이유", "",
        "Qwen은 시장 및 계좌 자료 설명이 주목적인 일반 과제로 사전 분류했다. 일반 과제의 모드별 권장 샘플링을 사용하며, 결과에 따라 수학 및 논리 과제 설정으로 사후 변경하지 않는다. Gemma는 공통 샘플링 권고를 적용한다.", "",
        "| 모델 | 모드 | temperature | top_p | top_k | presence_penalty | 문맥 창 | 생성 한도 | 호출 제한 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    # 같은 평가 문항의 의미를 별도 파일 없이 파악할 수 있도록 목차 표 삽입
    case_table = ["### 문항별 확인 대상", "",
                  "| 문항 | 확인할 사실 세 가지 |", "| --- | --- |"]
    for c in plans[0][1]["dataset"]["cases"]:
        case_table.append("| " + c["case_id"] + " | " + "<br>".join(
            f"{i}. {r['expected']}" for i, r in enumerate(c.get("rubric", []), 1)) + " |")
    insertion = lines.index("## 행동")
    lines[insertion:insertion] = case_table + [""]
    for mode, plan, attempts in plans:
        for name, model in plan["config"]["models"].items():
            o = model["options"]
            lines.append(f"| {name} | {mode} | {model['temperature']} | {o['top_p']} | {o['top_k']} | {o['presence_penalty']} | {model['num_ctx']:,} | {model['num_predict']:,} | {model['timeout']}초 |")
    lines += ["",
        "공통 seed 42, min_p 0, repeat_penalty 1, frequency_penalty 0이다. Qwen의 생성 한도 32,768은 일반 질의 권고, 문맥 창 131,072는 Serving 절의 추론 능력 보존 권고이다. Gemma의 문맥 창 32,768, 생성 한도 16,384와 공식 문서에 없는 옵션은 앞서 실행을 확인한 운영자 설정이며 공식 권고와 구분한다.",
        "",
        "같은 모델의 두 모드에는 동일 문맥과 생성 한도를 적용한다. 다만 Qwen은 temperature와 top_p도 바뀌므로 추론 여부만의 인과 효과를 뜻하지 않는다. 모델 간 문맥과 생성 한도가 달라 동일 자원 예산의 속도 순위로 해석할 수 없다.",
        "",
        "- [Qwen 공식 Best Practices 및 Serving](https://huggingface.co/Qwen/Qwen3.5-9B#best-practices)",
        "- [Gemma 공식 Best Practices](https://ai.google.dev/gemma/docs/core/model_card_4#best-practices)", "",
        "### 실행 코드와 기록 방식", "",
        "- 계획: modules/evaluation_poc.py의 prepare_best_practices()",
        "- 실행: 같은 모듈의 run(), 호출과 측정: modules/evaluation_local.py의 call_ollama()",
        "- 검토: modules/evaluation_review.py의 record_required_review(), 최종 응답의 인용과 입력 근거 대조",
        "- 보고서: modules/evaluation_report.py의 write_best_practices_report()",
        "- 원본 저장: data/poc-submission-20260915.sqlite의 계획, 요청, 응답과 검토 기록; 기존 내보내기 표의 2026-09-15 목록은 당시 스냅샷이며 이후 직접 저장한 시험은 아래 실행 ID로 조회",
        "- 문항: modules/poc_cases.json의 영문 10문항, 평가자용 정답을 모델 입력에서 제외",
        "- 모델 하나씩 적재 및 해제, 매 호출의 로딩 포함 시간과 서버 로딩 시간 별도 기록",
        "- 입력 자동 잘림과 문맥 이동 비활성화, 이전 문항의 대화 전달 없음, 자동 재시도 없음",
        "- 지정 JSON Schema를 이용한 형식 제한 생성, 자유 생성에서 모델이 스스로 형식을 지키는 능력과 구분",
        "- 응답 시간은 생성 프로세스 시작 직전부터 최종 응답 수신까지의 측정, 사전 정보 조회와 모델 해제 및 DB 저장 시간 제외",
        "- 비추론 후 추론, 각 문항에서 Qwen 후 Gemma의 고정 순서, 실행 순서와 열 상태의 영향에 대한 별도 통제 없음",
        "- 모델 및 모드별 단일 반복, 표본 확대 또는 반복 안정성 확인으로 해석하지 않는 기준",
        "- 입력 및 요청 보존과 호출 및 저장 경로의 자동 검사 27개 통과", "",
        "실행 예시는 아래와 같다. --prepare-best-practices-from은 기존 대표 문항 기록의 모델 식별값과 실행 한도를 가져와 새 계획을 저장하는 명령이며, 그 자체로 모델을 호출하지 않는다. --thinking이 없으면 비추론 계획이다. --mode-warmup을 지정하면 W00 별도 워밍업 계획이다.", "",
        block("uv run python -m modules.evaluation_poc --prepare-best-practices-from poc-completion-check-20260916T045745Z --db data/poc-submission-20260915.sqlite\nuv run python -m modules.evaluation_poc --prepare-best-practices-from poc-completion-check-20260916T045745Z --thinking --db data/poc-submission-20260915.sqlite\nuv run python -m modules.evaluation_poc <저장된_실험_ID> --db data/poc-submission-20260915.sqlite", "text"),
        "", "## 결과", "",
        "종료 여부와 설명의 정확성은 별도 지표이다. 아래 점수는 원본 대조 검토가 저장된 응답만 집계하며 미검토를 0점으로 대체하지 않는다. 300초는 기존 운영 요구, 1,800초는 이번 관측을 끝내는 제한으로 구분한다.", "",
        "| 모델 | 모드 | 저장 / 계획 | stop + JSON | 300초 이내 stop + JSON | 점수 / 검토 항목 | 검토 응답 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for mode, plan, attempts in plans:
        for name in plan["config"]["models"]:
            rows = [a for a in attempts if a["model"] == name and a.get("record")]
            good = [a for a in rows if a["record"].get("status") == "completed"
                    and (a["record"].get("response_metadata") or {}).get("done_reason") == "stop"
                    and (a.get("evaluation") or {}).get("assessment", {}).get("schema_valid")]
            fast = [a for a in good if a["record"].get("wall_seconds") is not None
                    and a["record"]["wall_seconds"] <= 300]
            reviews = [_poc_review(a) for a in rows if _poc_review(a)]
            points = sum(i["verdict"] == "pass" for r in reviews for i in r["required_items"])
            score = f"{points}/{3*len(reviews)}" if reviews else "미검토"
            lines.append(f"| {name} | {mode} | {len(rows)}/10 | {len(good)}/{len(rows)} | {len(fast)}/{len(rows)} | {score} | {len(reviews)}/10 |")
    lines += ["", "### 시간 및 생성량", "",
        "| 모델 | 모드 | 시간 측정 n | 전체 시간 중앙값 | 입력 토큰 범위 | 생성 토큰 범위 | GPU 전체 관측 최댓값 |",
        "| --- | --- | ---: | ---: | --- | --- | ---: |"]
    for mode, plan, attempts in plans:
        for name in plan["config"]["models"]:
            records = [a["record"] for a in attempts if a["model"] == name and a.get("record")]
            times = _distribution([r.get("wall_seconds") for r in records])
            incoming = _distribution([_tokens(r)[0] for r in records])
            outgoing = _distribution([_tokens(r)[1] for r in records])
            gpu = _distribution([r.get("peak_gpu_used_mib") for r in records])
            lines.append(f"| {name} | {mode} | {times['n']} | {value(times['median'])}초 | {incoming['min']}~{incoming['max']} (n={incoming['n']}) | {outgoing['min']}~{outgoing['max']} (n={outgoing['n']}) | {value(gpu['max'])}MiB (n={gpu['n']}) |")
    lines += ["", "시간 통계는 완료 및 실패를 포함해 실제 값이 기록된 호출을 대상으로 한다. 서버가 종료 통계를 반환하지 않은 토큰 값은 미측정으로 남긴다. GPU 전체 사용량은 주기적 관측의 최댓값으로, 모델 전용 사용량이나 하드웨어의 순간 절대 최댓값과 구분한다.", "",
        "| 모델 | 모드 | 로딩 시간 중앙값 | 생성 속도 중앙값 | 응답 후 모델 전체 / GPU 적재량 최댓값 |",
        "| --- | --- | ---: | ---: | ---: |"]
    for mode, plan, attempts in plans:
        for name in plan["config"]["models"]:
            records = [a["record"] for a in attempts if a["model"] == name and a.get("record")]
            loads = _distribution([_server_seconds(r, "load_duration") for r in records])
            speeds = []
            totals, vram = [], []
            for r in records:
                duration = _server_seconds(r, "eval_duration")
                count = _tokens(r)[1]
                if duration and count is not None:
                    speeds.append(count/duration)
                loaded = next((m for m in (r.get("loaded_models") or {}).get("models", [])
                               if m.get("name", m.get("model")) == name), None)
                if loaded:
                    if loaded.get("size") is not None:
                        totals.append(loaded["size"]/1048576)
                    if loaded.get("size_vram") is not None:
                        vram.append(loaded["size_vram"]/1048576)
            rate = _distribution(speeds)
            memory, gpu = _distribution(totals), _distribution(vram)
            lines.append(f"| {name} | {mode} | {value(loads['median'])}초 (n={loads['n']}) | {value(rate['median'])}토큰/초 (n={rate['n']}) | {value(memory['max'])} / {value(gpu['max'])}MiB (n={min(memory['n'], gpu['n'])}) |")
    lines += ["", "모델 전체 크기와 GPU 적재량에 차이가 있으면 CPU에도 분산 적재된 실행이다. 서버의 전체 생성 토큰은 추론과 최종 답변의 합산 수치이며, 제공되지 않은 구간별 토큰 수를 문자 수로 임의 환산하지 않는다."]
    total = sum(sum(bool(a.get("record")) for a in rows) for _, _, rows in plans)
    reviewed = sum(sum(bool(_poc_review(a)) for a in rows) for _, _, rows in plans)
    lines += ["", f"현재 본 시험 원본 저장 {total}/40회, 최종 답변의 세 항목 검토 {reviewed}/40회이다.",
        "전체 호출과 품질 검토가 완료되기 전에는 모델 선정 또는 정확성 개선을 확정하지 않는다." if total < 40 or reviewed < 40
        else "40회 원본과 필수 사실 검토를 확보했다. 위 결과는 지정한 설정과 문항에서의 관측이며 일반적인 거래 수익성이나 모델 전체의 정확도를 뜻하지 않는다.",
        "", "### 채점 해석", "",
        "- pass는 요구한 사실과 수치 및 맥락을 모두 정확히 설명한 경우의 1점, partial과 fail은 각각 일부 누락과 오류 또는 핵심 미충족의 0점",
        "- 인용문은 판정 위치를 찾기 위한 일부 구절이며 최종 답변 전체를 함께 대조하는 방식",
        "- 계산식을 특정 기호로 적을 의무는 없으나 요구한 수량, 가격 및 비용 관계를 실제로 연결한 검산 설명 필요",
        "- 올바른 계산 결과만 되풀이한 답변과 입력 수치로 검산한 답변의 구분",
        "- 추론 원문에 있는 정답을 최종 답변에 없는 설명의 점수로 대체하지 않는 기준",
        "- 명시적으로 잘못된 표현을 정정한 답변과 서로 모순되는 주장을 그대로 남긴 답변의 구분",
        "- 필수 세 항목 밖의 잘못된 매수 조건, 크기 비교 및 근거 없는 시장 단정은 별도 오류로 보존",
        "- 자동 거래 제한 검사는 반환된 결정과 비율에 대한 판정, 잘못된 설명의 부재를 보장하지 않는 한계",
        "", "### 문항별 측정과 판정", "",
        "| 모드 | 모델 | 문항 | 종료 | 입력 | 실제 생성 | 시간 | JSON | 사실 점수 |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |"]
    for mode, _, attempts in plans:
        for a in attempts:
            r = a.get("record")
            if not r:
                continue
            m = r.get("response_metadata") or {}
            review = _poc_review(a)
            score = str(sum(x["verdict"] == "pass" for x in review["required_items"])) + "/3" if review else "미검토"
            lines.append(f"| {mode} | {a['model']} | {a['case_id']} | {m.get('done_reason', r['status'])} | {m.get('prompt_eval_count')} | {m.get('eval_count')} | {value(r.get('wall_seconds'))}초 | {(a.get('evaluation') or {}).get('assessment', {}).get('schema_valid')} | {score} |")

    # 검토가 끝난 동일 문항끼리 비교, 회고는 전체 점수의 부분집합으로 별도 집계
    lines += ["", "### 같은 문항에서 달라진 결과", "",
              "| 모델 | 문항 | 비추론 점수 | 추론 점수 | 비추론 시간 | 추론 시간 |",
              "| --- | --- | ---: | ---: | ---: | ---: |"]
    for name in plans[0][1]["config"]["models"]:
        off = {a["case_id"]: a for a in plans[0][2] if a["model"] == name and a.get("record")}
        on = {a["case_id"]: a for a in plans[1][2] if a["model"] == name and a.get("record")}
        for case_id in sorted(set(off) & set(on)):
            pair = [off[case_id], on[case_id]]
            scores = [str(sum(i["verdict"] == "pass" for i in _poc_review(a)["required_items"])) + "/3"
                      if _poc_review(a) else "미검토" for a in pair]
            lines.append(f"| {name} | {case_id} | {scores[0]} | {scores[1]} | "
                         f"{value(pair[0]['record'].get('wall_seconds'))}초 | "
                         f"{value(pair[1]['record'].get('wall_seconds'))}초 |")
    lines += ["", "### 회고 항목", "",
              "회고는 실패한 호출, 실제 체결과 손익, 취소 주문, 과거 설명의 오류를 구분하는 Q07~Q10의 12개 항목이다. 아래 점수는 전체 30점에 이미 포함되며 별도로 더하지 않는다.",
              "", "| 모델 | 모드 | 회고 점수 / 검토 항목 | 검토 응답 |",
              "| --- | --- | ---: | ---: |"]
    for mode, plan, attempts in plans:
        categories = {c["case_id"]: {r["id"] for r in c.get("rubric", [])
                                    if r.get("category") == "reflection"}
                      for c in plan["dataset"]["cases"]}
        for name in plan["config"]["models"]:
            rows = [a for a in attempts if a["model"] == name and _poc_review(a)
                    and categories.get(a["case_id"])]
            items = [i for a in rows for i in _poc_review(a)["required_items"]
                     if i["id"] in categories[a["case_id"]]]
            score = f"{sum(i['verdict']=='pass' for i in items)}/{len(items)}" if items else "미검토"
            lines.append(f"| {name} | {mode} | {score} | {len(rows)}/4 |")
    lines += ["", "### 결과를 해석할 때의 한계", "",
              "생성 한도는 반드시 채워야 할 길이가 아니다. 실제 생성량이 한도보다 훨씬 작고 stop으로 종료한 비추론 답변의 오류를 생성 한도 소진으로 설명할 수 없다. 반대로 이전의 짧은 생성 한도에서 추론만 남기고 끝난 기록과 이번 완료 기록을 비교할 때에는 샘플링, 문맥 창, 시간 제한도 함께 달라졌음을 고려해야 한다.",
              "",
              "이번 각 문항은 한 번만 실행했다. 동일 문항의 두 모드 결과 차이는 관측 결과이며, 반복 안정성이나 추론 기능 하나의 인과 효과를 확정하지 않는다. Qwen의 모드별 공식 샘플링 값이 다르기 때문이다. Gemma는 모드 외 요청 옵션을 유지했으나 실행 순서와 열 상태까지 통제한 무작위 비교는 아니다.",
              "",
              "최종 응답의 필수 항목 점수와 별개로 발견한 잘못된 문장, 추론에서 반복된 확인, 중간 오류가 최종 답변에서 사라졌는지도 원문과 함께 남겼다. 추론 원문의 관찰은 모든 문장에 대한 완전한 논리 검증을 뜻하지 않는다. 검토 주체는 Codex이며 사람이 독립적으로 검토한 결과로 표기하지 않는다."]

    if warmup_ids:
        warmup_rows = [(eid, a) for eid in warmup_ids for a in load_attempts(eid, db_path)
                       if a.get("record")]
        lines += ["", "### 워밍업 별도 기록", "",
                  "아래 W00 호출은 연결과 반환 형식을 확인한 기록이며 본 시험 40회와 사실 30항목 집계에서 제외한다. 매 호출 뒤 모델을 해제하므로 뒤의 본 시험이 모두 메모리에 적재된 상태에서 시작하는 것은 아니다.", "",
                  "| 실험 | 모델 | 상태 | 시간 | 생성량 | JSON |",
                  "| --- | --- | --- | ---: | ---: | --- |"]
        for eid, a in warmup_rows:
            r = a["record"]
            m = r.get("response_metadata") or {}
            lines.append(f"| {eid} | {a['model']} | {m.get('done_reason', r['status'])} | {value(r.get('wall_seconds'))}초 | {m.get('eval_count')} | {(a.get('evaluation') or {}).get('assessment', {}).get('schema_valid')} |")
        for eid, a in warmup_rows:
            lines += ["", "<details>", f"<summary>워밍업 {a['model']}, {eid} 원본</summary>", "",
                      block({"request": a["request"], "record": a["record"], "evaluation": a.get("evaluation")}),
                      "", "</details>", ""]
    lines += ["", "## 입력, 실제 요청 및 응답 원본", "",
        "입력과 정답 대조 기준은 아래에 문항별로 한 번 제시한다. 실제 모델에는 input과 공통 지시문만 전달하며 rubric과 expected는 채점자용 자료이다.", ""]
    for case in plans[0][1]["dataset"]["cases"]:
        lines += ["<details>", f"<summary>{case['case_id']} 입력과 채점 근거</summary>", "",
                  block(case), "", "</details>", ""]
    for mode, plan, attempts in plans:
        lines += [f"### {mode} 실행 식별값과 환경", "", block({
            "experiment_id": plan["experiment_id"] if "experiment_id" in plan else (nonthinking_id if mode == "비추론" else thinking_id),
            "config_hash": plan["config_hash"], "dataset_hash": plan["dataset_hash"],
            "models": plan["config"]["models"], "environment": plan["config"]["environment"],
            "settings_provenance": plan["config"]["settings_provenance"],
            "source_hashes": plan["config"]["source_hashes"]})]
        for a in attempts:
            r = a.get("record")
            if not r:
                continue
            lines += ["", "<details>", f"<summary>{mode}, {a['model']}, {a['case_id']}: 요청, 추론, 답변 및 판정</summary>", "",
                "**실제 요청**", block(a["request"]), "**측정값과 종료 상태**",
                block({k:r.get(k) for k in ("attempt_id", "started_at", "finished_at", "status",
                    "response_metadata", "first_token_seconds", "first_content_seconds", "wall_seconds",
                    "loaded_models", "peak_gpu_used_mib", "errors", "final_response_complete")}),
                "**추론 원문**", block(r.get("thinking", ""), "text"),
                "**최종 답변 원문**", block(r.get("content", ""), "text"),
                "**필수 사실 판정과 발견한 오류**", block(_poc_review(a) or {"status":"미검토"}),
                "**추론 원문의 별도 관찰**", block([e["review"] for e in (a.get("evaluation") or {}).get("reviews", [])
                    if e["review"].get("method") == "source_grounded_thinking"] or {"status": "별도 관찰 기록 없음, 추론 전체 무오류 판정과 구분"}),
                "", "</details>", ""]
    lines += ["", "<details>", "<summary>이전 실행과 대표 문항 확인의 상세 이력</summary>", "",
        "아래 내용은 당시 범위와 실제 결과의 보존본이다. 과거의 실행 예정 또는 종료 표현은 현재 시험 상태를 뜻하지 않는다.",
        "", begin, prior, end, "", "</details>", ""]
    destination.write_text("\n".join(lines), encoding="utf-8")
    return {"stored_calls": total, "reviewed_calls": reviewed, "report_path": str(destination)}


def main():
    """
    PoC 집계 또는 새 Markdown 생성, 모델 호출과 DB 수정 없는 조회

    입력: 명령행의 실험 ID와 경로 및 선택 옵션
    처리: 명시한 준비, 실행 또는 조회 기능으로 분기
    반환: 결과 또는 생성된 실험 ID의 JSON 출력
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment_id")
    parser.add_argument("--db", default="data/evaluation.db")
    parser.add_argument("--markdown")
    parser.add_argument("--completion-check", action="store_true")
    parser.add_argument("--best-practices-thinking-id")
    parser.add_argument("--warmup-ids", nargs="*", default=[])
    parser.add_argument("--reasoning-id", help="같은 영문 입력의 추론 조건 실험 ID")
    parser.add_argument("--budget-ids", nargs="+", help="생성 길이별 추론 조건 ID 목록")
    args = parser.parse_args()
    if args.best_practices_thinking_id:
        if not args.markdown:
            parser.error("공식 권장 설정 보고서의 --markdown 경로 필요")
        result = write_best_practices_report(args.experiment_id, args.best_practices_thinking_id, args.markdown, args.db, args.warmup_ids)
    elif args.completion_check:
        if not args.markdown:
            parser.error("응답 완료 보고서의 --markdown 경로 필요")
        result = write_completion_report(args.experiment_id, args.markdown, args.db)
    elif args.budget_ids:
        if not args.markdown:
            parser.error("생성 길이별 통합 보고서의 --markdown 경로 필요")
        result = write_budget_report(args.experiment_id, args.budget_ids, args.markdown, args.db)
    elif args.reasoning_id:
        if args.markdown:
            result = write_mode_report(args.experiment_id, args.reasoning_id, args.markdown, args.db)
        else:
            result = summarize_modes(args.experiment_id, args.reasoning_id, args.db)
    elif args.markdown:
        result = write_poc_report(args.experiment_id, args.markdown, args.db)
    else:
        result = summarize_poc(args.experiment_id, args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
