"""
선택한 가상 실험의 제출용 SQLite 생성

처리 순서:
    1. 대상 실험과 입력 및 요청 해시 확인
    2. 인증 정보와 실제 계좌 자료의 혼입 검사
    3. 실험, 시도와 연결된 원본 호출 복사
    4. 집계 코드와 내보내기 범위의 기록 및 DB 무결성 검사

보호 범위: 원본 DB와 기존 목적지의 덮어쓰기 금지
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

from .evaluation_storage import _database, _read, _hash


def _reject_credentials(value, configured_secrets):
    """
    내보낼 원문에 인증 정보가 들어 있는지 검사하는 함수

    기준:
        - 인증 헤더와 키 및 암호 필드에 실제 값이 있으면 중단
        - 현재 환경과 프로젝트 설정에 있는 비밀값의 원문 포함 시 중단
        - 오류 메시지에 비밀값이나 해당 문장 출력 없음
        - 원본 수정이나 마스킹으로 기록 해시를 바꾸지 않는 처리
        - 검사 통과가 모든 개인정보 부재를 보증하는 것은 아니므로 명시적 실험 선택 필요

    입력: 내보낼 객체와 현재 설정에서 얻은 비밀값 목록
    효과: 값 포함, 키 형태와 인증 필드 검사
    예외: 의심 값 발견 시 내용을 출력하지 않고 ValueError
    """
    encoded = json.dumps(value, ensure_ascii=False)
    if any(secret in encoded for secret in configured_secrets):
        raise ValueError("인증 정보 포함 가능성 발견, 내보내기 중단")
    if re.search(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}", encoded):
        raise ValueError("인증 키 형태 발견, 내보내기 중단")
    def visit(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key.lower() in {"authorization", "api_key", "access_key", "secret_key", "password"} and child:
                    raise ValueError("인증 필드 포함, 내보내기 중단")
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
    visit(value)


def _configured_secrets():
    """
    현재 프로젝트와 환경의 비밀값 목록 구성, 값의 출력 및 저장 없음

    반환: 환경 변수와 .env에서 추린 길이 12 이상 비밀값 목록
    선택: 이름에 key, secret, token 또는 password 포함
    목적: 내보내기 검사에만 사용, 목록 자체의 로그 기록 금지
    """
    root = Path(__file__).resolve().parent.parent
    values = {**dotenv_values(root / ".env"), **os.environ}
    return [value for key, value in values.items()
            if any(word in key.lower() for word in ("key", "secret", "token", "password"))
            and isinstance(value, str) and len(value.strip()) >= 12]


def export_experiments(experiment_ids, destination, source="data/evaluation.db"):
    """
    지정한 가상 실험만 별도 SQLite로 복사하는 제출 자료 생성 함수

    포함:
        - 선택한 실험의 고정 설정, 입력과 평가용 정답 및 실행 코드 원문
        - 해당 실험에 속한 시도, 연결된 원본 응답과 측정값 및 사후 검토
        - 원본과 같은 설정 및 자료 해시, 내보낸 시점과 실험 ID 목록
        - 현재 집계 및 검토 코드의 원문과 해시

    제외:
        - 선택하지 않은 실험과 예비 호출, 실제 계좌 및 거래 테이블
        - 환경 파일과 인증 키, 모델 가중치 및 가상환경
        - 실행 중인 시도가 있는 실험의 불완전한 복사

    보존:
        - 읽기 전용 원본 연결과 단일 읽기 트랜잭션 사용
        - 원본 DB 및 이미 존재하는 목적지의 덮어쓰기 거부
        - 원문과 해시의 자동 수정 없음, 복사 실패 시 임시 결과만 제거
        - 미실행 계획은 미실행 상태 그대로 보존, 성공 기록의 보충 없음

    입력: 중복 없는 실험 ID 목록, 새 목적지와 원본 DB 경로
    반환: 파일 경로, 실험과 시도 및 호출 수, 크기와 SHA-256
    효과: 선택한 가상 실험과 분석 코드만 새 SQLite에 복사
    예외: 실행 중, 해시 손상, 인증 정보, 기존 목적지 또는 무결성 실패
    """
    ids = list(dict.fromkeys(experiment_ids))
    if not ids or len(ids) != len(experiment_ids):
        raise ValueError("중복 없는 실험 ID 목록 필요")
    src, dest = Path(source).resolve(), Path(destination).resolve()
    if not src.is_file() or dest == src or dest.exists():
        raise ValueError("기존 원본과 다른 새 목적지 필요")
    if dest.suffix not in {".sqlite", ".db"}:
        raise ValueError("SQLite 파일 확장자 필요")
    if not dest.parent.is_dir():
        raise ValueError("이미 존재하는 목적지 폴더 필요")
    secrets = _configured_secrets()
    experiments, attempts, calls = [], [], {}
    with closing(sqlite3.connect(src.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        for eid in ids:
            row = connection.execute("SELECT * FROM evaluation_experiments WHERE experiment_id=?", (eid,)).fetchone()
            if row is None:
                raise ValueError("등록되지 않은 실험 ID")
            exp = dict(row)
            config, dataset = _read(exp["config_json"]), _read(exp["dataset_json"])
            if _hash(config) != exp["config_hash"] or _hash(dataset) != exp["dataset_hash"]:
                raise ValueError("실험 설정 또는 원본 자료 해시 불일치")
            _reject_credentials(exp, secrets)
            # 임의의 실제 계좌 실험을 기본 제출 경로로 복사하지 않는 자료 범위 검사
            cases = dataset.get("cases") or []
            if not cases or any(not ("가상" in c.get("input", {}).get("data_notice", "")
                                  or re.search(r"\bsynthetic\b", c.get("input", {}).get("data_notice", ""), re.I))
                                for c in cases):
                raise ValueError("가상 자료 표시가 있는 고정 사례만 내보내기 가능")
            _reject_credentials(config, secrets)
            _reject_credentials(dataset, secrets)
            experiments.append(exp)
            rows = connection.execute("SELECT * FROM evaluation_attempts WHERE experiment_id=? ORDER BY started_at, attempt_id", (eid,))
            for item in rows:
                attempt = dict(item)
                if attempt["status"] == "running":
                    raise ValueError("실행 중인 시도 포함, 해당 실험 종료 후 내보내기 필요")
                request = _read(attempt["request_json"])
                if _hash(request) != attempt["request_hash"]:
                    raise ValueError("저장 요청 해시 불일치")
                _reject_credentials(attempt, secrets)
                _reject_credentials(request, secrets)
                if attempt["evaluation_json"]:
                    _reject_credentials(_read(attempt["evaluation_json"]), secrets)
                attempts.append(attempt)
                if attempt["call_id"]:
                    call = connection.execute("SELECT * FROM evaluation_calls WHERE run_id=?", (attempt["call_id"],)).fetchone()
                    if call is None:
                        raise ValueError("연결된 원본 호출 누락")
                    record = _read(call["record_json"])
                    if _hash(record["request"]) != attempt["request_hash"]:
                        raise ValueError("원본 응답과 요청 해시 불일치")
                    _reject_credentials(dict(call), secrets)
                    _reject_credentials(record, secrets)
                    calls[call["run_id"]] = dict(call)
        connection.rollback()
    selected_attempts = {a["attempt_id"] for a in attempts}
    if any(a["retry_of"] and a["retry_of"] not in selected_attempts for a in attempts):
        raise ValueError("재시도 원본을 포함한 실험 선택 필요")
    root = Path(__file__).resolve().parent.parent
    analysis_files = ["modules/evaluation_report.py", "modules/evaluation_review.py", "modules/evaluation_export.py"]
    analysis = {}
    for name in analysis_files:
        raw = (root / name).read_bytes()
        analysis[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "content": raw.decode("utf-8")}
    manifest = {"exported_at": datetime.now(timezone.utc).isoformat(),
                "experiment_ids": ids, "experiment_count": len(experiments),
                "attempt_count": len(attempts), "call_count": len(calls),
                "analysis_sources": analysis,
                "scope": "명시적으로 선택한 가상 실험과 연결된 호출만 포함, 원본 내용 및 해시 유지"}
    _reject_credentials(manifest, secrets)
    try:
        # 배타적 파일 생성으로 확인 이후 목적지가 생긴 경우에도 덮어쓰기 방지
        with dest.open("xb"):
            pass
    except FileExistsError:
        raise ValueError("목적지 생성 충돌, 기존 파일 유지") from None
    try:
        with _database(dest) as output:
            for table, rows in (("evaluation_experiments", experiments),
                                ("evaluation_calls", list(calls.values())),
                                ("evaluation_attempts", attempts)):
                if rows:
                    columns = list(rows[0])
                    query = "INSERT INTO " + table + " (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")"
                    output.executemany(query, [[row[key] for key in columns] for row in rows])
            output.execute("CREATE TABLE evaluation_export_manifest (manifest_json TEXT NOT NULL)")
            output.execute("INSERT INTO evaluation_export_manifest VALUES (?)",
                           (json.dumps(manifest, ensure_ascii=False),))
            if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or output.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("복사 결과 무결성 검사 실패")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return {"path": str(dest), "experiments": len(experiments), "attempts": len(attempts),
            "calls": len(calls), "bytes": dest.stat().st_size,
            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()}


def main():
    """
    새 모델 호출과 원본 수정 없이 선택한 결과를 별도 SQLite로 내보내는 명령

    입력: 명령행의 실험 ID와 경로 및 선택 옵션
    처리: 명시한 준비, 실행 또는 조회 기능으로 분기
    반환: 결과 또는 생성된 실험 ID의 JSON 출력
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment_ids", nargs="+")
    parser.add_argument("--output", required=True)
    parser.add_argument("--db", default="data/evaluation.db")
    args = parser.parse_args()
    print(json.dumps(export_experiments(args.experiment_ids, args.output, args.db), ensure_ascii=False))


if __name__ == "__main__":
    main()
