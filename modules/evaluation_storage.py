"""
실험 계획과 모델 원본 및 검토의 SQLite 저장

테이블:
    evaluation_experiments: 입력, 정답과 설정의 고정 원문
    evaluation_attempts: 계획 내 시도와 실제 요청 및 판정
    evaluation_calls: 모델 응답과 실행 측정 원문

보존 원칙: 해시 대조, 트랜잭션 적용, 기존 응답 덮어쓰기 차단
"""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .json_utils import _to_json, _reject_constant, _unique_object


def _hash(value):
    """
    키 순서에 영향을 받지 않는 원본 식별값 계산

    입력: 해시를 계산할 설정 또는 자료 객체
    반환: 정렬 JSON의 SHA-256 16진수 문자열
    목적: 입력과 설정 변경 여부의 재조회 확인
    """
    return hashlib.sha256(_to_json(value).encode("utf-8")).hexdigest()


def _read(raw):
    """
    중복 키와 비정상 숫자를 허용하지 않는 저장 원문 읽기

    입력: SQLite에 보관한 JSON 원문
    반환: 중복 키와 비정상 숫자가 없는 Python 객체
    예외: 원문 파싱 실패의 호출자 전달
    """
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


@contextmanager
def _database(db_path):
    """
    실험 저장용 연결의 생성과 확정 또는 취소 및 해제

    구성:
        - 실험별 고정 자료와 설정의 보존
        - 호출 전 시도 등록, 중단 후 같은 시도의 무단 재실행 방지
        - 평가용 세 테이블의 존재 확인과 최초 생성, 기존 자료 삭제 없음
        - 입력 등록과 완료 기록의 트랜잭션 적용

    입력: SQLite 파일 경로
    반환: contextmanager를 통한 행 이름 조회 가능 연결
    효과: 부모 폴더 및 실험 테이블 준비, 외래 키 활성화
    종료: 정상 시 확정, 예외 시 취소와 연결 해제
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS evaluation_calls (
                run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                model TEXT NOT NULL, phase TEXT NOT NULL, record_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluation_experiments (
                experiment_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                config_hash TEXT NOT NULL, config_json TEXT NOT NULL,
                dataset_hash TEXT NOT NULL, dataset_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluation_attempts (
                attempt_id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES evaluation_experiments(experiment_id),
                slot_id TEXT NOT NULL, phase TEXT NOT NULL, case_id TEXT NOT NULL,
                model TEXT NOT NULL, repeat_number INTEGER NOT NULL,
                retry_of TEXT REFERENCES evaluation_attempts(attempt_id),
                started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, request_hash TEXT NOT NULL, request_json TEXT NOT NULL,
                call_id TEXT UNIQUE REFERENCES evaluation_calls(run_id),
                evaluation_json TEXT, note TEXT,
                UNIQUE(experiment_id, slot_id)
            );
        """)
        with connection:
            yield connection
    finally:
        connection.close()


def save_experiment(experiment_id, config, dataset, db_path="data/evaluation.db"):
    """
    실행 전 설정과 전체 고정 사례의 원문 보존

    주의사항:
        - dataset은 평가용 정답을 포함하므로 모델 입력으로 사용 금지
        - config에 모델 식별값, 실행 순서, 지시문 및 제한값의 확정 내용 기록
        - 같은 ID와 같은 자료의 재등록은 허용, 다른 자료로 덮어쓰기 금지
        - 설정 변경 시 새 experiment_id 사용

    입력: 새 실험 ID, 설정과 평가자용 전체 자료, DB 경로
    반환: 새 저장 True, 완전히 같은 기존 자료의 재등록 False
    예외: 같은 ID의 입력이나 설정 변경 시 ValueError
    """
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ValueError("실험 ID 필요")
    config_hash, dataset_hash = _hash(config), _hash(dataset)
    with _database(db_path) as connection:
        previous = connection.execute(
            "SELECT config_hash, dataset_hash FROM evaluation_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if previous:
            if (previous["config_hash"], previous["dataset_hash"]) != (config_hash, dataset_hash):
                raise ValueError("같은 실험 ID의 설정 또는 자료 변경 금지")
            return False
        connection.execute(
            "INSERT INTO evaluation_experiments VALUES (?, ?, ?, ?, ?, ?)",
            (experiment_id, datetime.now(timezone.utc).isoformat(), config_hash,
             _to_json(config), dataset_hash, _to_json(dataset)),
        )
    return True


def load_experiment(experiment_id, db_path="data/evaluation.db"):
    """
    보존한 설정과 사례의 재조회 및 저장 중 손상 여부 확인

    입력: 실험 ID와 DB 경로
    반환: 저장 원문을 복원하고 해시를 확인한 실험 사전
    예외: 없는 실험 또는 설정과 자료의 해시 불일치
    주의: DB 경로 오타 시 새 파일 생성 가능, 제출 원본 조회 시 경로 명시
    """
    with _database(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM evaluation_experiments WHERE experiment_id=?", (experiment_id,)
        ).fetchone()
    if row is None:
        raise ValueError("등록되지 않은 실험")
    result = dict(row)
    for field in ("config", "dataset"):
        result[field] = _read(result.pop(field + "_json"))
        if _hash(result[field]) != result[field + "_hash"]:
            raise ValueError("저장된 " + field + " 해시 불일치")
    return result


def begin_attempt(experiment_id, slot_id, case_id, model, repeat_number, request,
                  *, phase="main", retry_of=None, db_path="data/evaluation.db"):
    """
    네트워크 호출 전에 시도와 실제 전송 예정 본문을 먼저 등록

    매개변수:
        slot_id: 실행 계획 안의 고유 위치, 예: main/Q01/1/qwen
        request: 인증 헤더를 제외한 모델 API의 전송 본문
        phase: main, warmup, retry, extension 또는 preflight
        retry_of: 재시도의 최초 또는 이전 실패 시도 ID

    반환값:
        str 또는 None: 새 시도 ID, 이미 등록된 위치이면 None

    재개 기준:
        - 완료 여부와 무관하게 기존 위치의 자동 재호출 금지
        - 중단 시도의 원본 보존 후 명시적인 별도 retry 위치로 재실행
        - 재시도를 본 실험의 최초 시도로 바꾸지 않는 구성

    입력: 실험과 실행 위치, 문항과 모델, 회차 및 실제 요청
    동시성: BEGIN IMMEDIATE와 실행 위치 UNIQUE 제약의 병용
    효과: 호출 전에 running 시도 저장, 모델 호출 자체는 없음
    """
    if phase not in ("main", "warmup", "retry", "extension", "preflight"):
        raise ValueError("시도 구분 오류")
    if not all(isinstance(value, str) and value.strip() for value in
               (experiment_id, slot_id, case_id, model)):
        raise ValueError("실험 위치와 사례 및 모델 ID 필요")
    if type(repeat_number) is not int or repeat_number < 1:
        raise ValueError("1 이상의 반복 회차 필요")
    if request.get("model") != model:
        raise ValueError("전송 요청과 기록 모델 불일치")
    if any(key.lower() in ("authorization", "api_key", "headers") for key in request):
        raise ValueError("인증 정보의 실험 기록 금지")
    if (phase == "retry") != (retry_of is not None):
        raise ValueError("재시도와 이전 시도 연결 필요")
    attempt_id = str(uuid4())
    with _database(db_path) as connection:
        # 두 실행기가 동시에 같은 위치를 등록하는 경우의 중복 호출 방지
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT request_hash, case_id, model, repeat_number, phase, retry_of "
            "FROM evaluation_attempts WHERE experiment_id=? AND slot_id=?",
            (experiment_id, slot_id),
        ).fetchone()
        identity = (_hash(request), case_id, model, repeat_number, phase, retry_of)
        if existing:
            if tuple(existing) != identity:
                raise ValueError("기존 실행 위치의 요청 또는 식별 정보 변경 금지")
            return None
        if retry_of:
            previous = connection.execute(
                "SELECT experiment_id, case_id, model, status FROM evaluation_attempts "
                "WHERE attempt_id=?", (retry_of,),
            ).fetchone()
            if (previous is None or tuple(previous)[:3] != (experiment_id, case_id, model)
                    or previous["status"] == "running"):
                raise ValueError("재시도 대상의 실험 또는 사례 불일치, 실행 중 재시도 금지")
        connection.execute(
            "INSERT INTO evaluation_attempts "
            "(attempt_id, experiment_id, slot_id, phase, case_id, model, repeat_number, "
            "retry_of, started_at, status, request_hash, request_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attempt_id, experiment_id, slot_id, phase, case_id, model, repeat_number,
             retry_of, datetime.now(timezone.utc).isoformat(), "running",
             _hash(request), _to_json(request)),
        )
    return attempt_id


def finish_attempt(attempt_id, record, evaluation, db_path="data/evaluation.db"):
    """
    원본 호출과 자동 판정의 한 번 저장 및 시도 완료

    주의사항:
        - 호출 전에 등록한 본문과 실제 호출 본문의 완전 일치 확인
        - 실패 응답도 그대로 저장, 평가 실패를 성공 응답으로 대체 금지
        - 내용 판정의 pending은 미검토 유지, 정상 JSON만으로 사실 정확성 확정 금지
        - 호출 원문 삽입과 완료 상태 변경의 동시 확정, 일부만 저장되는 상태 방지

    입력: 등록된 시도 ID, 실제 원본 기록과 자동 판정
    반환: 별도 값 없음
    효과: 원본 호출 삽입과 시도 종료를 하나의 트랜잭션으로 확정
    예외: 다른 요청 또는 이미 종료한 시도의 재저장 거부
    """
    if record.get("status") not in ("completed", "failed", "timeout"):
        raise ValueError("호출 종료 상태 필요")
    with _database(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM evaluation_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None or row["status"] != "running":
            raise ValueError("완료 또는 미등록 시도의 덮어쓰기 금지")
        if _hash(record["request"]) != row["request_hash"]:
            raise ValueError("예정 요청과 실제 요청 불일치")
        # 기존 예비 호출과 같은 형식으로 원본 보존, 호출 ID의 재사용 금지
        connection.execute(
            "INSERT INTO evaluation_calls VALUES (?, ?, ?, ?, ?)",
            (record["run_id"], record["started_at"], row["model"], row["phase"],
             _to_json(record)),
        )
        connection.execute(
            "UPDATE evaluation_attempts SET status=?, finished_at=?, call_id=?, "
            "evaluation_json=? WHERE attempt_id=?",
            (record["status"], datetime.now(timezone.utc).isoformat(), record["run_id"],
             _to_json(evaluation), attempt_id),
        )


def mark_interrupted(attempt_id, reason, db_path="data/evaluation.db"):
    """
    실행기가 끝난 것을 확인한 시도에 중단 사유 기록, 자동 재시도 없음

    입력: 시도 ID, 실제 실행 중단을 확인한 사유
    효과: running 시도의 상태와 종료 시각 및 사유만 변경
    주의: 프로세스 종료 기능 없음, 실행기의 종료 확인 후 사용
    """
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("중단 확인 사유 필요")
    with _database(db_path) as connection:
        result = connection.execute(
            "UPDATE evaluation_attempts SET status='interrupted', finished_at=?, note=? "
            "WHERE attempt_id=? AND status='running'",
            (datetime.now(timezone.utc).isoformat(), reason, attempt_id),
        )
        if result.rowcount != 1:
            raise ValueError("실행 중으로 남은 시도만 중단 표시 가능")


def load_attempts(experiment_id, db_path="data/evaluation.db"):
    """
    원본과 자동 판정 및 중단 기록의 실행 순서별 재조회

    입력: 실험 ID와 DB 경로
    반환: 등록 순서의 요청, 원본 응답과 검토 목록
    검사: 저장 요청 해시와 연결된 호출 요청의 일치
    보존: 원본이 없는 시도도 목록에 포함
    """
    load_experiment(experiment_id, db_path)
    with _database(db_path) as connection:
        rows = connection.execute(
            "SELECT a.*, c.record_json FROM evaluation_attempts a "
            "LEFT JOIN evaluation_calls c ON c.run_id=a.call_id "
            "WHERE experiment_id=? ORDER BY a.rowid", (experiment_id,),
        ).fetchall()
    results = []
    for row in rows:
        value = dict(row)
        value["request"] = _read(value.pop("request_json"))
        if _hash(value["request"]) != value["request_hash"]:
            raise ValueError("저장 요청 해시 불일치")
        value["record"] = _read(value["record_json"]) if value["record_json"] else None
        value["evaluation"] = _read(value["evaluation_json"]) if value["evaluation_json"] else None
        del value["record_json"], value["evaluation_json"]
        if value["record"] and _hash(value["record"]["request"]) != value["request_hash"]:
            raise ValueError("저장 원본과 예정 요청 불일치")
        results.append(value)
    return results

def save_review(attempt_id, reviewer, review, db_path="data/evaluation.db"):
    """
    원본 응답에 대한 사후 사실 검토의 추가 기록

    기준:
        - 응답 해시 대조 후 자동 판정과 원문을 유지한 채 reviews 목록에 추가
        - 판정 수정도 새 항목으로 기록, 이전 검토 덮어쓰기 없음
        - 문장별 판정은 평가자가 수행, 이 함수는 의미 정확성 자동 판정 없음
        - 검토별 ID와 시각 및 판정자와 검토 원문의 해시 보존

    입력: 원본이 있는 시도 ID, 검토자와 검토 객체
    반환: 새 검토 ID
    효과: reviews에 새 항목 추가, 기존 자동 판정과 과거 검토 보존
    검사: 검토 대상 응답 해시와 저장 원문의 일치
    """
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(review, dict):
        raise ValueError("판정자와 검토 객체 필요")
    with _database(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT a.evaluation_json, c.record_json FROM evaluation_attempts a "
            "JOIN evaluation_calls c ON c.run_id=a.call_id WHERE a.attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise ValueError("원본 호출을 저장한 시도만 검토 가능")
        record = _read(row["record_json"])
        response_hash = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
        if review.get("response_hash") != response_hash:
            raise ValueError("검토와 원본 응답 해시 불일치")
        entry = {"review_id": str(uuid4()), "reviewer": reviewer,
                 "created_at": datetime.now(timezone.utc).isoformat(),
                 "review_hash": _hash(review), "review": review}
        evaluation = _read(row["evaluation_json"])
        evaluation.setdefault("reviews", []).append(entry)
        connection.execute("UPDATE evaluation_attempts SET evaluation_json=? WHERE attempt_id=?",
                           (_to_json(evaluation), attempt_id))
    return entry["review_id"]
