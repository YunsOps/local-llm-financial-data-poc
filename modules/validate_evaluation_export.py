"""
PoC export 코드의 자동 검사

검사 방식:
    - 가상 입력과 임시 DB 또는 대체 응답 사용
    - 정상 사례와 실패 및 경계 사례의 결과 대조
    - 외부 모델 및 유료 API 호출 없음

용도: 실행과 저장 코드의 오류 확인, 실제 모델 품질 평가와 별도 구분
"""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from .evaluation_export import export_experiments
from .evaluation_storage import save_experiment, begin_attempt, finish_attempt, load_attempts, load_experiment


class ExportTests(unittest.TestCase):
    """실제 API 호출 없이 원본 보존, 선택 범위 및 인증 정보 차단을 확인하는 검사"""

    def setUp(self):
        """
        각 검사에 독립적으로 사용할 가상 입력과 임시 기록의 준비
        """
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "source.sqlite"
        self.output = Path(self.temp.name) / "selected.sqlite"
        self.secrets = patch("modules.evaluation_export._configured_secrets", return_value=["example-private-value-123456"])
        self.secrets.start()
        self.addCleanup(self.secrets.stop)
        self.addCleanup(self.temp.cleanup)
        self.request = {"model": "test:model", "messages": [{"role": "user", "content": "가상 입력"}]}
        self.dataset = {"cases": [{"case_id": "Q", "input": {"data_notice": "가상 자료", "value": 1}}]}
        for eid in ("selected", "excluded"):
            save_experiment(eid, {"models": {}}, self.dataset, self.source)
        self.attempt = begin_attempt("selected", "slot/1", "Q", "test:model", 1, self.request, db_path=self.source)
        self.finish("공개 원문")
        # 선택한 평가 표 외의 개인 계좌 표가 결과에 들어가지 않는 검사용 자료
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("CREATE TABLE private_accounts (content TEXT)")
            connection.execute("INSERT INTO private_accounts VALUES ('private fixture')")

    def finish(self, content):
        finish_attempt(self.attempt, {"run_id": "call", "started_at": "2026-09-15T00:00:00+00:00",
                                     "request": self.request, "status": "completed", "content": content,
                                     "raw_lines": ["원본 스트림"], "final_response_complete": True},
                       {"final_completed": True}, self.source)

    def test_selected_results_roundtrip_without_changing_source(self):
        """
        선택한 실험과 원본 및 검토의 내보내기, 원본 DB의 변경 없음 확인
        """
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = export_experiments(["selected"], self.output, self.source)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(load_experiment("selected", self.output), load_experiment("selected", self.source))
        self.assertEqual(load_attempts("selected", self.output), load_attempts("selected", self.source))
        with closing(sqlite3.connect(self.output)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM evaluation_experiments").fetchone()[0], 1)
            names = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("private_accounts", names)
            self.assertIn("evaluation_export_manifest", names)
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_existing_destination_and_source_overwrite_rejected(self):
        """
        이미 존재하는 목적지와 원본 경로의 덮어쓰기 거부 확인
        """
        self.output.write_text("기존 파일", encoding="utf-8")
        for dest in (self.output, self.source):
            with self.assertRaises(ValueError):
                export_experiments(["selected"], dest, self.source)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "기존 파일")

    def test_running_attempt_is_not_exported_as_finished(self):
        """
        실행 중인 시도를 완료 자료로 내보내는 동작의 차단 확인
        """
        begin_attempt("selected", "slot/2", "Q", "test:model", 2, self.request, db_path=self.source)
        with self.assertRaises(ValueError):
            export_experiments(["selected"], self.output, self.source)
        self.assertFalse(self.output.exists())

    def test_unselected_running_attempt_does_not_block_export(self):
        """
        선택 범위 밖의 실행 중 시도가 선택한 완료 실험의 복사를 막지 않는 처리 확인
        """
        begin_attempt("excluded", "slot/1", "Q", "test:model", 1, self.request, db_path=self.source)
        self.assertEqual(export_experiments(["selected"], self.output, self.source)["attempts"], 1)

    def test_secret_in_model_error_is_rejected_without_exposure(self):
        """
        오류 원문에 포함된 인증값의 내보내기 차단과 예외 메시지 노출 방지 확인
        """
        with closing(sqlite3.connect(self.source)) as connection, connection:
            raw = json.loads(connection.execute("SELECT record_json FROM evaluation_calls").fetchone()[0])
            raw["errors"] = [{"detail": "example-private-value-123456"}]
            connection.execute("UPDATE evaluation_calls SET record_json=?", (json.dumps(raw),))
        with self.assertRaises(ValueError) as caught:
            export_experiments(["selected"], self.output, self.source)
        self.assertNotIn("example-private-value-123456", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_real_data_and_corrupted_hash_are_rejected(self):
        """
        가상 자료 표시가 없는 원본과 해시가 손상된 실험의 내보내기 거부 확인
        """
        save_experiment("real", {}, {"cases": [{"input": {"data_notice": "실제 계좌"}}]}, self.source)
        with self.assertRaises(ValueError):
            export_experiments(["real"], self.output, self.source)
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("UPDATE evaluation_experiments SET dataset_json='{}' WHERE experiment_id='selected'")
        with self.assertRaises(ValueError):
            export_experiments(["selected"], self.output, self.source)
        self.assertFalse(self.output.exists())

    def test_english_synthetic_notice_is_accepted(self):
        """
        영문 Synthetic 표시가 있는 가상 사례의 정상 내보내기 확인
        """
        dataset = {"cases": [{"input": {"data_notice": "Synthetic data for a PoC."}}]}
        save_experiment("english", {}, dataset, self.source)
        result = export_experiments(["english"], self.output, self.source)
        self.assertEqual(result["experiments"], 1)
        self.assertEqual(load_experiment("english", self.output)["dataset"], dataset)

    def test_missing_and_duplicate_ids_rejected(self):
        """
        존재하지 않거나 중복된 실험 ID의 거부 확인
        """
        for ids in ([], ["selected", "selected"], ["missing"]):
            with self.assertRaises(ValueError):
                export_experiments(ids, self.output, self.source)


if __name__ == "__main__":
    unittest.main()
