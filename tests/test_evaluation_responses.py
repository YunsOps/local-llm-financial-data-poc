"""
응답 형식과 거래 제한의 자동 검사

검사 방식:
    - 가상 입력과 임시 DB 또는 대체 응답 사용
    - 정상 사례와 실패 및 경계 사례의 결과 대조
    - 외부 모델 및 유료 API 호출 없음

용도: 실행과 저장 코드의 오류 확인, 실제 모델 품질 평가와 별도 구분
"""

import copy
import json
import unittest

from modules.evaluation_poc import load_cases
from modules.evaluate_response import evaluate_case_response


class ResponseTests(unittest.TestCase):
    """가상 원본의 출력 형식과 주문 경계 검사, 실제 API 호출 없음"""

    def setUp(self):
        """
        각 검사에 독립적으로 사용할 가상 입력과 임시 기록의 준비
        """
        self.cases = {c["case_id"]: c for c in load_cases()["cases"] if c["language"] == "en"}

    def check(self, case_id, **changes):
        """동일 원본에 명시적으로 바꾼 응답만 적용하는 검사 보조 함수"""
        response = {"decision": "hold", "buy_allocation_percentage": 0,
                    "sell_allocation_percentage": 0, "reason": "Evidence.",
                    "reflection_log": "Check supplied history."}
        response.update(changes)
        return evaluate_case_response(self.cases[case_id], json.dumps(response), "fixture")

    def test_hold_has_zero_allocation(self):
        """
        관망 결정에서 매수와 매도 비중이 모두 0인 조건의 확인
        """
        self.assertTrue(self.check("Q01")["trading_valid"])
        self.assertFalse(self.check("Q01", buy_allocation_percentage=1)["trading_valid"])

    def test_missing_quote_cannot_be_replaced_by_candle(self):
        """
        호가 누락 사례에서 봉 종가를 대신 사용한 매도 허용 방지 확인
        """
        result = self.check("Q06", decision="sell", sell_allocation_percentage=100)
        self.assertFalse(result["trading_valid"])

    def test_minimum_buy_includes_fee(self):
        # 5,002.5원은 수수료 0.05% 포함 최소 5,000원 매수의 정확한 경계
        """
        수수료 포함 최소 매수 금액의 정확한 경계와 경계 미만 거부 확인
        """
        case = copy.deepcopy(self.cases["Q01"])
        case["expected"]["allowed_decisions"] = ["buy", "hold"]
        case["input"]["strategy_state"]["cash_available_krw"] = 5002.5
        case["input"]["trading_rules"].update(minimum_order_krw=5000, fee_rate=0.0005)
        raw = '{"decision":"buy","buy_allocation_percentage":100,"sell_allocation_percentage":0,"reason":"Boundary.","reflection_log":"No fills."}'
        self.assertTrue(evaluate_case_response(case, raw, "fixture")["trading_valid"])
        case["input"]["strategy_state"]["cash_available_krw"] = 5002.49
        self.assertFalse(evaluate_case_response(case, raw, "fixture")["trading_valid"])

    def test_invalid_json_and_duplicate_keys_are_not_repaired(self):
        """
        빈 응답, 외부 문장, 중복 키와 비정상 숫자의 원문 거부 확인
        """
        for raw in ("", "text outside {}", '{"decision":"hold","decision":"buy"}', '{"x":NaN}'):
            with self.subTest(raw=raw):
                self.assertFalse(evaluate_case_response(self.cases["Q01"], raw, "fixture")["json_valid"])

    def test_invalid_types_and_extra_fields_are_rejected(self):
        """
        bool 비중, 추가 필드와 공백 설명의 형식 통과 방지 확인
        """
        self.assertFalse(self.check("Q01", buy_allocation_percentage=True)["schema_valid"])
        self.assertFalse(self.check("Q01", extra="unexpected")["schema_valid"])
        self.assertFalse(self.check("Q01", reason=" ")["schema_valid"])


if __name__ == "__main__":
    unittest.main()
