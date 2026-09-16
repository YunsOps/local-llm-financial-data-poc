"""
PoC storage 코드의 자동 검사

검사 방식:
    - 가상 입력과 임시 DB 또는 대체 응답 사용
    - 정상 사례와 실패 및 경계 사례의 결과 대조
    - 외부 모델 및 유료 API 호출 없음

용도: 실행과 저장 코드의 오류 확인, 실제 모델 품질 평가와 별도 구분
"""

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from . import evaluation_storage as storage
from .evaluation_poc import load_cases


class EvaluationStorageTests(unittest.TestCase):
    """실제 모델 호출 없이 저장, 중단과 재개 및 원본 보존 확인"""

    def setUp(self):
        """
        각 검사에 독립적으로 사용할 가상 입력과 임시 기록의 준비
        """
        self.folder = tempfile.TemporaryDirectory()
        self.db = str(Path(self.folder.name) / "evaluation.sqlite")
        self.request = {"model": "test:model", "messages": [{"role": "user", "content": "원문"}]}
        self.config = {"models": ["test:model"], "repeats": 2}
        self.dataset = {"cases": [{"case_id": "X01", "input": {"value": 3},
                                    "expected": {"value": 3}}]}
        storage.save_experiment("test", self.config, self.dataset, self.db)

    def tearDown(self):
        """
        해당 검사에서 사용한 임시 파일과 DB의 정리
        """
        self.folder.cleanup()

    def begin(self, slot="main/1", **kwargs):
        """
        검사에서 사용하는 고정 실험의 시도 등록 보조
        """
        return storage.begin_attempt("test", slot, "X01", "test:model", 1,
                                     self.request, db_path=self.db, **kwargs)

    def record(self, status="completed"):
        """
        검사용 원본 또는 검토 객체 생성과 저장 결과 조회 보조
        """
        return {"run_id": "call-1", "started_at": "2026-09-15T00:00:00+00:00",
                "request": copy.deepcopy(self.request), "status": status,
                "content": '{"value":3}', "errors": [], "raw_lines": ["원본 스트림"]}

    def test_all_fixed_cases_and_answers_roundtrip(self):
        """
        현재 14개 문항과 언어 조합의 입력, 정답과 해시 저장 및 재조회 일치 확인
        """
        dataset = load_cases()
        storage.save_experiment("full-cases", {"purpose": "offline_validation"}, dataset, self.db)
        restored = storage.load_experiment("full-cases", self.db)["dataset"]
        self.assertEqual(restored, dataset)
        self.assertEqual(len(restored["cases"]), 14)
        self.assertTrue(all(case["expected"] and case["input_hash"] for case in restored["cases"]))

    def test_frozen_experiment_and_roundtrip(self):
        """
        같은 실험 ID의 자료 변경 차단과 동일 자료 재등록 허용 확인
        """
        result = storage.load_experiment("test", self.db)
        self.assertEqual(result["config"], self.config)
        self.assertEqual(result["dataset"], self.dataset)
        self.assertFalse(storage.save_experiment("test", self.config, self.dataset, self.db))
        with self.assertRaises(ValueError):
            storage.save_experiment("test", {"models": ["other"]}, self.dataset, self.db)

    def test_duplicate_position_is_not_reexecuted(self):
        """
        기존 실행 위치의 중복 등록 방지, 다른 요청으로의 위치 재사용 차단 확인
        """
        self.assertIsInstance(self.begin(), str)
        self.assertIsNone(self.begin())
        self.request["messages"][0]["content"] = "다른 원문"
        with self.assertRaises(ValueError):
            self.begin()

    def test_atomic_result_roundtrip_and_no_overwrite(self):
        """
        원본과 판정의 동시 저장, 재조회 일치 및 완료 기록 덮어쓰기 거부 확인
        """
        attempt_id = self.begin()
        record = self.record()
        grading = {"schema_valid": True, "review_status": "pending"}
        storage.finish_attempt(attempt_id, record, grading, self.db)
        result = storage.load_attempts("test", self.db)[0]
        self.assertEqual(result["record"], record)
        self.assertEqual(result["evaluation"], grading)
        self.assertEqual(result["status"], "completed")
        with self.assertRaises(ValueError):
            storage.finish_attempt(attempt_id, record, grading, self.db)

    def test_changed_actual_request_is_rejected_without_partial_write(self):
        """
        등록 요청과 실제 요청 불일치 시 원본의 부분 저장 방지 확인
        """
        attempt_id = self.begin()
        record = self.record()
        record["request"]["messages"][0]["content"] = "다른 요청"
        with self.assertRaises(ValueError):
            storage.finish_attempt(attempt_id, record, {}, self.db)
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evaluation_calls").fetchone()[0], 0)
        self.assertEqual(storage.load_attempts("test", self.db)[0]["status"], "running")

    def test_interruption_and_explicit_retry_keep_first_attempt(self):
        """
        중단 기록과 명시적 재시도의 분리, 최초 시도의 보존 확인
        """
        first = self.begin()
        with self.assertRaises(ValueError):
            self.begin("retry/1", phase="retry", retry_of=first)
        storage.mark_interrupted(first, "실행 프로세스 종료 확인", self.db)
        second = self.begin("retry/1", phase="retry", retry_of=first)
        storage.finish_attempt(second, self.record(), {}, self.db)
        result = storage.load_attempts("test", self.db)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["status"], "interrupted")
        self.assertEqual(result[1]["retry_of"], first)
        self.assertEqual(result[1]["phase"], "retry")
        self.assertIsNone(self.begin())

    def test_failed_response_is_preserved(self):
        """
        실패 상태와 응답 원문 및 오류 정보의 저장 확인
        """
        first = self.begin()
        record = self.record("timeout")
        record.update(content="부분 응답", errors=[{"type": "timeout"}])
        storage.finish_attempt(first, record, {"schema_valid": False}, self.db)
        result = storage.load_attempts("test", self.db)[0]
        self.assertEqual(result["record"]["content"], "부분 응답")
        self.assertEqual(result["status"], "timeout")

    def test_saved_request_corruption_is_detected(self):
        """
        저장된 요청의 사후 변조를 해시 비교로 검출하는 처리 확인
        """
        self.begin()
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE evaluation_attempts SET request_json=?",
                               (json.dumps({"model": "modified"}),))
        with self.assertRaises(ValueError):
            storage.load_attempts("test", self.db)

    def test_failed_second_write_rolls_back(self):
        """
        두 번째 저장 실패 시 첫 저장까지 취소하는 트랜잭션 확인
        """
        first = self.begin()
        storage.finish_attempt(first, self.record(), {}, self.db)
        second = self.begin("main/2")
        with self.assertRaises(sqlite3.IntegrityError):
            storage.finish_attempt(second, self.record(), {}, self.db)
        result = storage.load_attempts("test", self.db)
        self.assertEqual(result[1]["status"], "running")
        self.assertIsNone(result[1]["record"])

    def test_reviews_append_without_overwriting_automatic_result(self):
        """
        사후 검토의 추가 기록, 이전 검토와 자동 판정 및 원본 보존 확인
        """
        first = self.begin()
        record = self.record()
        storage.finish_attempt(first, record, {"schema_valid": True}, self.db)
        review = {"response_hash": hashlib.sha256(record["content"].encode()).hexdigest(),
                  "findings": ["수동 대조 메모"]}
        first_review = storage.save_review(first, "test-reviewer", review, self.db)
        second_review = storage.save_review(first, "test-reviewer", dict(review, findings=["추가 검토"]), self.db)
        result = storage.load_attempts("test", self.db)[0]
        self.assertNotEqual(first_review, second_review)
        self.assertEqual(result["record"], record)
        self.assertTrue(result["evaluation"]["schema_valid"])
        self.assertEqual(len(result["evaluation"]["reviews"]), 2)
        self.assertEqual(result["evaluation"]["reviews"][0]["review"], review)
        with self.assertRaises(ValueError):
            storage.save_review(first, "test-reviewer", {"response_hash": "wrong"}, self.db)

    def test_identity_and_secret_checks(self):
        """
        모델과 실행 식별값의 일치, 인증 정보가 있는 요청의 저장 거부 확인
        """
        with self.assertRaises(ValueError):
            self.begin("retry/1", phase="retry")
        self.request["headers"] = {"Authorization": "fake-value"}
        with self.assertRaises(ValueError):
            self.begin()
        self.assertEqual(storage.load_attempts("test", self.db), [])


if __name__ == "__main__":
    unittest.main()
