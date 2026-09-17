"""
점수, 시간과 자원 집계 및 보고서의 자동 검사

검사 방식:
    - 가상 입력과 임시 DB 또는 대체 응답 사용
    - 정상 사례와 실패 및 경계 사례의 결과 대조
    - 외부 모델 및 유료 API 호출 없음

용도: 실행과 저장 코드의 오류 확인, 실제 모델 품질 평가와 별도 구분
"""

import unittest
from unittest.mock import patch

from modules.evaluation_report import _distribution, _summarize_group


def _attempt(status="completed", schema=True, cost=None, review=None):
    """실제 모델 호출과 별개인 분모 검사용 최소 기록 구성"""
    return {"status": status, "repeat_number": 1, "case_id": "Q01",
            "record": {"content": "x", "final_response_complete": status == "completed",
                       "wall_seconds": 5, "cost": {"usd": cost},
                       "response_metadata": {"prompt_eval_count": 100, "eval_count": 20}},
            "evaluation": {"api_success": status == "completed",
                           "final_completed": status == "completed",
                           "assessment": {"json_valid": schema, "schema_valid": schema,
                                          "trading_valid": True if schema else None,
                                          "errors": []},
                           "reviews": review or []}}


class ReportTests(unittest.TestCase):
    """미실행과 실패의 분모, 관측 누락, 중복 검토 및 반복 재현성의 검사"""


    def test_poc_language_and_incomplete_quality_denominators(self):
        """
        영문과 한국어의 집계 분리, 미완료와 미검토를 구분한 품질 분모 확인
        """
        from modules.evaluation_report import summarize_poc
        from modules.json_utils import _to_json
        instructions = {"en": "English", "ko": "Korean"}
        cases = [{"case_id": "Q03", "language": lang, "input": {}, "input_hash": lang}
                 for lang in ("en", "ko")]
        schedule = [{"slot_id": key, "model": "m", "language": lang, "case_id": "Q03", "repeat": 2 if key == "b" else 1}
                    for key,lang in (("a","en"),("b","en"),("c","ko"))]
        experiment = {"experiment_id": "poc", "config_hash": "c", "dataset_hash": "d",
                      "config": {"evaluation_kind": "poc_main", "schedule": schedule,
                                 "instructions": instructions,
                                 "models": {"m": {"timeout": 300, "num_predict": 2048, "num_ctx": 8192}}},
                      "dataset": {"cases": cases}}
        reviews = [{"review": {"method": "source_grounded_required_items",
                              "required_items": [{"verdict": "pass"}]*3}}]
        attempts = []
        for slot in schedule:
            a = _attempt(status="failed" if slot["slot_id"]=="b" else "completed",
                         review=reviews if slot["language"]=="en" else [])
            a.update({"slot_id": slot["slot_id"], "model": "m", "case_id": "Q03"})
            a["request"] = {"messages": [{"role": "system", "content": instructions[slot["language"]]},
                                         {"role": "user", "content": _to_json({})}], "truncate": False}
            a["record"]["language"] = slot["language"]
            attempts.append(a)
        with patch("modules.evaluation_report.load_experiment", return_value=experiment), \
             patch("modules.evaluation_report.load_attempts", return_value=attempts):
            result = summarize_poc("poc")
            first = summarize_poc("poc", first_repeat_only=True)
        self.assertEqual(set(first["groups"]), {"m/en"})
        self.assertEqual(first["groups"]["m/en"]["quality"]["denominator"], 3)
        self.assertEqual(first["groups"]["m/en"]["planned"], 1)
        en = result["groups"]["m/en"]; ko = result["groups"]["m/ko"]
        self.assertEqual(en["quality"]["points"], 3)
        self.assertEqual(en["quality"]["denominator"], 6)
        self.assertEqual(en["quality"]["completed_only"]["denominator"], 3)
        self.assertTrue(en["quality"]["final"])
        self.assertEqual(en["english_output_requirement"]["reviewed_calls"], 1)
        self.assertEqual(en["english_output_requirement"]["unassessable_incomplete_calls"], 1)
        self.assertEqual(en["required_item_outcomes"]["incomplete_response"], 3)
        self.assertEqual(ko["quality"]["unreviewed_calls"], 1)
        self.assertFalse(ko["quality"]["final"])
        self.assertEqual(result["paired_language"]["m"]["en"]["planned"], 2)
        self.assertEqual(result["paired_language"]["m"]["ko"]["planned"], 1)
        self.assertTrue(all(x["same_messages_as_plan"] for x in result["input_checks"]))
        # 동일 기록을 사용자 중단으로 바꾸었을 때 모델 품질 실패와 분리되는지 확인
        attempts[1]["status"] = "interrupted"
        with patch("modules.evaluation_report.load_experiment", return_value=experiment), \
             patch("modules.evaluation_report.load_attempts", return_value=attempts):
            interrupted = summarize_poc("poc")["groups"]["m/en"]
        self.assertEqual(interrupted["operator_interrupted"], 1)
        self.assertEqual(interrupted["quality"]["denominator"], 3)
        self.assertFalse(interrupted["quality"]["final"])

    def test_mode_summary_separates_incomplete_and_actual_generation(self):
        """
        모드 비교의 요청 차이 검사와 응답 완료별 분모 확인

        점검 대상: 요청량 초과 생성, 빈 최종 답변, 유효 JSON만의 응답 시간
        검증 방식: 실제 호출이 없는 가상 기록, 미완료 시도의 시간 집계 분리
        """
        from modules.evaluation_report import summarize_modes
        import copy
        config = {"models": {"m": {"timeout": 300, "num_predict": 2048}}}
        first = _attempt()
        first.update(attempt_id="a", slot_id="s1", model="m", request={"think": False, "messages": []})
        first["record"]["language"] = "en"
        second = copy.deepcopy(first)
        second.update(attempt_id="b", slot_id="s2")
        source = [first, second]
        changed = copy.deepcopy(source)
        for a in changed:
            a["request"]["think"] = True
        changed[0]["record"]["response_metadata"]["eval_count"] = 2331
        changed[1]["record"].update(content="", final_response_complete=False, wall_seconds=100)
        changed[1]["evaluation"].update(final_completed=False)
        changed[1]["evaluation"]["assessment"]["schema_valid"] = False
        summary = {"groups": {"m/en": {}}}
        with patch("modules.evaluation_report.load_experiment", return_value={"config": config}), \
             patch("modules.evaluation_report.load_attempts", side_effect=[source, changed]), \
             patch("modules.evaluation_report.summarize_poc", return_value=summary):
            result = summarize_modes("off", "on")
        group = result["groups"]["reasoning/m"]
        self.assertEqual(group["valid_json_wall_seconds"]["n"], 1)
        self.assertEqual(group["valid_json_wall_seconds"]["mean"], 5)
        self.assertEqual(group["other_wall_seconds"]["mean"], 100)
        self.assertEqual(group["observed_generation_above_request"], ["a"])
        self.assertEqual(group["empty_final_content"], 1)
        self.assertTrue(all(c["only_think_changed"] for c in result["actual_request_comparison"]))
        self.assertFalse(result["all_actual_requests_match"])
        # 추론 플래그 외 입력 내용까지 바뀐 요청의 비교 불일치 확인
        changed[1]["request"]["messages"] = ["changed input"]
        with patch("modules.evaluation_report.load_experiment", return_value={"config": config}), \
             patch("modules.evaluation_report.load_attempts", side_effect=[source, changed]), \
             patch("modules.evaluation_report.summarize_poc", return_value=summary):
            result = summarize_modes("off", "on")
        self.assertFalse(result["actual_request_comparison"][1]["only_think_changed"])

    def test_best_practices_report_preserves_history_and_unreviewed(self):
        """
        공식 설정 보고서의 원본 보존과 미검토 표시, 재생성 시 이력 중복 방지 확인
        """
        import tempfile
        from pathlib import Path
        from modules.evaluation_report import write_best_practices_report
        plan = {"config_hash": "c", "dataset_hash": "d", "dataset": {"cases": []},
                "config": {"environment": {}, "settings_provenance": {}, "source_hashes": {},
                           "models": {"m": {"temperature": 1, "num_ctx": 32768,
                           "num_predict": 16384, "timeout": 1800,
                           "options": {"top_p": 0.95, "top_k": 64, "presence_penalty": 0}}}}}
        a = _attempt()
        a.update(model="m", attempt_id="id", request={"original": True})
        a["record"].update(status="completed", thinking="thought", content="raw")
        a["record"]["response_metadata"]["done_reason"] = "stop"
        with tempfile.TemporaryDirectory() as folder, \
             patch("modules.evaluation_report.load_experiment", return_value=plan), \
             patch("modules.evaluation_report.load_attempts", return_value=[a]):
            path = Path(folder)/"report.md"
            path.write_text("ORIGINAL_ARCHIVE", encoding="utf-8")
            for _ in range(2):
                result = write_best_practices_report("off", "on", path)
                self.assertEqual(result["stored_calls"], 2)
                self.assertEqual(result["reviewed_calls"], 0)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count("ORIGINAL_ARCHIVE"), 1)
            self.assertIn("미검토", text)
            self.assertIn("thought", text)
            self.assertIn("raw", text)

    def test_missing_is_not_zero(self):
        """
        누락과 비정상 수치의 표본 제외, 실제 측정값 0의 유효 표본 유지 확인
        """
        self.assertIsNone(_distribution([None, float("nan")])["mean"])
        self.assertEqual(_distribution([0, None])["n"], 1)

    def test_unstarted_not_failure(self):
        """
        계획만 있고 시작하지 않은 위치의 미실행 표시, 실패 분모 편입 방지 확인
        """
        result = _summarize_group([{}, {}], [_attempt()])
        self.assertEqual(result["final_completed"]["denominator"], 1)
        self.assertEqual(result["unstarted"], 1)

    def test_failure_stays_in_denominator(self):
        """
        실패한 시도를 포함한 형식 충족률 분모 확인
        """
        result = _summarize_group([{}, {}], [_attempt(), _attempt("failed", False)])
        self.assertEqual(result["strict_schema"]["rate"], 0.5)

    def test_running_is_not_finished(self):
        """
        실행 중 시도를 종료 수와 확정 실패율에 포함하지 않는 집계 확인
        """
        result = _summarize_group([{}], [_attempt("running", False)])
        self.assertEqual(result["finished"], 0)
        self.assertIsNone(result["strict_schema"]["rate"])

    def test_unparseable_not_trading_pass(self):
        """
        형식 해석이 불가능한 응답의 거래 규칙 통과 처리 방지 확인
        """
        result = _summarize_group([{}], [_attempt(schema=False)])
        self.assertIsNone(result["trading_rules_among_parseable"]["rate"])

    def test_cost_unknown_distinct_from_free(self):
        """
        미확인 비용 None과 실제 비용 0의 구분 확인
        """
        unknown = _summarize_group([{}], [_attempt()])
        zero = _summarize_group([{}], [_attempt(cost="0")])
        self.assertIsNone(unknown["cost_usd"]["sum"])
        self.assertEqual(zero["cost_usd"]["sum"], "0")

    def test_latest_review_only(self):
        """
        같은 방식의 수정 전 검토를 중복 합산하지 않는 집계 확인
        """
        reviews = [{"review": {"method": "required", "required_items": [{"verdict": "fail"}]}},
                   {"review": {"method": "required", "required_items": [{"verdict": "pass"}]}}]
        result = _summarize_group([{}], [_attempt(review=reviews)])
        self.assertEqual(result["review_count"], 1)
        self.assertEqual(result["required_item_pass"]["rate"], 1)

    def test_partial_response_review_separate_from_complete(self):
        """
        응답 일부의 정확한 문장을 정상 완료 응답의 성과로 오인하지 않는 검사

        미완료 응답의 부분 설명과 정상 완료 응답의 점수를 구분한 집계 확인
        """
        reviews = [{"review": {"method": "required", "required_items": [{"verdict": "pass"}]}}]
        result = _summarize_group([{}, {}, {}], [_attempt(review=reviews), _attempt("failed", False, review=reviews)])
        self.assertEqual(result["required_review_coverage"]["numerator"], 2)
        self.assertEqual(result["required_review_coverage"]["denominator"], 2)
        groups = result["required_items_by_completion"]
        self.assertEqual(groups["completed_response"]["pass"]["denominator"], 1)
        self.assertEqual(groups["incomplete_response"]["pass"]["denominator"], 1)
        self.assertEqual(result["final_completed"]["numerator"], 1)
        self.assertEqual(result["unstarted"], 1)


    def test_model_vram_uses_matching_tag_and_observation(self):
        """
        다른 모델의 적재량 및 시스템 전체 GPU 최댓값과 혼합하지 않는 검사

        요청 모델의 적재량 선택, 다른 모델 및 GPU 전체 사용량과의 혼합 방지 확인
        """
        from modules.evaluation_report import _loaded_mib
        record = {"request": {"model": "chosen"},
                  "peak_gpu_used_mib": 12000,
                  "loaded_models": {"models": [
                      {"name": "other", "size_vram": 1},
                      {"name": "chosen", "size_vram": 104857600, "size": 209715200}]}}
        self.assertEqual(_loaded_mib(record, "size_vram"), 100)
        self.assertEqual(_loaded_mib(record, "size"), 200)
        record["request"]["model"] = "not-loaded"
        self.assertIsNone(_loaded_mib(record, "size_vram"))


    def test_repeat_requires_two_completed_records(self):
        """
        두 완료 응답의 전체 문자열 일치 여부를 이용한 반복 결과 집계 확인
        """
        first, second = _attempt(), _attempt()
        second["repeat_number"] = 2
        result = _summarize_group([{}, {}], [first, second])
        self.assertEqual(result["identical_repeat_text"]["rate"], 1)
        second["record"]["content"] = "changed"
        result = _summarize_group([{}, {}], [first, second])
        self.assertEqual(result["identical_repeat_text"]["rate"], 0)


if __name__ == "__main__":
    unittest.main()
