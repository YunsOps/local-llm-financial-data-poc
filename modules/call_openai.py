"""
개인 API 키를 이용한 Luna 비교 실행

구성:
    - 전달받은 메시지를 사용한 단일 API 호출, 문항 선택은 main.py에서 수행
    - 공식 Responses API의 스트림 호출과 최종 내용 및 사용량 수집
    - 반환된 토큰 사용량과 고정 단가의 비용 계산
    - 전체 호출 시간 제한과 자동 유료 재시도 차단

보관 범위: 키를 제외한 요청 본문, 응답, 시간과 사용량
"""

# API 응답 수신을 별도 Python 프로세스에서 처리할 multiprocessing 가져오기
import multiprocessing
# 환경 변수에서 API 키를 읽을 os 모듈 가져오기
import os
# 오류 문자열에서 키 형태를 찾아 가릴 정규식 도구 re 가져오기
import re
# 서버 통계와 별도로 사용자가 기다린 경과 시간을 측정할 time 가져오기
import time
# 시작 및 종료 시각을 UTC와 함께 기록할 날짜 도구 가져오기
from datetime import datetime, timezone
# 달러 단가의 소수 오차를 줄이는 Decimal 숫자 자료형 가져오기
from decimal import Decimal
# 프로젝트 .env의 경로를 구성할 Path 가져오기
from pathlib import Path

# .env를 읽어 키와 값의 사전으로 반환하는 dotenv_values 가져오기
from dotenv import dotenv_values



# 외부 API 요청에 사용할 시험 대상 모델 이름 지정
LUNA_MODEL = "gpt-5.6-luna"
# 시험 당시 확인한 단가의 보존, 현재 청구 금액이나 최신 가격의 자동 조회 없음
# 시험 당시 보존한 가격 정보를 사전으로 정의, 현재 요금의 자동 조회 없음
LUNA_PRICING = {
    # 단가를 확인한 기록 날짜 보관
    "checked_at": "2026-09-15",
    # 기록 단가의 출처 주소 보관, 이 문자열 자체의 웹 조회 없음
    "source": "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
    # 통화를 미국 달러로 지정하고 단가의 기준 단위를 백만 토큰으로 명시
    "currency": "USD", "per_tokens": 1000000,
    # 일반 입력, 캐시 읽기와 출력의 당시 단가를 정확한 소수 변환용 문자열로 보관
    "input": "0.20", "cached_input": "0.02", "output": "1.20",
    # 캐시 쓰기 배율과 긴 입력에 적용할 기준 토큰 수 보관
    "cache_write_multiplier": "1.25", "long_input_threshold": 272000,
    # 긴 입력 조건에서 사용할 입력 및 출력 단가 배율 보관
    "long_input_multiplier": "2", "long_output_multiplier": "1.5",
# 가격 정보 사전 구성 종료
}


# 환경 변수 또는 프로젝트 파일에서 인증키를 읽는 내부 함수 정의
def _api_key():
    """
    현재 프로세스 또는 프로젝트 .env에서 개인 키 조회, 반환값의 기록 금지

    입력: OPENAI_API_KEY 환경 변수 또는 프로젝트 .env
    반환: 공백을 제거한 키 문자열, 미설정 시 빈 문자열
    우선순위: 프로세스 환경 변수 우선, 키 원문의 출력과 기록 금지
    """
    # 현재 모듈의 상위 폴더 두 단계를 올라가 프로젝트 루트의 .env 경로 구성
    path = Path(__file__).resolve().parent.parent / ".env"
    # 환경 변수 우선, 없으면 .env 값 사용, 모두 없으면 빈 문자열 반환 후 앞뒤 공백 제거
    return (os.environ.get("OPENAI_API_KEY") or dotenv_values(path).get("OPENAI_API_KEY") or "").strip()


# 키 원문 대신 설정 여부만 반환하는 함수 정의
def luna_key_available():
    """
    키 원문을 노출하지 않는 설정 여부 확인

    반환: 키 설정 여부의 bool
    목적: 유료 호출 전 설정 확인, 키 내용의 노출 방지
    """
    # 빈 문자열은 False, 내용이 있는 문자열은 True로 변환해 키 노출 없이 확인
    return bool(_api_key())


# 공통 메시지를 Responses API 요청 사전으로 변환하는 함수 정의, * 뒤는 이름 지정 인자
def build_luna_request(messages, *, max_output_tokens=2048, reasoning_effort="none",
                       # 선택적 JSON 구조와 샘플링 온도의 기본값 지정
                       output_format=None, temperature=0.0):
    """
    동일한 대화 내용을 Luna Responses API 본문으로 변환

    조건:
        - 외부 도구, 이전 응답 ID와 서버 대화 상태의 사용 없음
        - 입력 자동 잘림 비활성, 응답 저장 옵션 비활성
        - 기본 비교는 추론 none, 추론 변경은 별도 확장 조건
        - seed와 로컬 context 크기는 지원이 확인된 공통 API 인자로 취급하지 않는 기준

    입력: 공통 대화, 출력 한도와 추론 및 스키마 설정
    반환: 인증 정보가 없는 Responses API 요청 사전
    기준: 입력 자동 잘림과 서버 응답 저장 비활성, 도구 호출 없음
    """
    # 생성 한도가 1 이상의 정수인지 검사, bool과 실수는 제외
    if type(max_output_tokens) is not int or max_output_tokens < 1:
        # 잘못된 생성 한도를 서버 요청 전에 오류로 처리
        raise ValueError("양의 생성 한도 필요")
    # 프로젝트에서 허용한 추론 수준 이름인지 확인, 서버의 실제 지원 여부와는 별도 검사
    if reasoning_effort not in ("none", "low", "medium", "high", "xhigh", "max"):
        # 허용 목록에 없는 추론 설정의 요청 차단
        raise ValueError("지원하지 않는 Luna 추론 조건")
    # 대상 모델과 공통 메시지 지정, 스트림 사용과 서버 응답 저장 비활성 옵션 설정
    request = {"model": LUNA_MODEL, "input": messages, "stream": True, "store": False,
               # 입력 자동 잘림을 비활성화하고 최대 생성 토큰 수 지정
               "truncation": "disabled", "max_output_tokens": max_output_tokens,
               # 추론 수준과 표준 서비스 등급을 요청에 포함
               "reasoning": {"effort": reasoning_effort}, "service_tier": "default"}
    # 추론을 사용하지 않는 조건일 때만 온도 전달
    if reasoning_effort == "none":
        # 비추론 샘플링 온도를 요청 최상위 필드에 저장
        request["temperature"] = temperature
    # 출력 형식은 미지정, json 문자열 또는 스키마 딕셔너리 중 하나인지 확인
    if output_format is not None and output_format != "json" and not isinstance(output_format, dict):
        # 지원하지 않는 출력 형식 자료형을 오류로 처리
        raise ValueError("JSON 또는 JSON Schema 형식 필요")
    # 출력 형식이 지정된 경우 API의 text.format 구성
    if output_format is not None:
        # json 문자열이면 일반 JSON 객체 형식을 선택하는 조건식 시작
        form = ({"type": "json_object"} if output_format == "json" else
                # 스키마 사전이면 이름과 strict 옵션을 가진 JSON Schema 형식으로 구성
                {"type": "json_schema", "name": "evaluation_response", "strict": True,
                 # 호출자가 전달한 공통 스키마를 그대로 연결하며 조건식 종료
                 "schema": output_format})
        # 완성한 출력 형식을 API가 요구하는 중첩 사전에 저장
        request["text"] = {"format": form}
    # 인증키를 포함하지 않은 요청 본문 반환
    return request


# 실제 토큰 사용량과 기록 단가로 예상 달러 비용을 계산하는 함수 정의
def calculate_luna_cost(usage, *, service_tier="default"):
    """
    응답의 실제 사용량과 확인한 단가에 따른 달러 비용 계산

    주의사항:
        - 추론 토큰은 output_tokens에 포함되므로 추가 중복 합산 없음
        - 캐시 읽기와 쓰기 토큰은 전체 입력의 일부로 계산
        - 필요한 사용량이 없으면 0원 대신 미확인 처리
        - 표준 단가 외 등급과 도구 비용은 이번 계산 범위에서 제외
        - 세금과 환율 및 실제 청구서의 반올림은 별도 항목

    입력: API의 실제 usage와 서비스 등급
    반환: USD 계산값 또는 None, 적용 단가와 미확인 사유
    검산: 일반 입력, 캐시 읽기, 캐시 쓰기와 출력을 구분한 Decimal 계산
    """
    # 아직 계산하지 않은 비용은 None, 적용 단가의 복사본과 사용량 기준임을 기록
    result = {"usd": None, "pricing": dict(LUNA_PRICING), "basis": "reported_usage"}
    # 이번 계산에 사용하는 표준 서비스 등급인지 확인
    if service_tier not in ("default", "standard"):
        # 다른 서비스 등급이면 잘못된 가격을 적용하지 않고 계산 불가 사유 반환
        return dict(result, note="표준 단가 외 서비스 등급")
    # 토큰 사용량이 딕셔너리 형태로 실제 반환됐는지 확인
    if not isinstance(usage, dict):
        # 사용량을 받지 못하면 0원 대신 미확인 비용과 사유 반환
        return dict(result, note="사용량 미반환")
    # 캐시 사용량이 있는 입력 세부 사전 조회, 없으면 빈 사전 사용
    details = usage.get("input_tokens_details") or {}
    # 전체 입력 토큰과 캐시 읽기 토큰을 계산용 목록에 담기 시작
    values = [usage.get("input_tokens"), details.get("cached_tokens"),
              # 캐시 쓰기 토큰과 출력 토큰까지 같은 목록에 추가
              details.get("cache_write_tokens"), usage.get("output_tokens")]
    # 필요한 사용량 중 비정수, 음수 또는 누락값이 하나라도 있는지 검사
    if any(type(value) is not int or value < 0 for value in values):
        # 필수 사용량이 불확실한 경우 비용 추정 대신 미확인 사유 반환
        return dict(result, note="입력, 출력 또는 캐시 사용량 미확인")
    # 네 사용량을 순서에 맞게 이름 있는 변수 네 개로 분리
    input_tokens, cached, written, output_tokens = values
    # 입력의 일부인 캐시 읽기와 쓰기 합계가 전체 입력을 넘는지 검산
    if cached + written > input_tokens:
        # 합계가 맞지 않는 사용량에 비용을 붙이지 않고 오류 사유 반환
        return dict(result, note="입력과 캐시 사용량의 합계 불일치")
    # 전체 입력에서 캐시 읽기와 쓰기를 빼 일반 입력 토큰 수 계산
    regular = input_tokens - cached - written
    # 기록된 긴 입력 단가 기준을 넘는지 비교
    long_input = input_tokens > LUNA_PRICING["long_input_threshold"]
    # 긴 입력이면 입력 요금 배율 2, 아니면 1을 Decimal로 선택
    input_multiplier = Decimal("2") if long_input else Decimal("1")
    # 긴 입력이면 출력 요금 배율 1.5, 아니면 1을 선택
    output_multiplier = Decimal("1.5") if long_input else Decimal("1")
    # 일반 입력 토큰 수에 기록된 백만 토큰당 0.20달러 단가를 곱하는 식 시작
    price = ((Decimal(regular) * Decimal("0.20")
              # 캐시 읽기 토큰의 별도 0.02달러 단가분 합산
              + Decimal(cached) * Decimal("0.02")
              # 캐시 쓰기 비용을 일반 입력 단가의 1.25배로 합산한 뒤 입력 배율 적용
              + Decimal(written) * Decimal("0.20") * Decimal("1.25")) * input_multiplier
             # 출력 단가와 출력 배율을 적용한 금액을 더하고 백만으로 나누어 실제 토큰 수의 비용 계산
             + Decimal(output_tokens) * Decimal("1.20") * output_multiplier) / Decimal("1000000")
    # 정확한 소수 표현을 유지한 비용 문자열과 적용 조건 및 계산 근거 반환
    return dict(result, usd=str(price), long_input_pricing=long_input, note="단가 기준 사용량 계산")


# 자식 프로세스에서 실제 유료 API를 한 번 호출하는 내부 함수 정의
def _receive_luna(request, connection, timeout):
    """
    별도 프로세스에서 개인 API 호출, 자동 재시도 비활성 및 원본 이벤트 전달

    입력: 요청 본문, 부모 연결과 시간 제한
    전달: 원본 API 이벤트 또는 인증값을 제거한 오류
    기준: SDK 자동 재시도 0회, 연결 종료의 finally 처리
    """
    # 자식 프로세스 내부에서만 실제 인증키 조회
    key = _api_key()
    # 호출 및 이벤트 전달 실패를 부모에게 알리기 위한 예외 처리 시작
    try:
        # 실제 Cloud 호출 시점에 OpenAI 클라이언트 클래스 가져오기
        from openai import OpenAI
        # 인증키가 비어 있는지 호출 직전에 다시 확인
        if not key:
            # 키가 없으면 외부 요청 없이 오류 발생
            raise ValueError("OPENAI_API_KEY 설정 필요")
        # 환경의 임의 프록시 주소를 API 목적지로 사용하지 않는 고정 공식 경로
        # 공식 API 주소와 개인 키로 클라이언트 열기, with 종료 시 연결 정리
        with OpenAI(api_key=key, base_url="https://api.openai.com/v1",
                    # SDK의 자동 재시도를 0회로 지정하고 네트워크 제한 시간 적용
                    max_retries=0, timeout=timeout) as client:
            # **request로 요청 사전을 인자로 펼쳐 스트림 생성, 이 지점에서 실제 유료 요청 수행
            with client.responses.create(**request) as stream:
                # 서버가 순서대로 보내는 응답 이벤트를 하나씩 수신
                for event in stream:
                    # SDK 이벤트 객체를 JSON 호환 사전으로 변환해 부모 프로세스에 전달
                    connection.send(("event", event.model_dump(mode="json")))
        # 스트림 수신을 마쳤음을 end 이벤트로 부모에게 전달
        connection.send(("end", None))
    # 키 누락, 네트워크 오류와 SDK 예외를 공통 처리
    except Exception as error:
        # 예외 메시지를 기록 가능한 문자열로 변환
        detail = str(error)
        # 조회한 인증키가 있을 때 원문 제거 처리 수행
        if key:
            # 오류 메시지에 실제 키가 포함된 경우 REDACTED 표시로 교체
            detail = detail.replace(key, "[REDACTED]")
        # 추가로 sk- 형태의 키 문자열도 정규식으로 찾아 가림
        detail = re.sub(r"sk-[A-Za-z0-9_*-]+", "[REDACTED]", detail)
        # 키를 제거한 오류의 클래스 이름을 전달할 사전 구성
        connection.send(("error", {"type": type(error).__name__,
                                  # HTTP 상태가 있으면 함께 기록하고 없으면 None 사용, 정리한 오류 설명 전달
                                  "status": getattr(error, "status_code", None), "detail": detail}))
    # 호출 결과와 무관하게 실행하는 연결 정리 구간
    finally:
        # 자식의 송신 통로 닫기
        connection.close()


# 외부 API 호출과 전체 제한 시간을 관리하고 결과를 반환하는 함수 정의
def call_luna(messages, *, max_output_tokens=2048, reasoning_effort="none",
              # 출력 구조, 비추론 온도와 기본 제한 300초 지정
              output_format=None, temperature=0.0, timeout=300):
    """
    Luna 한 번 호출 및 응답 원문, 시간과 실제 사용량 보존

    측정 기준:
        - 네트워크와 서버 처리 시간을 포함한 사용자 관측 지연
        - 원격 서버 GPU와 RAM은 미확인, 로컬 GPU 수치로 대체 없음
        - SDK 자동 재시도 0회, 실패 후 임의 추가 결제 호출 없음
        - 부모 프로세스에서 전체 호출 제한 적용
        - 시간 초과 후 제공되지 않은 사용량과 비용은 미확인 유지

    입력: 대화, 출력 형식과 생성 및 시간 제한
    반환: 실제 요청과 최종 응답, 시간과 사용량 및 비용 사전
    구분: API 종료 이벤트, 완전한 최종 텍스트와 형식 검사는 별개
    효과: 유료 요청 1회, 서버 내부 시간과 GPU 사용량은 미측정
    """
    # 전체 호출 제한이 양수인지 확인
    if timeout <= 0:
        # 0 이하의 시간 제한을 요청 전 차단
        raise ValueError("양의 호출 시간 제한 필요")
    # API 키 설정 여부만 확인, 키를 반환 결과에 포함하지 않는 구분
    if not luna_key_available():
        # 키 미설정 시 실제 유료 요청 전에 오류 발생
        raise ValueError("OPENAI_API_KEY 설정 필요")
    # 대화와 생성 한도를 공통 요청 구성 함수에 전달
    request = build_luna_request(messages, max_output_tokens=max_output_tokens,
                                 # 추론 수준을 요청에 반영
                                 reasoning_effort=reasoning_effort,
                                 # 출력 스키마와 온도까지 전달해 최종 요청 본문 생성
                                 output_format=output_format, temperature=temperature)
    # 호출 시각과 측정에 필요한 값만 기록, 관리용 식별값 생성 없음
    # 호출 시작 시각을 마이크로초까지 포함한 UTC 문자열로 기록
    started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    # 시작 시각과 실제 요청을 결과 사전에 보관
    record = {"started_at": started_at, "request": request,
              # 진행 상태, 최종 내용, 거절 내용과 오류 목록 초기화
              "status": "running", "content": "", "refusal": "", "errors": [],
              # 서버 통계와 사용량은 None, 최종 답변 완료 여부는 False로 초기화
              "response_metadata": None, "usage": None, "final_response_complete": False}
    # 종료 이벤트를 받지 못한 경우에도 조회 가능한 빈 최종 응답 사전 준비
    response = {}
    # ------------------------------ * 별도 수신 프로세스와 전체 제한 시간 준비 * ------------------------------
    # 새 Python 프로세스를 시작하는 spawn 방식 선택
    context = multiprocessing.get_context("spawn")
    # 단방향 Pipe 생성, 부모 수신용 reader와 자식 송신용 writer 분리
    reader, writer = context.Pipe(duplex=False)
    # 자식이 실행할 API 함수에 요청과 송신 통로 및 제한 시간을 전달하도록 준비
    process = context.Process(target=_receive_luna, args=(request, writer, timeout))
    # 사용자 관측 응답 시간의 시작 기준 확보
    started = time.perf_counter()
    # 최종 내용의 첫 조각을 아직 받지 않았다는 None 지정
    first_content = None
    # 자식 프로세스를 시작해 API 호출 진행
    process.start()
    # 부모 쪽에 남아 있는 송신 통로 닫기, 자식 송신 통로는 유지
    writer.close()
    # 수신 도중 예외가 나도 프로세스를 정리하기 위한 try 구간
    try:
        # 종료나 오류 또는 시간 초과까지 이벤트 수신 반복
        while True:
            # 네트워크와 서버 대기를 포함한 전체 시간이 제한에 도달했는지 확인
            if time.perf_counter() - started >= timeout:
                # 시간 초과 상태 기록
                record["status"] = "timeout"
                # 반복을 끝내고 정리 단계로 이동
                break
            # 최대 0.05초 동안 수신 가능한 이벤트가 있는지 확인
            if reader.poll(0.05):
                # 통로의 갑작스러운 종료를 구분하기 위한 예외 처리
                try:
                    # 자식 이벤트 이름과 내용 사전을 수신
                    event, value = reader.recv()
                # 송신 통로가 닫혀 더 읽을 내용이 없는 EOFError 처리
                except EOFError:
                    # 더 기다리지 않고 수신 반복 종료
                    break
                # SDK 원본 이벤트가 전달된 경우 분기
                if event == "event":
                    # 서버 이벤트의 세부 유형 이름 조회
                    event_type = value.get("type")
                    # 최종 답변 텍스트의 증가분 이벤트인지 확인
                    if event_type == "response.output_text.delta":
                        # 이번 delta 문자열을 앞서 받은 답변 뒤에 추가
                        record["content"] += value.get("delta", "")
                        # 최종 내용의 첫 이벤트인지 확인
                        if first_content is None:
                            # 첫 내용 이벤트까지의 클라이언트 경과 시간 기록
                            first_content = time.perf_counter() - started
                    # 답변 거절 내용의 증가분 이벤트인 경우 분기
                    elif event_type == "response.refusal.delta":
                        # 거절 문구를 정상 답변과 구분한 문자열에 누적
                        record["refusal"] += value.get("delta", "")
                    # 서버의 정상 완료, 불완전 완료 또는 실패 종료 이벤트인지 확인
                    elif event_type in ("response.completed", "response.incomplete", "response.failed"):
                        # 종료 이벤트에 포함된 최종 응답 객체 확보
                        response = value["response"]
                        # 최종 응답에서 텍스트 조각들을 빈 구분자로 연결하는 작업 시작
                        final_text = "".join(
                            # 응답의 output 목록을 순회하며 각 텍스트 부분의 text 조회
                            part.get("text", "") for item in response.get("output", [])
                            # message 유형의 출력만 선택한 뒤 해당 메시지의 content 항목들을 순회
                            if item.get("type") == "message" for part in item.get("content", [])
                            # content 중 output_text 유형만 골라 최종 답변에 포함
                            if part.get("type") == "output_text"
                        # 선택한 모든 최종 텍스트를 하나의 문자열로 연결 완료
                        )
                        # 종료 객체에 실제 최종 답변 텍스트가 있는지 확인
                        if final_text:
                            # 스트림으로 누적한 내용이 있는데 최종 객체의 텍스트와 다른지 대조
                            if record["content"] and final_text != record["content"]:
                                # 두 원문이 다른 경우의 오류 유형 기록 시작
                                record["errors"].append({"type": "stream_content_mismatch",
                                                         # 수신 중간 내용과 최종 원문의 불일치 이유 기록
                                                         "detail": "중간 문자열과 최종 원문 불일치"})
                            # 서버 종료 객체의 최종 텍스트를 결과에 보관, 불일치는 오류 기록으로 유지
                            record["content"] = final_text
                        # 서버가 제공한 실제 토큰 사용량 객체 보존
                        record["usage"] = response.get("usage")
                        # 실패 이벤트이면 failed, 그 외 종료 이벤트이면 호출 절차상 completed로 분류
                        record["status"] = "failed" if event_type == "response.failed" else "completed"
                        # 정상 완료 이벤트이면 종료 사유를 stop으로 지정
                        reason = ("stop" if event_type == "response.completed" else
                                  # 불완전 응답이면 서버가 제공한 이유 조회, 없으면 failed로 표시
                                  (response.get("incomplete_details") or {}).get("reason", "failed"))
                        # 서버가 보고한 실제 모델 이름을 최종 통계에 기록
                        record["response_metadata"] = {"model": response.get("model"),
                                                       # 서버 응답 식별값을 내부 호출 기록에 보관, CSV의 관리용 열로 추가하는 처리와는 별개
                                                       "response_id": response.get("id"),
                                                       # 정상 또는 불완전 종료 사유 기록
                                                       "done_reason": reason,
                                                       # 서비스 등급을 보존해 비용 계산 시 적용 가능 여부 확인
                                                       "service_tier": response.get("service_tier")}
                        # 최종 이벤트 처리 후 수신 반복 종료
                        break
                    # 서버 스트림 자체의 오류 이벤트인지 확인
                    elif event_type == "error":
                        # 서버가 전달한 오류 설명 보존
                        record["errors"].append({"type": "server", "detail": value.get("message")})
                # SDK 예외 등을 자식이 정리한 error 이벤트인 경우 분기
                elif event == "error":
                    # 키가 제거된 오류 사전을 결과에 추가
                    record["errors"].append(value)
                    # 자식 호출 실패 후 수신 반복 종료
                    break
                # end 등 수신 종료 이벤트 처리
                else:
                    # 수신을 끝내고 최종 이벤트 유무 검사로 이동
                    break
            # 이벤트가 없으며 자식도 종료된 경우 확인
            elif not process.is_alive():
                # 더 이상 응답을 기다릴 수 없는 상태에서 반복 종료
                break
        # 최종 이벤트와 시간 초과 처리 없이 여전히 running인지 확인
        if record["status"] == "running":
            # 끝나지 않은 스트림을 완료로 오인하지 않도록 failed 지정
            record["status"] = "failed"
            # 구체적인 오류가 아직 없는 경우에만 종료 표식 부재 기록
            if not record["errors"]:
                # 최종 이벤트 없이 통로가 끝났다는 오류 추가
                record["errors"].append({"type": "incomplete_stream", "detail": "최종 이벤트 없이 종료"})
    # 수신 결과와 관계없이 프로세스와 통로 정리 수행
    finally:
        # 정리 작업 이전까지의 전체 관측 시간 확정
        record["wall_seconds"] = time.perf_counter() - started
        # 첫 내용 도착 시간을 별도 필드에 저장
        record["first_content_seconds"] = first_content
        # 자식 수신 프로세스가 계속 실행 중인지 확인
        if process.is_alive():
            # 남은 자식 프로세스 종료 요청, 이미 발생한 서버 사용량의 취소를 보장하는 동작은 아님
            process.terminate()
        # 프로세스 종료를 최대 5초 기다리며 정리
        process.join(timeout=5)
        # 부모 수신 통로 닫기
        reader.close()
    # ------------------------------ * 정상 완료 여부와 실제 사용량 비용 확정 * ------------------------------
    # 단순 스트림 종료와 실제 사용 가능한 최종 답변을 구분하는 조건식 시작
    record["final_response_complete"] = (
        # 클라이언트 호출 상태와 서버 응답 상태 모두 completed인지 확인
        record["status"] == "completed" and response.get("status") == "completed"
        # 최종 내용 존재, 오류 부재와 거절 내용 부재까지 모두 요구
        and bool(record["content"].strip()) and not record["errors"] and not record["refusal"]
    # 최종 응답 완료 여부를 bool로 계산하는 조건식 종료
    )
    # 서버에서 받은 실제 사용량으로 비용 계산 함수 호출
    record["cost"] = calculate_luna_cost(record["usage"],
                                          # 서버 서비스 등급을 함께 전달해 고정 단가 적용 가능 여부 검사
                                          service_tier=response.get("service_tier", "default"))
    # 모든 정리와 비용 계산이 끝난 시각을 UTC로 기록
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    # 인증키를 제외한 요청, 응답, 시간과 사용량 및 비용 사전 반환
    return record
