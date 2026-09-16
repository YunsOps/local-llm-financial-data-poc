"""
PoC cloud 코드의 자동 검사

검사 방식:
    - 가상 입력과 임시 DB 또는 대체 응답 사용
    - 정상 사례와 실패 및 경계 사례의 결과 대조
    - 외부 모델 및 유료 API 호출 없음

용도: 실행과 저장 코드의 오류 확인, 실제 모델 품질 평가와 별도 구분
"""

import unittest
from unittest.mock import patch

from . import evaluation_cloud as cloud
from .validate_evaluation_local import _Context


class CloudEvaluationTests(unittest.TestCase):
    """유료 API 호출 없는 요청, 비용, 중단과 원본 보존 검사"""

    def usage(self):
        return {"input_tokens": 1000, "output_tokens": 100,
                "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300},
                "output_tokens_details": {"reasoning_tokens": 20}, "total_tokens": 1100}

    def invoke(self, events, timeout=1):
        """
        준비한 이벤트로 호출 수신과 종료 경로 실행, 실제 모델 호출 없음
        """
        context = _Context(events)
        with patch.object(cloud, "luna_key_available", return_value=True), \
             patch.object(cloud.multiprocessing, "get_context", return_value=context):
            result = cloud.call_luna([{"role": "user", "content": "가상 입력"}], timeout=timeout)
        self.assertFalse(context.process.is_alive())
        return result

    def final_event(self, status="completed", text="{}"):
        response = {"id": "test-response", "model": cloud.LUNA_MODEL, "status": status,
                    "usage": self.usage(), "service_tier": "default",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        return ("event", {"type": "response." + status, "response": response})

    def test_request_and_options(self):
        """
        동일 대화와 출력 형식의 전달, 잘못된 생성 한도 및 추론 설정의 거부 확인
        """
        request = cloud.build_luna_request([{"role": "user", "content": "원문"}])
        self.assertEqual(request["model"], "gpt-5.6-luna")
        self.assertEqual(request["truncation"], "disabled")
        self.assertIs(request["store"], False)
        self.assertNotIn("tools", request)
        self.assertNotIn("temperature", cloud.build_luna_request([], reasoning_effort="low"))
        with self.assertRaises(ValueError):
            cloud.build_luna_request([], output_format="unknown")

    def test_cache_and_reasoning_cost_without_double_count(self):
        """
        캐시 읽기와 쓰기 단가 적용, 출력에 포함된 추론 토큰의 중복 과금 방지 확인
        """
        self.assertEqual(cloud.calculate_luna_cost(self.usage())["usd"], "0.000299")
        usage = self.usage()
        usage["output_tokens_details"]["reasoning_tokens"] = 50
        self.assertEqual(cloud.calculate_luna_cost(usage)["usd"], "0.000299")

    def test_long_input_pricing(self):
        """
        긴 입력 구간의 입력 및 출력 배수 적용과 비용 계산 경계 확인
        """
        usage = {"input_tokens": 300000, "output_tokens": 1000,
                 "input_tokens_details": {"cached_tokens": 100000, "cache_write_tokens": 0}}
        result = cloud.calculate_luna_cost(usage)
        self.assertEqual(result["usd"], "0.0858")
        self.assertTrue(result["long_input_pricing"])

    def test_missing_usage_is_not_zero_cost(self):
        """
        사용량 누락이나 비표준 서비스 등급에서 비용 None 유지 확인
        """
        self.assertIsNone(cloud.calculate_luna_cost(None)["usd"])
        usage = self.usage()
        del usage["input_tokens_details"]["cache_write_tokens"]
        self.assertIsNone(cloud.calculate_luna_cost(usage)["usd"])
        self.assertIsNone(cloud.calculate_luna_cost(self.usage(), service_tier="priority")["usd"])

    def test_completed_event_and_usage_preserved(self):
        """
        완료 이벤트와 사용량의 원본 보존, 완전한 최종 응답 표시 확인
        """
        result = self.invoke([("event", {"type": "response.output_text.delta", "delta": "{}"}),
                              self.final_event()])
        self.assertTrue(result["final_response_complete"])
        self.assertEqual(result["usage"], self.usage())
        self.assertEqual(result["cost"]["usd"], "0.000299")
        self.assertEqual(result["sdk_max_retries"], 0)
        self.assertEqual(len(result["raw_lines"]), 2)

    def test_final_text_without_delta_is_preserved(self):
        """
        중간 텍스트 조각 없이 최종 이벤트에만 있는 응답의 보존 확인
        """
        result = self.invoke([self.final_event(text="최종 원문")])
        self.assertEqual(result["content"], "최종 원문")
        self.assertTrue(result["final_response_complete"])
        self.assertIsNone(result["first_content_seconds"])

    def test_generation_limit_is_not_complete(self):
        """
        생성 한도 도달과 정상 완료의 구분, 미완료 응답의 성공 집계 방지 확인
        """
        result = self.invoke([self.final_event("incomplete", text="{")])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["final_response_complete"])
        self.assertEqual(result["response_metadata"]["done_reason"], "max_output_tokens")

    def test_deadline_and_missing_key(self):
        """
        전체 시간 제한 적용과 키 미설정 시 유료 호출 전 중단 확인
        """
        result = self.invoke([], timeout=0.02)
        self.assertEqual(result["status"], "timeout")
        self.assertIsNone(result["cost"]["usd"])
        with patch.object(cloud, "luna_key_available", return_value=False):
            with self.assertRaises(ValueError):
                cloud.call_luna([])


    def test_poc_preserves_local_inputs_and_prevents_paid_retries(self):
        """
        로컬의 고정 다섯 문항 복사, 요청 동일성 및 등록된 Cloud 시도의 재호출 방지 확인
        """
        import json
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from .evaluation_poc import load_cases
        from .evaluation_prompt import get_response_schema
        from .evaluation_storage import save_experiment, load_experiment, load_attempts
        from .data_evaluation_cases import _to_json

        source = load_cases()
        config = {"evaluation_kind": "poc_main", "cloud_case_ids": source["cloud_case_ids"],
                  "instructions": source["instructions"], "response_schema": get_response_schema(),
                  "scoring_policy": source["scoring_policy"]}
        def call(messages, **kwargs):
            """
            계획된 요청과 동일한 가상 응답 기록 생성
            """
            request = cloud.build_luna_request(messages,
                max_output_tokens=kwargs["max_output_tokens"], reasoning_effort=kwargs["reasoning_effort"],
                output_format=kwargs["output_format"], temperature=kwargs["temperature"])
            return {"run_id": str(uuid4()), "started_at": "2026-09-15T00:00:00Z",
                    "request": request, "status": "completed", "final_response_complete": True,
                    "wall_seconds": 0.1, "cost": {"usd": None}, "errors": [],
                    "content": json.dumps({"decision": "hold", "buy_allocation_percentage": 0,
                                          "sell_allocation_percentage": 0, "reason": "Mock.",
                                          "reflection_log": "Mock."})}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(cloud, "luna_key_available", return_value=True), \
             patch.object(cloud, "call_luna", side_effect=call) as mocked:
            db = str(Path(folder) / "poc.sqlite")
            save_experiment("local", config, {"cases": source["cases"]}, db)
            eid = cloud.prepare_poc_cloud("local", db)
            plan = load_experiment(eid, db)
            self.assertEqual([c["case_id"] for c in plan["dataset"]["cases"]], source["cloud_case_ids"])
            self.assertEqual(cloud.run_poc_cloud(eid, db), 5)
            self.assertEqual(cloud.run_poc_cloud(eid, db), 0)
            self.assertEqual(mocked.call_count, 5)
            for a in load_attempts(eid, db):
                case = next(c for c in source["cases"] if c["case_id"] == a["case_id"] and c["language"] == "en")
                self.assertEqual(a["request"]["input"][1]["content"], _to_json(case["input"]))
                self.assertNotIn("allowed_decisions", a["request"]["input"][1]["content"])
                self.assertNotIn("seed", a["request"])


if __name__ == "__main__":
    unittest.main()
