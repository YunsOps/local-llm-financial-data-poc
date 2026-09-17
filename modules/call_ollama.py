"""
Ollama 호출과 로컬 자원 측정

구성:
    - 별도 수신 프로세스와 부모 프로세스의 전체 제한 시간 적용
    - 응답과 추론의 누적, 서버 측정값과 실패 원문의 보존
    - Windows 시스템 상태와 nvidia-smi 표본의 수집
    - 응답 직후 모델 적재량 조회 및 해제

측정 구분: 전체 호출 시간, 서버 처리 시간, 모델 적재량과 시스템 전체 사용량
"""

# Python에서 Windows의 C 언어 함수와 메모리 구조를 사용하기 위한 ctypes 가져오기
import ctypes
# 요청과 응답의 JSON 변환을 위한 표준 json 모듈 가져오기
import json
# 설정값이 NaN이나 무한대가 아닌 유한한 숫자인지 검사할 math 가져오기
import math
# 응답 수신을 별도 Python 프로세스에서 실행할 multiprocessing 가져오기
import multiprocessing
# 외부 명령 nvidia-smi를 실행하고 출력을 읽을 subprocess 가져오기
import subprocess
# 호출 제한과 경과 시간을 측정할 time 가져오기
import time
# 시작 및 종료 시각을 UTC 시간대와 함께 남길 날짜 도구 가져오기
from datetime import datetime, timezone
# 서버의 HTTP 오류 상태와 본문을 따로 읽기 위한 HTTPError 가져오기
from urllib.error import HTTPError
# HTTP 요청을 만들 Request와 실제 연결을 여는 urlopen 가져오기
from urllib.request import Request, urlopen


# 현재 컴퓨터의 Ollama 서버 주소 지정, localhost는 이 컴퓨터 자체를 의미
OLLAMA_URL = "http://localhost:11434"


# 요청과 기록을 일관된 JSON 문자열로 만드는 내부 함수 정의
def _json(value):
    """
    입력과 기록의 UTF-8 JSON 표현 통일

    입력: Ollama 요청 또는 수신 기록 객체
    반환: 한글을 보존한 간결한 JSON 문자열
    기준: 입력 키 순서를 유지하고 비정상 숫자의 전송 거부
    """
    # 한글을 그대로 유지하고 공백을 줄여 JSON으로 변환, NaN과 Infinity 전송 차단
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


# 서버 정보 조회와 명시적인 모델 해제에 사용할 공통 HTTP 함수 정의
def get_ollama_info(path="/api/tags", body=None):
    """
    Ollama 설치 정보 조회 또는 모델 해제 요청

    입력: 로컬 API 경로, 선택적인 POST 본문
    반환: JSON으로 해석한 서버 응답
    시간 제한: 요청당 20초
    효과: 조회 또는 명시적인 모델 해제 요청, 서버 접속 오류의 호출자 전달
    """
    # 본문이 있으면 JSON을 UTF-8 바이트로 변환, 없으면 조회용 None 사용
    data = _json(body).encode("utf-8") if body is not None else None
    # 서버 주소와 경로를 연결해 요청 객체 생성, data가 있으면 기본 POST이고 없으면 GET인 구성
    request = Request(OLLAMA_URL + path, data=data, headers={"Content-Type": "application/json"})
    # 20초 제한으로 연결 열기, with 블록 종료 시 연결 자동 정리
    with urlopen(request, timeout=20) as response:
        # 응답 본문의 JSON을 Python 객체로 읽어 반환
        return json.load(response)


# 자식 프로세스에서 서버의 응답 줄을 읽어 부모에게 전달하는 함수 정의
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
    # 서버 연결과 수신 중의 오류를 전달하기 위한 예외 처리 시작
    try:
        # 대화 생성 경로 /api/chat에 모델과 메시지를 JSON 바이트로 전송할 요청 구성
        request = Request(OLLAMA_URL + "/api/chat", data=_json(payload).encode("utf-8"),
                          # 본문이 JSON임을 서버에 알리는 Content-Type 헤더 지정
                          headers={"Content-Type": "application/json"})
        # 서버 수신 제한을 적용한 연결 열기, 부모의 전체 시간 제한과 별도로 적용
        with urlopen(request, timeout=timeout) as response:
            # 스트림 응답에서 도착한 바이트 줄을 순서대로 읽기
            for line in response:
                # UTF-8로 해석한 한 줄을 line 이벤트와 함께 프로세스 연결 통로로 전달
                connection.send(("line", line.decode("utf-8")))
        # 응답을 끝까지 읽었음을 end 이벤트로 알림, 정상 모델 종료 여부는 부모에서 별도 검사
        connection.send(("end", None))
    # 서버가 오류 HTTP 상태로 응답한 경우 별도 처리
    except HTTPError as error:
        # HTTP 오류 분류와 상태 코드를 부모에게 보낼 사전 구성
        connection.send(("error", {"type": "http", "status": error.code,
                                  # 오류 본문도 보존, 깨진 UTF-8 문자는 대체 문자로 읽어 오류 전달 자체의 실패 방지
                                  "detail": error.read().decode("utf-8", errors="replace")}))
    # HTTP 상태 오류 외의 연결 및 처리 오류 수신
    except Exception as error:
        # 예외 종류와 설명을 문자열로 변환해 error 이벤트 전달
        connection.send(("error", {"type": type(error).__name__, "detail": str(error)}))
    # 성공 여부와 무관하게 연결을 정리하는 finally 구간
    finally:
        # 자식이 사용한 프로세스 간 전송 통로 닫기
        connection.close()


# Windows 메모리 조회 함수가 채울 C 구조체와 대응하는 클래스 정의
class _MemoryStatus(ctypes.Structure):
    """Windows가 제공하는 전체 물리 메모리의 조회 구조"""
    # 구조체 크기와 메모리 부하율에 대응하는 32비트 부호 없는 필드 정의
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                # 전체 및 사용 가능한 물리 메모리를 바이트로 받을 64비트 필드 정의
                ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                # 페이지 파일 관련 전체 및 가용 용량 필드 정의, Windows 구조체 배치 유지 목적
                ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                # 가상 주소 공간의 전체 및 가용 용량 필드 정의
                ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                # 확장 가상 메모리 필드까지 Windows 함수가 요구하는 구조체 순서 완성
                ("extended", ctypes.c_ulonglong)]


# 호출 중 한 시점의 GPU와 시스템 자원 표본을 얻는 함수 정의
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
    # GPU 메모리와 사용률을 미측정 의미인 None으로 초기화
    sample = {"gpu_used_mib": None, "gpu_utilization_percent": None,
              # 시스템 사용 RAM과 가용 RAM의 미측정값 초기화
              "ram_used_mib": None, "ram_available_mib": None,
              # CPU 유휴 및 전체 누적 시간과 사용률 초기화, 실패 시 0으로 대체하지 않는 구성
              "cpu_idle_ticks": None, "cpu_total_ticks": None, "cpu_utilization_percent": None}
    # GPU 도구가 없거나 실패해도 모델 응답 수신은 유지하기 위한 예외 처리
    try:
        # 외부 명령을 실행하고 종료 코드 및 출력 문자열을 받는 작업 시작
        output = subprocess.run(
            # nvidia-smi에서 GPU 메모리 사용량과 연산 사용률 두 항목 요청
            ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
             # 헤더와 단위 문자를 생략한 쉼표 구분 형식으로 숫자 출력 요청
             "--format=csv,noheader,nounits"],
            # 출력을 화면 대신 변수로 받고 텍스트로 해석, 명령 자체의 제한은 2초
            capture_output=True, text=True, timeout=2,
            # Windows에서 별도 콘솔 창을 띄우지 않는 플래그 사용, 없으면 0 사용
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        # nvidia-smi 실행 인자 구성 종료
        )
        # 종료 코드 0인 정상 실행의 출력만 숫자로 해석
        if output.returncode == 0:
            # 여백을 제거하고 첫 GPU의 출력 줄을 선택한 뒤 쉼표로 항목 분리
            values = output.stdout.strip().splitlines()[0].split(",")
            # 첫 항목을 실수로 변환해 GPU 전체 메모리 사용량 MiB 기록
            sample["gpu_used_mib"] = float(values[0])
            # 둘째 항목을 실수로 변환해 GPU 연산 사용률 퍼센트 기록
            sample["gpu_utilization_percent"] = float(values[1])
    # 도구 없음, 숫자 해석 실패, 빈 출력이나 명령 시간 초과 처리
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        # 해당 측정을 건너뛰고 None 유지, 모델 호출 실패로 바꾸지 않는 처리
        pass
    # Windows 물리 메모리 조회를 위한 독립 예외 처리 시작
    try:
        # Windows API가 채울 메모리 구조체 인스턴스 생성
        memory = _MemoryStatus()
        # API가 구조체 크기를 알 수 있도록 실제 바이트 크기 설정
        memory.length = ctypes.sizeof(memory)
        # 구조체 주소를 Windows 함수에 전달하고 성공 여부 확인
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            # 전체 메모리에서 가용 메모리를 빼고 1,048,576으로 나누어 사용 RAM을 MiB로 변환
            sample["ram_used_mib"] = (memory.total - memory.available) / 1048576
            # 사용 가능한 물리 메모리도 바이트에서 MiB로 변환
            sample["ram_available_mib"] = memory.available / 1048576
    # Windows 함수가 없거나 OS 조회 실패 시 처리
    except (AttributeError, OSError):
        # RAM 측정 불가를 None으로 유지한 채 나머지 측정 진행
        pass
    # 시스템 CPU 누적 시간 조회를 위한 예외 처리 시작
    try:
        # 유휴, 커널과 사용자 CPU 누적 시간을 받을 64비트 저장 공간 세 개 생성
        idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
        # 유휴 및 커널 시간 저장 공간의 주소를 GetSystemTimes에 전달
        if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                # 사용자 시간 저장 공간까지 전달한 후 Windows 조회 성공 여부 확인
                                                ctypes.byref(user)):
            # 유휴 누적 시간 값을 Python 사전에 저장
            sample["cpu_idle_ticks"] = idle.value
            # 유휴 시간을 포함하는 커널 시간과 사용자 시간을 더해 전체 누적 시간 저장
            sample["cpu_total_ticks"] = kernel.value + user.value
    # CPU 조회 기능 부재 또는 OS 오류 처리
    except (AttributeError, OSError):
        # CPU 측정값을 None으로 유지하고 다른 표본 결과 보존
        pass
    # 가능한 항목만 채운 현재 시점의 자원 표본 반환
    return sample


# Ollama 요청 본문 생성 함수 정의, * 뒤의 생성 설정은 이름을 붙여 전달하는 인자
def build_ollama_request(model, messages, *, num_ctx=32768, num_predict=2048,
                         # 추론과 출력 형식 및 추가 옵션의 기본값 지정, None은 해당 옵션 미지정 의미
                         thinking=None, output_format=None, temperature=0.0, extra_options=None):
    """
    로컬 API에 보낼 본문 구성, 호출 전 저장과 실제 전송의 공통 사용

    주의사항:
        - 입력 초과 시 자동 잘림과 생성 중 context 이동의 비활성화
        - 서버의 지원 여부는 별도 예비 검사로 확인
        - 모든 실험에서 추론 원문을 후속 대화에 자동 삽입하지 않는 구성

    입력: 모델, 대화와 문맥 및 생성 설정
    반환: 호출 전 저장과 실제 전송에 공통 사용할 본문
    설정: 범용 기본 문맥값과 실험별 문맥값의 구분, 저장된 계획의 num_ctx 적용
    제한: 추가 옵션으로 문맥과 생성 한도 등의 덮어쓰기 차단
    """
    # 모델과 메시지 지정, 스트리밍 사용과 응답 후 1분 적재 유지를 요청
    payload = {"model": model, "messages": messages, "stream": True, "keep_alive": "1m",
               # 입력 자동 잘림과 문맥 이동을 사용하지 않도록 요청 옵션 지정
               "truncate": False, "shift": False,
               # 문맥 창과 최대 생성 토큰 수를 options 하위 사전에 저장
               "options": {"num_ctx": num_ctx, "num_predict": num_predict,
                           # 샘플링 온도와 난수 시드 42 지정, 시드만으로 모든 환경의 동일 응답 보장 불가
                           "temperature": temperature, "seed": 42}}
    # 실험 계획에서 정한 반복 억제와 토큰 선택 설정의 명시적 전달
    # 문맥 크기와 생성 한도 등을 extra_options로 우회 변경하지 않는 제한
    # 별도 생성 옵션을 전달한 경우에만 허용 범위 검사 수행
    if extra_options is not None:
        # 등장 및 빈도 기반 반복 페널티의 허용 범위를 -2~2로 지정
        bounds = {"presence_penalty": (-2, 2), "frequency_penalty": (-2, 2),
                  # 반복 페널티와 top_k는 0 이상, None은 이 검사에서 별도 상한이 없다는 의미
                  "repeat_penalty": (0, None), "top_k": (0, None),
                  # top_p와 min_p의 확률 범위를 0~1로 설정
                  "top_p": (0, 1), "min_p": (0, 1)}
        # 추가 옵션이 사전인지 확인하고 허용 목록 밖의 옵션 이름 검출
        if not isinstance(extra_options, dict) or set(extra_options) - set(bounds):
            # 알 수 없는 옵션이나 다른 자료형을 오류로 처리
            raise ValueError("지원하지 않는 추가 생성 설정")
        # 추가 옵션의 이름과 값을 한 쌍씩 검사
        for name, value in extra_options.items():
            # 현재 옵션에 대응하는 하한과 상한 튜플을 두 변수에 분리
            lower, upper = bounds[name]
            # 정수 또는 실수이면서 유한한 값인지 확인, bool과 NaN 및 Infinity 제외
            if type(value) not in (int, float) or not math.isfinite(value):
                # 계산 가능한 숫자가 아닌 생성 설정의 전송 차단
                raise ValueError("생성 설정은 유한한 숫자 필요")
            # 하한 미만 또는 지정된 상한 초과 여부 확인
            if value < lower or (upper is not None and value > upper):
                # 범위를 벗어난 옵션값 오류 발생
                raise ValueError("생성 설정 범위 초과")
            # 후보 개수 top_k에 실수가 전달됐는지 확인
            if name == "top_k" and type(value) is not int:
                # top_k에 정수 사용을 요구하는 오류 발생
                raise ValueError("top_k는 정수 필요")
            # 누적 확률 top_p가 0인지 추가 검사
            if name == "top_p" and value == 0:
                # top_p는 0보다 커야 한다는 조건 위반 처리
                raise ValueError("top_p는 0 초과 필요")
        # 검증된 추가 생성 옵션을 요청의 options 사전에 반영
        payload["options"].update(extra_options)
    # 추론 옵션이 생략되지 않은 경우에만 요청에 추가
    if thinking is not None:
        # True는 추론 활성, False는 비활성이라는 요청값 전달
        payload["think"] = thinking
    # JSON 등의 출력 형식을 지정한 경우에만 요청에 추가
    if output_format is not None:
        # 출력 스키마 또는 형식 문자열을 Ollama format 필드에 저장
        payload["format"] = output_format
    # 서버로 보낼 완성된 본문 사전 반환, 이 함수 자체의 네트워크 호출 없음
    return payload


# 모델 한 번의 호출과 시간 및 자원 측정을 담당하는 함수 정의
def call_ollama(model, messages, *, num_ctx=32768, num_predict=2048,
                # 추론 설정, 출력 구조, 전체 제한과 샘플링 옵션의 기본값 지정
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
        dict: 실제 요청, 최종 내용과 추론, 종료 상태와 측정값

    주의사항:
        - 동일 정보와 동일 토큰 수는 다른 조건, 모델별 실측 토큰 수 기록
        - API 정상 종료와 사실 정확성은 별도 판정
        - 호출 제한 시간은 부모에서 적용, 종료 및 정리 비용은 별도 발생 가능
        - keep_alive로 적재 상태 유지 후 관측, 마지막에 해당 모델 해제
        - 다중 대화는 호출자가 messages에 필요한 최종 응답 이력 제공
        - 별도 프로세스 사용으로 실행 진입점의 __main__ 보호 필요

    입력: 모델 태그와 대화, 출력 형식 및 시간과 생성 제한
    반환: 요청, 최종 응답과 추론, 서버 측정값과 관측한 자원 최댓값
    측정: 수신 프로세스 시작부터 응답 수신까지의 wall_seconds
    효과: 모델 1회 호출과 해제, CSV 저장은 상위 실행기의 별도 수행
    """
    # 문맥, 생성량과 시간 제한 중 0 이하의 값이 있는지 확인
    if num_ctx <= 0 or num_predict <= 0 or timeout <= 0:
        # 실행할 수 없는 한도 설정을 요청 전 오류로 처리
        raise ValueError("context, 생성 한도와 시간 제한은 양수 필요")

    # ------------------------------ * 1. 요청과 응답 기록 준비 * ------------------------------
    # 모델과 메시지 및 문맥과 생성 한도를 공통 요청 구성 함수에 전달
    payload = build_ollama_request(model, messages, num_ctx=num_ctx, num_predict=num_predict,
                                   # 추론 여부와 출력 스키마 전달
                                   thinking=thinking, output_format=output_format,
                                   # 온도와 추가 생성 옵션까지 전달해 실제 payload 생성
                                   temperature=temperature, extra_options=extra_options)
    # 측정 시작 시각의 기록, 관리용 호출 ID 생성 없음
    # 호출 시작을 UTC ISO 문자열로 기록, 시간 측정용 기준과 구분
    started_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    # 이번 호출 결과를 담을 사전에 시작 시각 지정
    record = {"started_at": started_at,
              # 실제로 전송할 본문을 보존해 상위 실행기의 요청 일치 검사에 사용
              "request": payload,
              # 진행 중 상태와 빈 오류 목록 초기화
              "status": "running", "errors": [], 
              # 최종 답변과 추론은 빈 문자열, 아직 없는 서버 통계와 적재 정보는 None으로 초기화
              "content": "", "thinking": "", "response_metadata": None, "loaded_models": None}

    # ------------------------------ * 2. 전체 시간 제한과 자원 관측 * ------------------------------
    # Windows에서 새 Python 인터프리터를 시작하는 spawn 방식의 프로세스 환경 선택
    context = multiprocessing.get_context("spawn")
    # 단방향 Pipe 생성, 부모 수신용 receiver와 자식 송신용 sender를 분리
    receiver, sender = context.Pipe(duplex=False)
    # 자식이 실행할 함수와 인자 지정, Process 생성만으로는 아직 호출하지 않는 상태
    process = context.Process(target=_receive_chat, args=(payload, sender, timeout))
    # CPU 표본 간 차이를 계산하기 위해 호출 전 자원 상태 확보
    baseline = _sample_resources()
    # 호출 중 표본은 최댓값 계산에만 사용, 스트림과 설치 정보의 중복 복사 제외
    # 호출 중 수집할 자원 표본의 빈 리스트 생성
    samples = []
    # 전체 경과 시간의 시작 기준 확보
    started = time.perf_counter()
    # 자식 프로세스를 실제 시작하여 서버 호출과 스트림 수신 진행
    process.start()
    # 부모가 가진 송신 통로 복사본 닫기, 실제 송신은 자식이 담당
    sender.close()
    # 첫 자원 측정을 바로 수행하도록 다음 표본 시각을 시작값으로 지정
    next_sample = started
    # 직전 표본을 호출 전 상태로 초기화
    previous_sample = baseline
    # 최종 답변 첫 조각이 아직 도착하지 않았음을 None으로 표시
    first_content = None
    # 추론 또는 답변의 첫 조각이 아직 도착하지 않았음을 None으로 표시
    first_token = None
    # 서버의 done=true 종료 표식을 아직 받지 않았다는 상태 지정
    done = False
    # 수신 처리에 문제가 생겨도 프로세스와 모델을 정리하기 위한 try 구간
    try:
        # 종료 조건이 발생해 break를 실행할 때까지 반복 수신
        while True:
            # 현재 경과 시간 기준값 조회
            now = time.perf_counter()
            # 수신 대기와 로딩을 포함한 전체 경과 시간이 제한에 도달했는지 확인
            if now - started >= timeout:
                # 시간 초과 상태로 기록
                record["status"] = "timeout"
                # 수신 반복을 끝내고 finally의 정리 단계로 이동
                break
            # 다음 자원 표본을 수집할 시점인지 확인
            if now >= next_sample:
                # 현재 GPU와 시스템 메모리 및 CPU 시간 조회
                sample = _sample_resources()
                # 표본 사이 시스템 전체 CPU 사용률, 모델 전용 수치와 구분
                # 현재와 직전 표본의 CPU 누적 시간이 모두 있을 때만 차이 계산
                if sample.get("cpu_total_ticks") is not None and previous_sample.get("cpu_total_ticks") is not None:
                    # 두 관측 사이에 누적된 전체 CPU 시간 계산
                    total = sample["cpu_total_ticks"] - previous_sample["cpu_total_ticks"]
                    # 같은 구간의 유휴 CPU 시간 계산
                    idle = sample["cpu_idle_ticks"] - previous_sample["cpu_idle_ticks"]
                    # 전체 시간 차이가 양수일 때만 비율을 계산해 0으로 나누는 오류 방지
                    if total > 0:
                        # 1-유휴비율로 CPU 사용률 계산, 관측값을 0~100 범위로 제한
                        sample["cpu_utilization_percent"] = max(0.0, min(100.0, 100 * (1 - idle / total)))
                # 현재 표본을 다음 차이 계산의 기준으로 갱신
                previous_sample = sample
                # 표본이 호출 시작 후 몇 초에 관측됐는지 기록
                sample["elapsed_seconds"] = time.perf_counter() - started
                # 완성된 표본을 리스트에 추가해 종료 후 최댓값 계산에 사용
                samples.append(sample)
                # 측정 완료 후 약 1초가 지나면 다음 표본을 수집하도록 예약
                next_sample = time.perf_counter() + 1.0
            # 최대 0.05초 동안 수신 통로에 읽을 이벤트가 있는지 확인
            if receiver.poll(0.05):
                # 통로가 갑자기 닫힌 경우를 구분하기 위한 예외 처리
                try:
                    # 자식이 보낸 이벤트 이름과 실제 내용을 두 변수로 분리
                    event, value = receiver.recv()
                # 이벤트를 받기 전에 통로가 끝난 경우 EOFError 처리
                except EOFError:
                    # 통로 종료 시 더 이상 기다리지 않고 수신 반복 종료
                    break
                # 서버의 한 줄 원문을 전달받은 이벤트인지 확인
                if event == "line":
                    # 개별 스트림 줄을 해석하는 중의 오류도 보존하기 위한 try 구간
                    try:
                        # 수신한 한 줄 JSON을 딕셔너리로 변환
                        chunk = json.loads(value)
                        # 메시지 부분 조회, 없는 경우 빈 사전 사용
                        message = chunk.get("message", {})
                        # 이번 조각의 최종 답변 문자열 조회, 없는 경우 빈 문자열 사용
                        content = message.get("content", "")
                        # 이번 조각의 추론 문자열 조회, 최종 답변과 별도 보관
                        thinking_text = message.get("thinking", "")
                        # 추론 또는 답변이 처음 도착한 시점인지 확인
                        if (content or thinking_text) and first_token is None:
                            # 호출 시작부터 첫 내용 도착까지의 지연 기록, 서버 토큰 단위 도착 시각과는 구분
                            first_token = time.perf_counter() - started
                        # 추론을 제외한 최종 답변 내용이 처음 도착했는지 확인
                        if content and first_content is None:
                            # 최종 답변 첫 조각까지의 지연 기록
                            first_content = time.perf_counter() - started
                        # 최종 답변 조각을 앞서 받은 문자열 뒤에 순서대로 연결
                        record["content"] += content
                        # 추론 조각도 별도 문자열에 순서대로 연결
                        record["thinking"] += thinking_text
                        # 서버가 스트림 본문에 오류 항목을 반환했는지 확인
                        if chunk.get("error"):
                            # 서버 오류 내용을 오류 목록에 보존
                            record["errors"].append({"type": "server", "detail": chunk["error"]})
                        # 현재 줄에 정상 스트림 종료 표식 done이 있는지 확인
                        if chunk.get("done"):
                            # 종료 응답의 항목을 이름과 값으로 순회해 서버 통계 구성
                            record["response_metadata"] = {key: val for key, val in chunk.items()
                                                           # 이미 누적한 message를 제외하고 토큰 수, 소요 시간과 종료 사유 등의 항목만 보관
                                                           if key != "message"}
                            # 종료 표식을 받았다는 상태 갱신
                            done = True
                            # 마지막 응답을 확보했으므로 수신 반복 종료
                            break
                    # 잘못된 JSON이나 예상과 다른 스트림 자료형의 오류 처리
                    except (ValueError, TypeError, AttributeError) as error:
                        # 해석 실패 이유와 문제의 원문 줄을 함께 보존
                        record["errors"].append({"type": "stream", "detail": str(error), "raw": value})
                # 자식이 네트워크 또는 HTTP 오류를 보낸 경우 분기
                elif event == "error":
                    # 자식이 정리한 오류 사전을 결과에 추가
                    record["errors"].append(value)
                    # 오류 수신 후 응답 대기 종료
                    break
                # 한 줄이나 오류가 아닌 end 등 종료 이벤트 처리
                else:
                    # 수신 반복 종료, 최종 성공 여부는 다음 단계에서 판정
                    break
            # 읽을 이벤트가 없고 자식 프로세스도 종료됐는지 확인
            elif not process.is_alive():
                # 더 이상 수신될 응답이 없는 상태에서 반복 종료
                break
        # 이미 시간 초과로 분류한 결과는 덮어쓰지 않고 나머지 상태만 판정
        if record["status"] != "timeout":
            # 정상 종료 표식 없이 끝난 연결의 명시적 분류, 부분 응답의 성공 처리 방지
            # 종료 표식도 없고 별도 오류도 기록되지 않은 연결 종료 확인
            if not done and not record["errors"]:
                # 불완전한 스트림이라는 오류 유형 추가
                record["errors"].append({"type": "incomplete_stream",
                                         # done=true가 없는 상태로 통로가 끝났다는 구체적 이유 보존
                                         "detail": "done=true 표식 없이 응답 통로 종료"})
            # 종료 표식 수신과 오류 부재를 모두 만족할 때만 completed 지정
            record["status"] = "completed" if done and not record["errors"] else "failed"
    # 수신 성공 및 실패와 관계없이 실행할 정리 구간 시작
    finally:
        # 프로세스와 모델 정리 전에 전체 응답 경과 시간 확정
        record["wall_seconds"] = time.perf_counter() - started
        # 추론 또는 답변 첫 조각 지연을 기록에 보관
        record["first_token_seconds"] = first_token
        # 최종 답변 첫 조각 지연을 별도 필드에 보관
        record["first_content_seconds"] = first_content
        # 자식 수신 프로세스가 아직 동작 중인지 확인
        if process.is_alive():
            # 남아 있는 수신 프로세스 종료 요청, 서버 모델 해제와는 별도 동작
            process.terminate()
        # 자식 프로세스 종료를 최대 5초 기다리며 자원 회수
        process.join(timeout=5)
        # 부모의 수신 통로 닫기
        receiver.close()
        # 모델 적재량 조회가 실패해도 이미 받은 응답을 보존하기 위한 예외 처리
        try:
            # 모델 해제 전에 Ollama의 적재 모델 정보를 조회해 GPU 적재량 기록
            record["loaded_models"] = get_ollama_info("/api/ps")
        # 적재 정보 조회 실패 처리
        except Exception as error:
            # 자원 조회 오류를 결과에 별도 기록
            record["errors"].append({"type": "resource_query", "detail": str(error)})
        # 호출 후 모델 해제 요청의 예외 처리 시작
        try:
            # keep_alive 0으로 해당 모델의 메모리 해제를 요청, 새 답변 생성용 프롬프트 없음
            get_ollama_info("/api/generate", {"model": model, "keep_alive": 0})
        # 모델 해제 요청 실패 처리
        except Exception as error:
            # 해제 실패를 상위 실행기가 확인해 다음 호출을 중단할 수 있도록 기록
            record["errors"].append({"type": "unload", "detail": str(error)})

    # ------------------------------ * 3. 관측 결과 요약 * ------------------------------
    # GPU 메모리와 사용률, RAM 및 CPU 사용률을 항목별로 집계
    for field in ("gpu_used_mib", "gpu_utilization_percent", "ram_used_mib", "cpu_utilization_percent"):
        # 측정된 값만 추려 리스트 구성, 조회 실패인 None은 집계 제외
        values = [sample[field] for sample in samples if sample.get(field) is not None]
        # 값이 있으면 관측 최댓값 저장, 전부 미측정이면 None 유지
        record["peak_" + field] = max(values) if values else None
    # 서버 최종 통계가 없을 때도 get 조회가 가능하도록 빈 사전 사용
    metadata = record["response_metadata"] or {}
    # 호출 절차 완료와 실제 답변 완성을 구분하는 최종 판정식 구성 시작
    record["final_response_complete"] = (
        # 호출 상태 completed와 종료 사유 stop을 모두 요구
        record["status"] == "completed" and metadata.get("done_reason") == "stop"
        # 공백을 제외한 최종 답변이 실제로 있는지도 확인
        and bool(record["content"].strip())
    # 세 조건을 결합한 최종 답변 완료 여부 계산 종료
    )
    # 호출 후 정리까지 끝난 시각을 UTC로 기록, wall_seconds의 측정 범위와 구분
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    # 요청, 응답과 추론, 오류 및 측정 결과를 상위 실행기에 반환
    return record
