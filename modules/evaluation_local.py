"""
Ollama HTTP 호출과 로컬 자원 측정

구성:
    - 별도 수신 프로세스와 부모 프로세스의 전체 제한 시간 적용
    - 응답 스트림, 최종 메타데이터와 실패 원문의 보존
    - Windows 시스템 상태와 nvidia-smi 표본의 수집
    - 응답 직후 모델 적재량 조회 및 해제

측정 구분: 전체 호출 시간, 서버 처리 시간, 모델 적재량과 시스템 전체 사용량
"""

import ctypes
import hashlib
import json
import math
import multiprocessing
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from .data_evaluation_cases import _reject_constant, _unique_object, _to_json


# 로컬 서버만 사용하는 평가 경로, 실제 주문과 외부 유료 API 호출 제외
OLLAMA_URL = "http://localhost:11434"


def _json(value):
    """
    입력과 기록의 UTF-8 JSON 표현 통일

    입력: Ollama 요청 또는 수신 기록 객체
    반환: 한글을 보존한 간결한 JSON 문자열
    구분: 정렬 해시용 _to_json과 달리 입력 키 순서를 유지하는 전송 표현
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def get_ollama_info(path="/api/tags", body=None):
    """
    Ollama 설치 정보 조회 또는 모델 해제 요청

    입력: 로컬 API 경로, 선택적인 POST 본문
    반환: JSON으로 해석한 서버 응답
    시간 제한: 요청당 20초
    효과: 조회 또는 명시적인 모델 해제 요청, 서버 접속 오류의 호출자 전달
    """
    data = _json(body).encode("utf-8") if body is not None else None
    request = Request(OLLAMA_URL + path, data=data, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def _receive_chat(payload, connection, timeout):
    """
    별도 프로세스에서 응답 원문을 한 줄씩 전달하는 함수

    목적:
        - 첫 토큰을 기다리는 중에도 부모에서 전체 호출 시간 제한 적용
        - 최종 JSON 누락이나 중간 연결 종료 시 이미 받은 응답의 보존
        - 모델 응답을 고치거나 실패를 hold 결정으로 바꾸지 않는 처리

    입력: 전송 본문, 부모 연결 통로와 수신 제한 시간
    전달: line, end 또는 error 이벤트
    종료: 성공과 실패 모두 연결 통로 해제
    """
    try:
        request = Request(OLLAMA_URL + "/api/chat", data=_json(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            for line in response:
                connection.send(("line", line.decode("utf-8")))
        connection.send(("end", None))
    except HTTPError as error:
        connection.send(("error", {"type": "http", "status": error.code,
                                  "detail": error.read().decode("utf-8", errors="replace")}))
    except Exception as error:
        connection.send(("error", {"type": type(error).__name__, "detail": str(error)}))
    finally:
        connection.close()


class _MemoryStatus(ctypes.Structure):
    """Windows가 제공하는 전체 물리 메모리의 조회 구조"""
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                ("extended", ctypes.c_ulonglong)]


def _sample_resources():
    """
    GPU 전체 사용량과 시스템 전체 RAM 상태 조회

    주의사항:
        - GPU 사용량에 바탕화면과 다른 프로그램의 사용량 포함
        - RAM은 모델 프로세스 전용 사용량이 아닌 시스템 전체 사용량
        - 표본 간격 사이의 순간 최대값은 관측하지 못할 수 있는 한계
        - 조회 불가 항목은 None 유지, 0으로 대체하지 않는 처리

    반환: GPU 사용량, RAM과 CPU 누적 시간 표본의 사전
    단위: 메모리 MiB, 이용률 %, CPU 누적 틱
    기준: CPU 이용률은 호출 루프의 연속 표본 차이로 계산
    """
    sample = {"gpu_used_mib": None, "gpu_utilization_percent": None,
              "ram_used_mib": None, "ram_available_mib": None,
              "cpu_idle_ticks": None, "cpu_total_ticks": None, "cpu_utilization_percent": None}
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if output.returncode == 0:
            values = output.stdout.strip().splitlines()[0].split(",")
            sample["gpu_used_mib"] = float(values[0])
            sample["gpu_utilization_percent"] = float(values[1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    try:
        memory = _MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            sample["ram_used_mib"] = (memory.total - memory.available) / 1048576
            sample["ram_available_mib"] = memory.available / 1048576
    except (AttributeError, OSError):
        pass
    try:
        idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                ctypes.byref(user)):
            sample["cpu_idle_ticks"] = idle.value
            sample["cpu_total_ticks"] = kernel.value + user.value
    except (AttributeError, OSError):
        pass
    return sample


def build_ollama_request(model, messages, *, num_ctx=32768, num_predict=2048,
                         thinking=None, output_format=None, temperature=0.0, extra_options=None):
    """
    로컬 API에 보낼 본문 구성, 호출 전 저장과 실제 전송의 공통 사용

    주의사항:
        - 입력 초과 시 자동 잘림과 생성 중 context 이동의 비활성화
        - 서버의 지원 여부는 별도 예비 검사로 확인
        - 모든 실험에서 추론 원문을 후속 대화에 자동 삽입하지 않는 구성

    입력: 모델, 대화와 문맥 및 생성 설정
    반환: 호출 전 저장과 실제 전송에 공통 사용할 본문
    주의: 범용 기본 문맥값과 달리 이번 PoC에서는 num_ctx=8192 명시
    제한: 추가 옵션으로 문맥과 생성 한도 등의 덮어쓰기 차단
    """
    payload = {"model": model, "messages": messages, "stream": True, "keep_alive": "1m",
               "truncate": False, "shift": False,
               "options": {"num_ctx": num_ctx, "num_predict": num_predict,
                           "temperature": temperature, "seed": 42}}
    # PoC의 반복 억제 및 표본 선택 설정 명시, 기존 호출의 기본 동작 유지
    # 문맥 크기와 생성 한도 등을 extra_options로 우회 변경하지 않는 제한
    if extra_options is not None:
        bounds = {"presence_penalty": (-2, 2), "frequency_penalty": (-2, 2),
                  "repeat_penalty": (0, None), "top_k": (0, None),
                  "top_p": (0, 1), "min_p": (0, 1)}
        if not isinstance(extra_options, dict) or set(extra_options) - set(bounds):
            raise ValueError("지원하지 않는 추가 생성 설정")
        for name, value in extra_options.items():
            lower, upper = bounds[name]
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError("생성 설정은 유한한 숫자 필요")
            if value < lower or (upper is not None and value > upper):
                raise ValueError("생성 설정 범위 초과")
            if name == "top_k" and type(value) is not int:
                raise ValueError("top_k는 정수 필요")
            if name == "top_p" and value == 0:
                raise ValueError("top_p는 0 초과 필요")
        payload["options"].update(extra_options)
    if thinking is not None:
        payload["think"] = thinking
    if output_format is not None:
        payload["format"] = output_format
    return payload


def call_ollama(model, messages, *, num_ctx=32768, num_predict=2048,
                thinking=None, output_format=None, timeout=300, temperature=0.0, extra_options=None):
    """
    로컬 모델 한 번 호출 및 원본 응답과 실행 측정값 반환

    매개변수:
        model (str): 설치된 정확한 Ollama 태그
        messages (list): 평가용 정답을 제외한 역할별 입력
        num_ctx (int): 명시적으로 요청할 context 크기
        num_predict (int): 추론을 포함한 생성 토큰의 요청 상한
        thinking (bool 또는 None): 지원 모델의 추론 설정, None은 인자 생략
        output_format (str, dict 또는 None): json, JSON Schema 또는 제약 없음
        timeout (float): 모델 로딩부터 최종 응답까지의 전체 제한 시간
        temperature (float): 호출 시 명시할 샘플링 설정
        extra_options (dict 또는 None): 반복 억제 및 top_k, top_p, min_p의 공통 지정값

    반환값:
        dict: 요청 원문, 원본 스트림, 최종 내용, 종료 상태와 측정값

    주의사항:
        - 동일 정보와 동일 토큰 수는 다른 조건, 모델별 실측 토큰 수 기록
        - API 정상 종료와 사실 정확성은 별도 판정
        - 호출 제한 시간은 부모에서 적용, 종료 및 정리 비용은 별도 발생 가능
        - keep_alive로 적재 상태 유지 후 관측, 마지막에 해당 모델 해제
        - 다중 대화는 호출자가 messages에 필요한 최종 응답 이력 제공
        - 별도 프로세스 사용으로 실행 진입점의 __main__ 보호 필요

    입력: 모델 태그와 대화, 출력 형식 및 시간과 생성 제한
    반환: 요청, 수신 원문, 최종 응답, 메타데이터와 자원 표본 사전
    측정: 수신 프로세스 시작부터 응답 수신까지의 wall_seconds
    효과: 모델 1회 호출과 해제, SQLite 저장은 상위 실행기의 별도 수행
    """
    if num_ctx <= 0 or num_predict <= 0 or timeout <= 0:
        raise ValueError("context, 생성 한도와 시간 제한은 양수 필요")

    # ------------------------------ * 1. 요청과 실행 식별 정보 구성 * ------------------------------
    payload = build_ollama_request(model, messages, num_ctx=num_ctx, num_predict=num_predict,
                                   thinking=thinking, output_format=output_format,
                                   temperature=temperature, extra_options=extra_options)
    record = {"run_id": str(uuid4()), "started_at": datetime.now(timezone.utc).isoformat(),
              "request": payload, "request_hash": hashlib.sha256(_to_json(payload).encode()).hexdigest(),
              "status": "running", "raw_lines": [], "errors": [], "resource_samples": [],
              "content": "", "thinking": "", "response_metadata": None, "loaded_models": None}

    # ------------------------------ * 2. 전체 시간 제한과 자원 관측 * ------------------------------
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_receive_chat, args=(payload, sender, timeout))
    baseline = _sample_resources()
    record["baseline_resources"] = baseline
    record["runner_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    try:
        record["ollama_version"] = get_ollama_info("/api/version").get("version")
        installed = get_ollama_info("/api/tags").get("models", [])
        record["installed_model"] = next((item for item in installed if item["name"] == model), None)
        details = get_ollama_info("/api/show", {"model": model})
        record["model_settings"] = {key: details.get(key) for key in
                                    ("template", "parameters", "details", "capabilities", "requires")}
    except Exception as error:
        record["errors"].append({"type": "metadata_query", "detail": str(error)})
    record["messages_hash"] = hashlib.sha256(_to_json(messages).encode("utf-8")).hexdigest()
    started = time.perf_counter()
    process.start()
    sender.close()
    next_sample = started
    previous_sample = baseline
    first_content = None
    first_token = None
    done = False
    try:
        while True:
            now = time.perf_counter()
            if now - started >= timeout:
                record["status"] = "timeout"
                break
            if now >= next_sample:
                sample = _sample_resources()
                # 표본 사이 시스템 전체 CPU 사용률, 모델 전용 수치와 구분
                if sample.get("cpu_total_ticks") is not None and previous_sample.get("cpu_total_ticks") is not None:
                    total = sample["cpu_total_ticks"] - previous_sample["cpu_total_ticks"]
                    idle = sample["cpu_idle_ticks"] - previous_sample["cpu_idle_ticks"]
                    if total > 0:
                        sample["cpu_utilization_percent"] = max(0.0, min(100.0, 100 * (1 - idle / total)))
                previous_sample = sample
                sample["elapsed_seconds"] = time.perf_counter() - started
                record["resource_samples"].append(sample)
                next_sample = time.perf_counter() + 1.0
            if receiver.poll(0.05):
                try:
                    event, value = receiver.recv()
                except EOFError:
                    break
                if event == "line":
                    record["raw_lines"].append(value)
                    try:
                        chunk = json.loads(value)
                        message = chunk.get("message", {})
                        content = message.get("content", "")
                        thinking_text = message.get("thinking", "")
                        if (content or thinking_text) and first_token is None:
                            first_token = time.perf_counter() - started
                        if content and first_content is None:
                            first_content = time.perf_counter() - started
                        record["content"] += content
                        record["thinking"] += thinking_text
                        if chunk.get("error"):
                            record["errors"].append({"type": "server", "detail": chunk["error"]})
                        if chunk.get("done"):
                            record["response_metadata"] = {key: val for key, val in chunk.items()
                                                           if key != "message"}
                            done = True
                            break
                    except (ValueError, TypeError, AttributeError) as error:
                        record["errors"].append({"type": "stream", "detail": str(error)})
                elif event == "error":
                    record["errors"].append(value)
                    break
                else:
                    break
            elif not process.is_alive():
                break
        if record["status"] != "timeout":
            # 정상 종료 표식 없이 끝난 연결의 명시적 분류, 부분 응답의 성공 처리 방지
            if not done and not record["errors"]:
                record["errors"].append({"type": "incomplete_stream",
                                         "detail": "done=true 표식 없이 응답 통로 종료"})
            record["status"] = "completed" if done and not record["errors"] else "failed"
    finally:
        record["wall_seconds"] = time.perf_counter() - started
        record["first_token_seconds"] = first_token
        record["first_content_seconds"] = first_content
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        receiver.close()
        try:
            record["loaded_models"] = get_ollama_info("/api/ps")
        except Exception as error:
            record["errors"].append({"type": "resource_query", "detail": str(error)})
        try:
            get_ollama_info("/api/generate", {"model": model, "keep_alive": 0})
        except Exception as error:
            record["errors"].append({"type": "unload", "detail": str(error)})

    # ------------------------------ * 3. 관측 결과 요약 * ------------------------------
    samples = record["resource_samples"]
    for field in ("gpu_used_mib", "gpu_utilization_percent", "ram_used_mib", "cpu_utilization_percent"):
        values = [sample[field] for sample in samples if sample.get(field) is not None]
        record["peak_" + field] = max(values) if values else None
    metadata = record["response_metadata"] or {}
    record["final_response_complete"] = (
        record["status"] == "completed" and metadata.get("done_reason") == "stop"
        and bool(record["content"].strip())
    )
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    return record
