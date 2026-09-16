"""
PoC poc 코드의 자동 검사

검사 방식:
    - 가상 입력과 임시 DB 또는 대체 응답 사용
    - 정상 사례와 실패 및 경계 사례의 결과 대조
    - 외부 모델 및 유료 API 호출 없음

용도: 실행과 저장 코드의 오류 확인, 실제 모델 품질 평가와 별도 구분
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import evaluation_poc as poc
from .evaluation_local import build_ollama_request
from .evaluation_storage import load_attempts, load_experiment


class PocExecutionTests(unittest.TestCase):
    """실제 모델을 호출하지 않는 요청, 고정 자료와 저장 중복 방지 검사"""


    def test_selfcheck_keeps_reference_input_and_single_call_scope(self):
        """
        본 비교 자료와 설정을 보존한 Q06 단일 재실행 계획, 실행 중 부모 계획의 거부 확인
        """
        cases = poc.load_cases()["cases"]
        reference = next(c for c in cases if c["case_id"]=="Q06" and c["language"]=="en")
        original = {"config_hash":"original-config", "dataset_hash":"original-data",
                    "dataset":{"cases":cases}, "config":{
                        "evaluation_kind":"poc_main", "models":{"gemma4:12b":{"digest":"fixed", "num_ctx":8192}},
                        "schedule":[{"slot_id":"one"}], "source_hashes":{"modules/evaluation_poc.py":"old"},
                        "source_contents":{}, "instructions":{"en":"Original instruction"}}}
        before = copy.deepcopy(original)
        with patch.object(poc,"load_experiment",return_value=original), \
             patch.object(poc,"load_attempts",return_value=[{"status":"completed"}]), \
             patch.object(poc,"environment_snapshot",return_value={"mock":True}), \
             patch.object(poc,"save_experiment") as saved:
            poc.prepare_selfcheck("original")
        args = saved.call_args.args
        self.assertEqual(len(args[1]["schedule"]),1)
        self.assertEqual(args[1]["schedule"][0]["phase"],"extension")
        self.assertEqual(args[2]["cases"][0],reference)
        self.assertEqual(args[1]["models"]["gemma4:12b"]["digest"],"fixed")
        self.assertEqual(args[1]["instructions"]["en"],"Original instruction")
        self.assertEqual(original,before)
        with patch.object(poc,"load_experiment",return_value=original), \
             patch.object(poc,"load_attempts",return_value=[{"status":"running"}]):
            with self.assertRaises(ValueError):
                poc.prepare_selfcheck("original")

    def test_completion_check_preserves_case_and_declares_setting_sources(self):
        """대표 문항과 요청 원문의 보존, 모델별 공식 설정과 자체 한도의 구분 확인"""
        def info(path="/api/tags", body=None):
            if path == "/api/tags":
                return {"models": [{"name": m, "digest": m} for m in poc.MODELS]}
            return {"version": "mock"}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(poc, "get_ollama_info", side_effect=info), \
             patch.object(poc, "environment_snapshot", return_value={}):
            db = str(Path(folder) / "check.sqlite")
            baseline_id = poc.prepare("main", db)
            baseline = load_experiment(baseline_id, db)
            eid = poc.prepare_completion_check(baseline_id, db)
            plan = load_experiment(eid, db)
            self.assertEqual(len(plan["config"]["schedule"]), 2)
            self.assertEqual([c["case_id"] for c in plan["dataset"]["cases"]], ["Q08"])
            self.assertEqual(plan["config"]["acceptance"], {})
            for slot in plan["config"]["schedule"]:
                old = poc.build_request(baseline, slot)
                new = poc.build_request(plan, slot)
                self.assertEqual(old["messages"], new["messages"])
                self.assertEqual(old["format"], new["format"])
                self.assertTrue(new["think"])
                self.assertEqual(new["options"]["temperature"], 1.0)
            q = plan["config"]["models"]["qwen3.5:9b"]
            g = plan["config"]["models"]["gemma4:12b"]
            self.assertEqual((q["num_ctx"], q["num_predict"]), (131072, 32768))
            self.assertEqual((q["options"]["top_k"], q["options"]["presence_penalty"]), (20, 1.5))
            self.assertEqual(g["options"]["top_k"], 64)
            self.assertIn("operator_limits", plan["config"]["settings_provenance"]["gemma"])
            self.assertEqual(load_experiment(baseline_id, db), baseline)
            # 실제 모델 대신 가상 실패 응답을 사용한 시도 등록과 저장 경로 검사
            from uuid import uuid4
            with patch.object(poc, "call_ollama", side_effect=lambda *args, **kwargs: {
                    "run_id": str(uuid4()), "status": "failed", "content": "", "thinking": "",
                    "started_at": "2026-09-16T00:00:00Z",
                    "request": build_ollama_request(*args, **{k:v for k,v in kwargs.items() if k != "timeout"}),
                    "errors": [{"type": "mock", "detail": "연결 실패 대체 응답"}],
                    "final_response_complete": False}):
                self.assertEqual(poc.run(eid, db), 2)
            self.assertTrue(all(a["phase"] == "preflight" for a in load_attempts(eid, db)))
            # 실패한 대표 기록의 확대 실행 차단과 성공 설정의 20개 요청 보존 검사
            with self.assertRaises(ValueError):
                poc.prepare_recommended_main(eid, db)
            successes = [{"model": m, "status": "completed",
                          "record": {"content": "{}", "response_metadata": {"done_reason": "stop"}},
                          "evaluation": {"assessment": {"schema_valid": True}}} for m in poc.MODELS]
            with patch.object(poc, "load_attempts", return_value=successes):
                main_id = poc.prepare_recommended_main(eid, db)
            main = load_experiment(main_id, db)
            self.assertEqual(len(main["config"]["schedule"]), 20)
            self.assertEqual(len(main["dataset"]["cases"]), 10)
            self.assertEqual(main["config"]["models"], plan["config"]["models"])
            self.assertEqual(load_attempts(main_id, db), [])
            for slot in main["config"]["schedule"]:
                self.assertEqual(slot["repeat"], 1)
                self.assertEqual(slot["phase"], "main")
                before = poc.build_request(baseline, slot)
                after = poc.build_request(main, slot)
                self.assertEqual(before["messages"], after["messages"])
                self.assertEqual(before["format"], after["format"])
                if slot["case_id"] == "Q08":
                    self.assertEqual(after, poc.build_request(plan, slot))


    def test_best_practices_preserves_cases_and_paired_limits(self):
        """
        모드별 공식 설정과 입력 보존, 두 모드의 동일 길이 한도 확인

        검사: 실제 모델 호출 없이 임시 DB의 계획과 요청 원문 대조
        목적: 정답 노출과 문항 변경, 서로 다른 생성 한도의 혼입 방지
        """
        def info(path="/api/tags", body=None):
            if path == "/api/tags":
                return {"models": [{"name": m, "digest": m} for m in poc.MODELS]}
            return {"version": "mock"}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(poc, "get_ollama_info", side_effect=info), \
             patch.object(poc, "environment_snapshot", return_value={"mock": True}):
            db = str(Path(folder)/"best-practices.sqlite")
            baseline_id = poc.prepare("main", db)
            baseline = load_experiment(baseline_id, db)
            representative = poc.prepare_completion_check(baseline_id, db)
            plans = [load_experiment(poc.prepare_best_practices(
                representative, thinking=t, db_path=db), db) for t in (False, True)]
            for plan in plans:
                self.assertEqual(len(plan["config"]["schedule"]), 20)
                for slot in plan["config"]["schedule"]:
                    req = poc.build_request(plan, slot)
                    original = poc.build_request(baseline, slot)
                    self.assertEqual(req["messages"], original["messages"])
                    self.assertEqual(req["format"], original["format"])
                    self.assertFalse(req["truncate"])
                    self.assertFalse(req["shift"])
            for name in poc.MODELS:
                off, on = [p["config"]["models"][name] for p in plans]
                for key in ("digest", "num_ctx", "num_predict", "timeout"):
                    self.assertEqual(off[key], on[key])
                self.assertFalse(off["thinking"])
                self.assertTrue(on["thinking"])
            self.assertEqual(plans[0]["config"]["models"][poc.MODELS[0]]["temperature"], 0.7)
            self.assertEqual(plans[0]["config"]["models"][poc.MODELS[0]]["options"]["top_p"], 0.8)
            self.assertEqual(plans[1]["config"]["models"][poc.MODELS[0]]["temperature"], 1.0)
            warm = load_experiment(poc.prepare_best_practices(
                representative, thinking=True, warmup=True, db_path=db), db)
            self.assertEqual(len(warm["config"]["schedule"]), 2)
            self.assertTrue(all(s["phase"] == "warmup" for s in warm["config"]["schedule"]))

    def test_generation_limit_changes_only_declared_request_fields(self):
        """
        생성 길이별 계획의 동일 문항과 요청 보존, 문맥 초과의 사전 거부 확인

        검증 대상: 영문 10문항 1회, 추론 여부와 생성 길이 이외 설정의 불변성
        검사 방식: 임시 SQLite와 가상 환경 정보, 실제 모델 호출 없음
        """
        def info(path="/api/tags", body=None):
            if path == "/api/tags":
                return {"models": [{"name": m, "digest": m} for m in poc.MODELS]}
            return {"version": "mock"}
        observed = [{"model": m, "record": {"language": "en",
                     "response_metadata": {"prompt_eval_count": 1669}}} for m in poc.MODELS]
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(poc, "get_ollama_info", side_effect=info), \
             patch.object(poc, "environment_snapshot", return_value={"mock": True}):
            db = str(Path(folder) / "mode.sqlite")
            baseline_id = poc.prepare("main", db)
            baseline = load_experiment(baseline_id, db)
            with patch.object(poc, "load_attempts", return_value=observed):
                eid = poc.prepare_mode(baseline_id, num_predict=4096, repeat_count=1, db_path=db)
                with self.assertRaises(ValueError):
                    poc.prepare_mode(baseline_id, num_predict=8192, repeat_count=1, db_path=db)
            proposed = load_experiment(eid, db)
            self.assertEqual(len(proposed["config"]["schedule"]), 20)
            self.assertEqual(len(proposed["dataset"]["cases"]), 10)
            self.assertTrue(all(s["repeat"] == 1 for s in proposed["config"]["schedule"]))
            for slot in proposed["config"]["schedule"]:
                expected = poc.build_request(baseline, slot)
                expected["think"] = True
                expected["options"]["num_predict"] = 4096
                self.assertEqual(expected, poc.build_request(proposed, slot))
            self.assertEqual(load_experiment(baseline_id, db), baseline)
            self.assertEqual(proposed["config"]["models"][poc.MODELS[0]]["timeout"], 300)

    def test_common_options_and_no_silent_truncation(self):
        """
        공통 샘플링과 문맥 및 생성 한도 보존, 추가 옵션 우회와 비정상 값의 거부 확인
        """
        request = build_ollama_request(
            "model", [], extra_options=poc.OPTIONS, num_ctx=8192, num_predict=2048)
        self.assertEqual(request["options"]["presence_penalty"], 0)
        self.assertEqual(request["options"]["repeat_penalty"], 1)
        self.assertEqual(request["options"]["num_ctx"], 8192)
        self.assertFalse(request["truncate"])
        self.assertFalse(request["shift"])
        with self.assertRaises(ValueError):
            build_ollama_request("model", [], extra_options={"num_predict": 9999})
        for invalid in ({"top_k": True}, {"top_p": 0}, {"repeat_penalty": float("nan")}):
            with self.assertRaises(ValueError):
                build_ollama_request("model", [], extra_options=invalid)

    def test_warmup_has_no_korean_in_model_input(self):
        """
        영문 워밍업 입력에 한글 잔여 문장 없음, 가용 잔고에 맞는 기대 결정 확인
        """
        import re
        case = next(c for c in poc.load_cases()["cases"] if c["case_id"] == "W00")
        self.assertIsNone(re.search("[가-힣]", json.dumps(case["input"], ensure_ascii=False)))
        self.assertEqual(case["expected"]["allowed_decisions"], ["sell", "hold"])


    def test_translation_preserves_all_facts_and_expected_answers(self):
        """
        언어 조건 간 수치와 사실, 별도 정답 및 채점 기준의 동일성 확인
        """
        import re
        cases = poc.load_cases()["cases"]
        english = {c["case_id"]: c for c in cases if c["language"] == "en"}
        self.assertEqual(set(english), {"W00"} | {f"Q{i:02}" for i in range(1, 11)})
        for c in english.values():
            self.assertIsNone(re.search("[가-힣]", json.dumps(c["input"], ensure_ascii=False)))
        korean = [c for c in cases if c["language"] == "ko"]
        self.assertEqual({c["case_id"] for c in korean}, {"Q03", "Q06", "Q10"})
        def facts(value):
            """
            언어에 따라 달라지는 설명만 제외한 수치와 사실 비교 자료 생성
            """
            value = copy.deepcopy(value)
            value.pop("data_notice")
            value.pop("required_observations")
            value["trading_rules"].pop("buy_allocation_basis")
            value["trading_rules"].pop("sell_allocation_basis")
            for decision in value["strategy_state"]["previous_decisions"]:
                decision.pop("reason", None)
            return value
        for c in korean:
            en = english[c["case_id"]]
            self.assertEqual(facts(c["input"]), facts(en["input"]))
            self.assertEqual(c["expected"], en["expected"])
            self.assertEqual(c["rubric"], en["rubric"])

    def test_independent_arithmetic_and_time_oracles(self):
        """
        거래량 증가율, 수수료와 손익, 시간대 및 잘못된 과거 비교의 독립 검산
        """
        from decimal import Decimal as D
        from datetime import datetime, timezone
        cases = {c["case_id"]: c["input"] for c in poc.load_cases()["cases"] if c["language"] == "en"}
        days = cases["Q03"]["market_input"]["ohlcv"]["day"]
        before, after = D(str(days[0]["volume"])), D(str(days[1]["volume"]))
        self.assertEqual(after-before, D(100))
        self.assertEqual((after-before)/before*100, D(50))
        c = cases["Q04"]
        self.assertEqual(D(str(c["trading_rules"]["minimum_order_krw"])) *
                         (1+D(str(c["trading_rules"]["fee_rate"]))), D("5002.5"))
        for name, wanted in (("Q08", D(4750)), ("Q10", D(-10250))):
            state = cases[name]["strategy_state"]; fill = state["executions"][0]
            valuation = state["portfolio_valuation"]
            gross = D(str(fill["btc"])) * (D(str(valuation["mark_price_krw"])) - D(str(fill["price_krw"])))
            self.assertEqual(gross, D(str(valuation["unrealized_pnl_before_fees_krw"])))
            self.assertEqual(gross-D(str(fill["fee_krw"])), wanted)
            self.assertEqual(D(str(state["cash_available_krw"])) +
                             D(str(state["btc_available"]))*D(str(valuation["mark_price_krw"])),
                             D(str(valuation["equity_krw"])))
        c = cases["Q05"]["market_input"]
        close = datetime.fromisoformat(c["ohlcv"]["minute5"][0]["candle_close_time"])
        quote = datetime.fromisoformat(c["orderbook"]["observed_at"])
        self.assertEqual(close.astimezone(timezone.utc).isoformat(), "2026-09-15T00:55:00+00:00")
        self.assertEqual(quote.astimezone(timezone.utc).isoformat(), "2026-09-15T00:59:59+00:00")
        observations = cases["Q09"]["strategy_state"]["price_observations"]
        self.assertEqual((D(str(observations[1]["price_krw"])) /
                          D(str(observations[0]["price_krw"]))-1)*100, D(2))
        original = cases["Q10"]["strategy_state"]["previous_decisions"][0]
        self.assertLess(original["source_data"]["day_close"], original["source_data"]["day_ema"])
        self.assertIn("above", original["reason"])  # 정정 대상인 원본의 잘못된 주장을 그대로 보존

    def test_request_record_and_completed_slot_are_preserved(self):
        """
        예정 요청과 실제 기록 일치, 완료 위치의 재호출 방지 및 채점 예외 시 원본 보존 확인
        """
        def info(path="/api/tags", body=None):
            """
            시험에 사용할 설치 모델과 서버 상태의 가상 반환
            """
            if path == "/api/tags":
                return {"models": [{"name": m, "digest": m} for m in poc.MODELS]}
            if path == "/api/version":
                return {"version": "mock"}
            return {"models": []}

        def call(model, messages, **options):
            """
            계획된 요청과 동일한 가상 응답 기록 생성
            """
            from uuid import uuid4
            request = build_ollama_request(
                model, messages, num_ctx=options["num_ctx"],
                num_predict=options["num_predict"], thinking=options["thinking"],
                output_format=options["output_format"], temperature=options["temperature"],
                extra_options=options["extra_options"])
            return {"run_id": str(uuid4()), "started_at": "2026-09-15T00:00:00Z",
                    "request": request, "status": "completed",
                    "content": json.dumps({"decision": "hold", "buy_allocation_percentage": 0,
                                          "sell_allocation_percentage": 0,
                                          "reason": "Mock response.", "reflection_log": "No history."}),
                    "final_response_complete": True, "errors": []}

        with tempfile.TemporaryDirectory() as folder, \
             patch.object(poc, "get_ollama_info", side_effect=info), \
             patch.object(poc, "environment_snapshot", return_value={"mock": True}), \
             patch.object(poc, "call_ollama", side_effect=call) as mocked:
            db = str(Path(folder) / "test.sqlite")
            eid = poc.prepare("warmup", db)
            experiment = load_experiment(eid, db)
            for slot in experiment["config"]["schedule"]:
                request = poc.build_request(experiment, slot)
                self.assertNotIn("allowed_decisions", request["messages"][1]["content"])
                self.assertEqual(request["options"]["presence_penalty"], 0)
            self.assertEqual(poc.run(eid, db), 2)
            self.assertEqual(poc.run(eid, db), 0)
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(len(load_attempts(eid, db)), 2)
            main_id = poc.prepare("main", db)
            main = load_experiment(main_id, db)
            schedule = main["config"]["schedule"]
            self.assertEqual(len(schedule), 52)
            self.assertEqual(sum(s["language"] == "en" for s in schedule), 40)
            self.assertEqual(sum(s["language"] == "ko" for s in schedule), 12)
            # 의미 검사 실패가 원본 저장이나 다음 재조회까지 막지 않는지 확인
            with patch.object(poc, "evaluate_case_response", side_effect=ValueError("mock grader error")):
                self.assertEqual(poc.run(main_id, db, limit=1), 1)
            saved = load_attempts(main_id, db)[0]
            self.assertEqual(saved["evaluation"]["grading_error"], "mock grader error")
            self.assertIn("Mock response.", saved["record"]["content"])


if __name__ == "__main__":
    unittest.main()
