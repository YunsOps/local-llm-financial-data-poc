"""
응답 원문과 입력 자료를 연결한 검토 기록

역할:
    - 필수 항목별 판정과 실제 인용 및 근거의 연결
    - 추가 오류의 원본 필드 경로 확인
    - 같은 언어와 모델 및 문항의 완전히 같은 응답에 한정한 근거 재사용

판정 주체: 검토자, 코드에 의한 의미 정확성 자동 확정 없음
"""

import copy
import hashlib

from .evaluation_storage import load_attempts, load_experiment, save_review


METHOD = "source_grounded_required_items"


def record_required_review(experiment_id, attempt_id, items, findings=None, *,
                           reviewer="Codex, 원본 대조 검토", db_path="data/evaluation.db"):
    """
    평가자가 작성한 항목별 판정을 원본과 연결하여 저장하는 함수

    매개변수:
        items: 필수 관찰 항목 순서에 따른 (pass 또는 partial 또는 fail, 인용문, 판단 근거) 목록
        findings: 발견한 오류의 (유형, 인용문, 원본 필드 경로, 설명) 목록
        reviewer: 실제 검토 주체의 이름, 사람 검토로 자동 표기하지 않는 기준

    검증 범위:
        - 필수 관찰 항목 수와 판정 값 확인
        - 응답 원문에 인용문이 실제로 존재하는지 확인
        - 오류 근거의 원본 필드 경로가 해당 사례에 존재하는지 확인
        - 문장 의미의 옳고 그름은 평가자가 판단, 이 함수의 자동 판정 없음
        - 기존 자동 형식 판정과 이전 검토를 덮어쓰지 않는 추가 저장

    입력: 시도 ID, 항목별 판정과 인용 및 근거, 선택적 추가 오류
    반환: 저장된 검토 ID
    효과: 인용과 원본 경로 확인 후 사후 검토 추가
    예외: 존재하지 않는 인용, 판정 개수나 근거 경로 불일치
    """
    experiment = load_experiment(experiment_id, db_path)
    attempt = next((a for a in load_attempts(experiment_id, db_path)
                    if a["attempt_id"] == attempt_id), None)
    if attempt is None or not attempt.get("record"):
        raise ValueError("원본 응답이 저장된 시도 필요")
    # 같은 문항의 언어별 입력 해시와 확인 항목을 구분, 과거 단일 언어 기록도 지원
    language = attempt["record"].get("language")
    case = next(c for c in experiment["dataset"]["cases"]
                if c["case_id"] == attempt["case_id"]
                and (language is None or c.get("language") == language))
    observations = case["input"]["required_observations"]
    if len(items) != len(observations):
        raise ValueError("필수 관찰 항목과 판정 수 불일치")
    raw = attempt["record"]["content"]
    required = []
    for number, (verdict, quote, note) in enumerate(items, 1):
        if verdict not in ("pass", "partial", "fail"):
            raise ValueError("지원하지 않는 판정")
        if not isinstance(quote, str) or (quote and quote not in raw):
            raise ValueError("원문에 없는 인용")
        if verdict == "pass" and not quote:
            raise ValueError("충족 판정의 실제 인용 필요")
        if not isinstance(note, str) or not note.strip():
            raise ValueError("판정 근거 필요")
        required.append({"id": f"R{number:02d}", "instruction": observations[number - 1],
                         "verdict": verdict, "quote": quote, "note": note})
    checked_findings = []
    for kind, quote, path, note in findings or []:
        if not isinstance(quote, str) or (quote and quote not in raw):
            raise ValueError("오류 인용과 원문 불일치")
        if not all(isinstance(value, str) and value.strip() for value in (kind, path, note)):
            raise ValueError("오류 종류와 원본 경로 및 설명 필요")
        value = case["input"]
        for part in path.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        checked_findings.append({"type": kind, "quote": quote, "source_path": path, "note": note})
    review = {
        "method": METHOD, "response_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "dataset_hash": experiment["dataset_hash"], "input_hash": case["input_hash"],
        "case_id": case["case_id"], "required_items": required, "findings": checked_findings,
        "scope": "필수 관찰 항목과 발견한 오류의 원본 대조, 전체 문장의 사실 정확도와 구분",
        "format_policy": "원문 형식 판정 유지, 코드 블록 제거 후 통과로 변경하지 않는 기준",
        "verdict_policy": "pass는 정확한 전체 포함, partial은 일부 누락, fail은 사실 오류 또는 핵심 설명 미충족",
        "not_reviewed": "모든 추가 문장의 완전한 주장 단위 정확도 및 사람의 독립 검토",
    }
    return save_review(attempt_id, reviewer, review, db_path)


def reuse_identical_review(experiment_id, source_attempt_id, target_attempt_id, *,
                          reviewer="Codex, 원본 대조 검토", db_path="data/evaluation.db"):
    """
    같은 사례의 원문이 완전히 동일한 반복에 한하여 기존 근거 연결

    제한:
        - 같은 실험, 사례, 모델의 서로 다른 시도에만 적용
        - 문장 일부 일치나 JSON 키 순서만 같은 경우에는 적용 금지
        - 기존 대상 검토가 있으면 중복 추가 대신 중단
        - 복사 근거의 시도 ID와 검토 ID 보존, 새 독립 검토로 가장하지 않는 기준

    입력: 같은 실험의 원본 및 대상 시도 ID
    반환: 재사용 근거를 포함한 새 검토 ID
    제한: 같은 모델, 문항, 언어와 응답 전체 일치 필수
    예외: 원문 차이 또는 대상에 기존 검토 존재 시 중단
    """
    attempts = {a["attempt_id"]: a for a in load_attempts(experiment_id, db_path)}
    source, target = attempts[source_attempt_id], attempts[target_attempt_id]
    if source_attempt_id == target_attempt_id:
        raise ValueError("서로 다른 시도 필요")
    if (source["case_id"], source["model"], (source.get("record") or {}).get("language")) != (
            target["case_id"], target["model"], (target.get("record") or {}).get("language")):
        raise ValueError("동일 사례와 모델 필요")
    if not source.get("record") or not target.get("record"):
        raise ValueError("양쪽 원본 필요")
    if source["record"]["content"] != target["record"]["content"]:
        raise ValueError("원문 전체 불일치")
    if any(e["review"].get("method") == METHOD for e in (target["evaluation"] or {}).get("reviews", [])):
        raise ValueError("대상 검토 존재, 자동 덮어쓰기 금지")
    entries = [e for e in (source["evaluation"] or {}).get("reviews", [])
               if e["review"].get("method") == METHOD]
    if not entries:
        raise ValueError("원본 검토 없음")
    entry = entries[-1]
    review = copy.deepcopy(entry["review"])
    review["same_text_basis"] = {"attempt_id": source_attempt_id, "review_id": entry["review_id"],
                                "check": "동일 사례와 모델의 응답 원문 전체 일치 확인"}
    return save_review(target_attempt_id, reviewer, review, db_path)


# 오류 회고에 미리 넣은 주장별 대조 기준, 모델 입력과 별도로 유지하는 평가 자료


def record_thinking_review(experiment_id, attempt_id, findings, *,
                           reviewer="Codex, 추론 원문과 최종 답변 대조", db_path="data/evaluation.db"):
    """
    추론 원문의 반복과 사실 오류, 최종 답변에서의 정정 여부 기록

    입력: 실험 및 시도 ID, 추론 인용과 근거를 포함한 발견 사항 목록
    항목: type, quote, source_path, note, resolved_in_final의 사전
    확인: 추론 원문에 실제 인용 존재, 지정한 원본 필드 경로의 존재
    구분: 최종 답변의 필수 세 항목 점수와 별도 저장, 중간 오류의 중복 감점 없음
    한계: 문장 의미와 정정 여부는 검토자의 판정, 함수 자체의 자동 의미 채점 없음
    """
    experiment = load_experiment(experiment_id, db_path)
    attempt = next(a for a in load_attempts(experiment_id, db_path) if a["attempt_id"] == attempt_id)
    record = attempt.get("record")
    if not record or not record.get("thinking"):
        raise ValueError("저장된 추론 원문 필요")
    case = next(c for c in experiment["dataset"]["cases"]
                if c["case_id"] == attempt["case_id"] and c["language"] == record["language"])
    checked = []
    for finding in findings:
        item = copy.deepcopy(finding)
        if not item.get("quote") or item["quote"] not in record["thinking"]:
            raise ValueError("추론 원문에 없는 인용")
        if not item.get("type") or not item.get("note"):
            raise ValueError("발견 종류와 판단 근거 필요")
        if item.get("resolved_in_final") not in (True, False, None):
            raise ValueError("최종 정정 여부는 참, 거짓 또는 확인 불가 필요")
        if item.get("source_path"):
            value = case["input"]
            for part in item["source_path"].split("."):
                value = value[int(part)] if isinstance(value, list) else value[part]
        checked.append(item)
    review = {"method": "source_grounded_thinking", "findings": checked,
              "response_hash": hashlib.sha256(record["content"].encode("utf-8")).hexdigest(),
              "thinking_hash": hashlib.sha256(record["thinking"].encode("utf-8")).hexdigest(),
              "scope": "추론의 관측 오류와 반복 및 최종 정정 여부, 최종 답변 점수와 별도 판정",
              "not_reviewed": "모든 추론 문장의 완전한 논리적 타당성, 사람의 독립 검토"}
    return save_review(attempt_id, reviewer, review, db_path)
