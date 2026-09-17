"""
검토 인용, 판정 근거와 원본 연결의 자동 검사

검사 방식:
    - 가상 입력과 임시 DB 또는 대체 응답 사용
    - 정상 사례와 실패 및 경계 사례의 결과 대조
    - 외부 모델 및 유료 API 호출 없음

용도: 실행과 저장 코드의 오류 확인, 실제 모델 품질 평가와 별도 구분
"""

import copy
import unittest
from unittest.mock import patch

from modules.evaluation_poc import load_cases
from modules.evaluation_review import record_required_review, reuse_identical_review, record_thinking_review


class ReviewTests(unittest.TestCase):
    """실제 인용과 언어별 원본 연결 검사, 의미 판정을 자동 생성하지 않는 구성"""

    def setUp(self):
        """
        각 검사에 독립적으로 사용할 가상 입력과 임시 기록의 준비
        """
        self.case = next(c for c in load_cases()["cases"] if c["case_id"] == "Q03" and c["language"] == "en")
        self.experiment = {"dataset": {"cases": [self.case]}, "dataset_hash": "fixture"}
        self.attempt = {"attempt_id": "one", "case_id": "Q03", "model": "m",
                        "record": {"content": "Actual response.", "language": "en"}}
        self.items = [("pass", "Actual response.", "Source comparison.")] * 3

    def record(self, items):
        """
        검사용 원본 또는 검토 객체 생성과 저장 결과 조회 보조
        """
        with patch("modules.evaluation_review.load_experiment", return_value=self.experiment), \
             patch("modules.evaluation_review.load_attempts", return_value=[self.attempt]), \
             patch("modules.evaluation_review.save_review") as save:
            record_required_review("test", "one", items)
            return save.call_args.args[2]

    def test_thinking_review_is_separate_from_final_score(self):
        """
        추론 원문 인용 확인과 최종 답변 점수 분리, 다른 필드 인용의 거부 확인
        """
        self.attempt["record"]["thinking"] = "Compare the candle again. Compare the candle again."
        findings = [{"type": "repetition", "quote": "Compare the candle again.",
                     "source_path": None, "note": "Repeated sentence observed.", "resolved_in_final": None}]
        with patch("modules.evaluation_review.load_experiment", return_value=self.experiment), \
             patch("modules.evaluation_review.load_attempts", return_value=[self.attempt]), \
             patch("modules.evaluation_review.save_review") as save:
            record_thinking_review("test", "one", findings)
            record = save.call_args.args[2]
            self.assertEqual(record["method"], "source_grounded_thinking")
            self.assertNotIn("required_items", record)
            self.assertIn("thinking_hash", record)
            findings[0]["quote"] = "Actual response."
            with self.assertRaises(ValueError):
                record_thinking_review("test", "one", findings)

    def test_review_keeps_source_and_quote(self):
        """
        검토의 입력 해시와 필수 인용 연결 및 세 항목 보존 확인
        """
        result = self.record(self.items)
        self.assertEqual(result["input_hash"], self.case["input_hash"])
        self.assertEqual(len(result["required_items"]), 3)

    def test_invented_or_missing_quote_is_rejected(self):
        """
        원문에 없는 인용과 근거 없는 충족 판정의 저장 거부 확인
        """
        for quote in ("Not in the response.", ""):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                self.record([("pass", quote, "Claimed evidence.")] * 3)

    def test_same_text_different_language_is_not_reused(self):
        """
        응답 문자열이 같더라도 다른 입력 언어의 검토 재사용 차단 확인
        """
        target = copy.deepcopy(self.attempt)
        target["attempt_id"] = "two"
        target["record"]["language"] = "ko"
        with patch("modules.evaluation_review.load_attempts", return_value=[self.attempt, target]):
            with self.assertRaises(ValueError):
                reuse_identical_review("test", "one", "two")


if __name__ == "__main__":
    unittest.main()
