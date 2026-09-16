"""
PoC local 코드의 자동 검사

검사 방식:
    - 가상 입력과 임시 DB 또는 대체 응답 사용
    - 정상 사례와 실패 및 경계 사례의 결과 대조
    - 외부 모델 및 유료 API 호출 없음

용도: 실행과 저장 코드의 오류 확인, 실제 모델 품질 평가와 별도 구분
"""

import time
import unittest
from unittest.mock import patch

from . import evaluation_local as local


class _Connection:
    """실제 모델 호출 없이 정상 응답과 연결 종료를 재현하는 입력 통로"""
    def __init__(self, events):
        """
        실제 외부 호출을 대체할 검사용 상태와 이벤트의 초기화
        """
        self.events = list(events)

    def poll(self, timeout):
        """
        남은 가상 이벤트 존재 여부 반환, 짧은 대기로 제한 시간 경로 재현
        """
        if not self.events:
            time.sleep(min(timeout, 0.002))
        return bool(self.events)

    def recv(self):
        """
        대기 중인 가상 이벤트 한 건의 순서대로 반환
        """
        return self.events.pop(0)

    def close(self):
        """
        외부 자원 없는 가상 연결의 종료 인터페이스 제공
        """
        pass


class _Process:
    """시간 초과 시 실행 프로세스 종료 여부 확인용 대체 객체"""
    def __init__(self):
        """
        실제 외부 호출을 대체할 검사용 상태와 이벤트의 초기화
        """
        self.alive = False

    def start(self):
        """
        가상 프로세스의 실행 중 상태 설정
        """
        self.alive = True

    def is_alive(self):
        """
        가상 프로세스의 실행 중 여부 반환
        """
        return self.alive

    def terminate(self):
        """
        가상 프로세스의 종료 상태 설정
        """
        self.alive = False

    def join(self, timeout):
        """
        실제 대기 없는 가상 프로세스의 종료 대기 인터페이스 제공
        """
        pass


class _Context:
    """Windows의 별도 실행 프로세스와 통로를 동일 인터페이스로 대체"""
    def __init__(self, events):
        """
        실제 외부 호출을 대체할 검사용 상태와 이벤트의 초기화
        """
        self.reader = _Connection(events)
        self.process = _Process()

    def Pipe(self, duplex=False):
        """
        검사용 읽기 통로와 빈 쓰기 통로의 쌍 반환
        """
        return self.reader, _Connection([])

    def Process(self, **kwargs):
        """
        미리 만든 가상 프로세스 반환, 실제 프로세스 생성 없음
        """
        return self.process


class LocalEvaluationTests(unittest.TestCase):
    """실측 도구의 실패 보존과 시간 제한 및 SQLite 무결성 검증"""

    def invoke(self, events, timeout=1):
        """
        준비한 이벤트로 호출 수신과 종료 경로 실행, 실제 모델 호출 없음
        """
        context = _Context(events)
        resources = {"gpu_used_mib": None, "gpu_utilization_percent": None,
                     "ram_used_mib": None, "ram_available_mib": None}
        # 외부 서버와 GPU를 사용하지 않는 검사, 실제 측정값과 혼합 방지
        with patch.object(local.multiprocessing, "get_context", return_value=context), \
             patch.object(local, "_sample_resources", return_value=resources), \
             patch.object(local, "get_ollama_info", return_value={"version": "test", "models": []}):
            result = local.call_ollama("test:model", [], timeout=timeout)
        self.assertFalse(context.process.is_alive())
        return result

    def test_context_overflow_is_not_silently_shortened(self):
        """
        입력 자동 잘림과 문맥 이동의 비활성 설정 보존 확인
        """
        request = local.build_ollama_request("test:model", [{"role": "user", "content": "원문"}])
        self.assertIs(request["truncate"], False)
        self.assertIs(request["shift"], False)

    def test_completed_response_and_metadata(self):
        """
        최종 응답과 서버 토큰 및 종료 메타데이터의 보존 확인
        """
        result = self.invoke([
            ("line", '{"message":{"content":"한글 응답"}}\n'),
            ("line", '{"done":true,"done_reason":"stop","eval_count":3}\n'),
        ])
        self.assertEqual(result["content"], "한글 응답")
        self.assertTrue(result["final_response_complete"])
        self.assertEqual(result["response_metadata"]["eval_count"], 3)
        self.assertIsNone(result["peak_gpu_used_mib"])

    def test_partial_response_preserved_after_error(self):
        """
        수신 중 오류가 발생해도 이미 받은 원문을 유지하는 처리 확인
        """
        result = self.invoke([("line", '{"message":{"content":"{부분"}}\n'),
                              ("error", {"type": "connection", "detail": "연결 종료"})])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["content"], "{부분")
        self.assertEqual(len(result["raw_lines"]), 1)
        self.assertFalse(result["final_response_complete"])

    def test_stream_ends_without_final_marker(self):
        """
        종료 표식 없이 끊긴 스트림의 실패 기록과 부분 응답 보존 확인
        """
        result = self.invoke([("line", '{"message":{"content":"부분 응답"}}\n'),
                              ("end", None)])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"][0]["type"], "incomplete_stream")
        self.assertFalse(result["final_response_complete"])
        self.assertEqual(result["content"], "부분 응답")

    def test_generation_limit_is_not_complete(self):
        """
        생성 한도 도달과 정상 완료의 구분, 미완료 응답의 성공 집계 방지 확인
        """
        result = self.invoke([("line", '{"message":{"content":"{}"},"done":true,"done_reason":"length"}\n')])
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["final_response_complete"])

    def test_deadline_without_first_token(self):
        """
        첫 토큰이 없는 대기에서도 전체 제한 시간에 따른 프로세스 종료 확인
        """
        result = self.invoke([], timeout=0.02)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["content"], "")
        self.assertLess(result["wall_seconds"], 0.3)

    def test_invalid_stream_kept_as_failure(self):
        """
        해석 불가 스트림의 실패 분류, 정상 JSON으로의 자동 보정 방지 확인
        """
        result = self.invoke([("line", "JSON 아님\n"), ("end", None)])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["raw_lines"], ["JSON 아님\n"])


if __name__ == "__main__":
    unittest.main()
