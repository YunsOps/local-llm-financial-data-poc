"""호출 한 번의 입력, 출력, 측정값과 채점란을 한 행에 저장하는 CSV 모듈.

입력 사전의 각 값은 경로별 열로 분리, 배열은 1부터 시작하는 순번으로 구분.
관리용 식별값과 중복 원본 JSON 제외, 실제 입력 및 모델 응답 문장의 수정 없음.
형식과 거래 규칙만 자동 검사, 사실 설명의 판정과 점수는 별도 작성 대상.
"""

# 딕셔너리의 값을 CSV 열로 읽고 쓰기 위한 csv 모듈 가져오기
import csv
# 메모리에 있는 문자열을 파일처럼 읽기 위한 io 모듈 가져오기
import io
# JSON 문자열과 Python 자료 사이의 변환을 위한 json 모듈 가져오기
import json
# 파일 저장 완료와 파일 교체 등 운영체제 기능을 사용하기 위한 os 모듈 가져오기
import os
# 기존 CSV를 교체하기 전에 임시 파일을 만들기 위한 tempfile 모듈 가져오기
import tempfile
# 날짜 해석, 시간 차이 계산, 시간대 지정을 위한 날짜 관련 클래스 가져오기
from datetime import datetime, timedelta, timezone
# 파일과 폴더의 경로를 객체로 다루기 위한 Path 가져오기
from pathlib import Path

# 모델의 응답 문자열을 JSON 객체로 해석하는 같은 패키지의 함수 가져오기
from .validate_response import parse_response


# Excel의 셀 길이 제한보다 작은 단위로 같은 행의 오른쪽 열에 이어서 보존
# 긴 응답을 여러 셀로 나누기 위한 셀당 최대 문자 수 30,000자 지정
TEXT_LIMIT = 30000
# 모델의 최종 답변에서 각각 별도 열로 저장할 결정과 매수 및 매도 비율 필드 지정
OUTPUT_FIELDS = ["decision", "buy_allocation_percentage", "sell_allocation_percentage",
                 # 최종 답변의 판단 근거와 과거 기록 검토 필드를 출력 열 목록에 포함
                 "reason", "reflection_log"]
# 문맥 크기, 생성 한도, 추론 여부와 토큰 선택 설정을 저장할 열 이름 지정
SETTING_FIELDS = ["num_ctx", "num_predict", "think", "temperature", "top_p", "top_k", "min_p",
                  # 반복 억제 설정, 난수 초기값과 호출 제한 시간을 설정 열 목록에 포함
                  "presence_penalty", "frequency_penalty", "repeat_penalty", "seed", "timeout_seconds"]
# 입력, 출력, 캐시 및 추론 토큰 수를 저장할 측정 열 이름 지정
METRIC_FIELDS = ["input_tokens", "generated_tokens", "cached_input_tokens", "reasoning_tokens",
                 # 전체 요청, 서버 처리, 모델 로딩, 입력 처리와 생성에 걸린 시간의 열 이름 지정
                 "wall_seconds", "server_seconds", "load_seconds", "input_seconds", "generation_seconds",
                 # 초당 생성량과 첫 토큰 및 첫 최종 답변 도착 시간의 열 이름 지정
                 "tokens_per_second", "first_token_seconds", "first_content_seconds",
                 # GPU 메모리, 모델 적재량과 GPU 사용률의 열 이름 지정
                 "gpu_peak_mib", "model_gpu_mib", "model_total_mib", "gpu_peak_percent",
                 # 시스템 메모리, CPU 사용률과 API 비용의 열 이름 지정
                 "ram_peak_mib", "cpu_peak_percent", "cost_usd"]
# JSON 구조, 거래 규칙, 300초 제한 검사 결과와 설명 점수를 저장할 열 이름 지정
REVIEW_FIELDS = ["json_valid", "trading_valid", "json_within_300_seconds", "score"]
# 응답마다 평가하는 세 항목에 맞추어 1부터 3까지 차례대로 반복
for number in range(1, 4):
    # 현재 항목 번호와 세부 이름을 조합한 채점 열 이름을 목록에 추가
    REVIEW_FIELDS += [f"review_{number}_{name}" for name in
                      # 기대 설명, 입력 경로, 판정, 점수, 응답 인용과 판정 이유를 각 항목의 세부 열로 지정
                      ("expected", "source_paths", "verdict", "score", "quote", "rationale")]


# 모델과 시험 조건 및 시작 시각을 받아 영어 CSV 파일명을 만드는 함수 정의
def csv_filename(model, condition, language, phase, num_predict, started_at):
    """영문 모델명과 시험명, 한국 표준시 시작 시각으로 파일명 구성.

    시간대가 포함된 시작 시각을 UTC+09:00으로 변환, 임의 ID나 순번 추가 없음.
    기본 추론의 생성 한도와 워밍업, 대표 문항 점검 및 재실행의 파일명 구분.
    같은 초의 이름 충돌은 TrialCsv 생성 시 예외 처리, 기존 파일 덮어쓰기 방지.
    """
    # Cloud 조건이면 파일명에 cloud-nonthinking을 사용하고 나머지는 받은 조건 이름 사용
    name = ("cloud-nonthinking" if condition == "cloud" else condition)
    # 입력 언어 코드 en 또는 ko를 영어 단어로 바꾸어 시험 이름에 연결
    name += "-" + {"en": "english", "ko": "korean"}[language]
    # 기본 설정 추론 시험인지 확인하여 생성 한도를 파일명에 포함할 필요 판단
    if condition == "basic-thinking":
        # 생성 한도가 다른 기본 추론 시험을 구분하기 위한 토큰 한도 문구 추가
        name += f"-tokens{num_predict}"
    # 본 시험이 아닌 워밍업이나 점검 실행인지 확인
    if phase != "main":
        # 실행 목적을 파일명용 영어 표현으로 바꾸어 시험 이름에 연결
        name += "-" + {"warmup": "warmup", "preflight": "completion-check", "extension": "rerun"}[phase]
    # ISO 형식의 시작 시각 문자열을 날짜와 시간을 다룰 수 있는 datetime 객체로 변환
    started = datetime.fromisoformat(started_at)
    # 시작 시각에 UTC와의 시간 차이 정보가 없는지 확인
    if started.utcoffset() is None:
        # 시간대를 알 수 없어 파일명 시각을 일관되게 만들 수 없는 경우 오류 발생
        raise ValueError("시간대가 있는 시험 시작 시각 필요")
    # 시작 시각을 한국 표준시로 변환한 뒤 연월일-시분초 형식의 문자열 생성
    stamp = started.astimezone(timezone(timedelta(hours=9))).strftime("%Y%m%d-%H%M%S")
    # 모델 태그의 콜론을 하이픈으로 바꾸고 시험 이름과 시각을 이어 CSV 파일명 반환
    return f"{model.replace(':', '-')}_{name}_{stamp}.csv"


# Python 자료를 줄바꿈 없는 JSON 문자열로 변환하는 공통 함수 정의
def json_text(value):
    """콘솔 출력 및 JSON 자료형의 표기를 위한 직렬화, NaN과 Infinity 거부."""
    # 한글을 그대로 유지하고 비표준 숫자는 거부하며 불필요한 구분 공백을 없앤 JSON 반환
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# 중첩된 입력 자료를 경로별 CSV 열로 펼치는 함수 정의, 기본 열 이름 접두어로 input 사용
def flatten_input(value, prefix="input"):
    """입력의 각 값을 원래 키 경로에 대응하는 영어 열로 분리.

    예: market_input.ohlcv.day[0].close → input_market_input_ohlcv_day_1_close.
    배열 순서는 1부터 시작, count 열에 배열 길이를 기록하여 빈 배열도 보존.
    명시적인 null은 문자열 null, 빈 사전은 {}로 표시하여 없는 셀과 구분.
    입력의 주문 번호 등 실제 자료는 보존, 실행 관리용 ID를 추가하지 않는 구성.
    """
    # 현재 값이 비어 있지 않은 딕셔너리인지 확인
    if isinstance(value, dict) and value:
        # 현재 딕셔너리의 하위 값을 펼쳐 담을 빈 결과 딕셔너리 생성
        result = {}
        # 딕셔너리의 각 키와 그에 대응하는 값을 하나씩 꺼내 반복
        for key, child in value.items():
            # 현재 경로 뒤에 키를 붙이고 같은 함수를 다시 호출하여 더 깊은 자료까지 펼치기
            nested = flatten_input(child, f"{prefix}_{key}")
            # 이미 만든 열 이름과 새로 만든 열 이름의 교집합이 있는지 확인
            if result.keys() & nested.keys():
                # 서로 다른 입력 경로가 같은 열 이름이 되어 덮어쓰일 수 있는 경우 오류 발생
                raise ValueError("입력 경로를 펼친 열 이름의 중복")
            # 하위 자료에서 만든 열과 값을 현재 결과 딕셔너리에 합치기
            result.update(nested)
        # 현재 딕셔너리의 모든 하위 자료를 펼친 결과 반환
        return result
    # 현재 값이 순서가 있는 목록인 리스트인지 확인
    if isinstance(value, list):
        # 리스트의 항목 수를 별도 count 열에 기록하는 결과 딕셔너리 생성
        result = {f"{prefix}_count": len(value)}
        # 리스트의 각 항목에 사람이 읽기 쉬운 1부터 시작하는 번호를 붙여 반복
        for index, child in enumerate(value, 1):
            # 항목 번호를 경로에 붙여 하위 값을 펼친 뒤 현재 결과에 합치기
            result.update(flatten_input(child, f"{prefix}_{index}"))
        # 리스트 길이와 각 항목의 개별 열이 담긴 결과 반환
        return result
    # 입력의 None은 null, 빈 딕셔너리는 {} 문자열로 보존하고 나머지 단일 값은 그대로 반환
    return {prefix: "null" if value is None else "{}" if value == {} else value}


# 한 행의 값을 CSV 셀에 저장할 형태로 바꾸고 긴 문자열을 분할하는 함수 정의
def _cells(row):
    """한 행의 셀 표현 통일과 긴 문자열의 가로 분할.

    측정되지 않은 None은 공란, bool은 true/false로 표기.
    30,000자를 넘는 원문은 같은 행의 field_part_2, field_part_3 열에 연속 보존.
    줄바꿈, 쉼표와 따옴표의 처리는 csv 모듈에 위임, 문장의 번역이나 요약 없음.
    """
    # 변환된 열 이름과 셀 값을 담을 빈 딕셔너리 생성
    cells = {}
    # 저장할 행의 각 열 이름과 값을 하나씩 꺼내 반복
    for name, value in row.items():
        # 현재 값이 값 없음 상태인 None인지 확인
        if value is None:
            # 측정되지 않았거나 아직 채점하지 않은 값을 나타내는 빈 셀 문자열 지정
            value = ""
        # 현재 값이 딕셔너리 또는 리스트인지 확인
        elif isinstance(value, (dict, list)):
            # 셀에 직접 저장하기 어려운 복합 자료를 JSON 문자열로 변환
            value = json_text(value)
        # 현재 값이 참 또는 거짓을 나타내는 bool인지 확인
        elif isinstance(value, bool):
            # 참과 거짓을 CSV에서 일관되게 읽을 수 있도록 소문자 영어 문자열로 변환
            value = "true" if value else "false"
        # 문자열 길이가 셀당 최대 문자 수를 넘는지 확인
        if isinstance(value, str) and len(value) > TEXT_LIMIT:
            # 긴 문자열을 최대 문자 수 단위로 나눌 시작 위치와 1부터 시작하는 조각 번호 생성
            for index, start in enumerate(range(0, len(value), TEXT_LIMIT), 1):
                # 첫 조각은 원래 열 이름을 쓰고 다음 조각부터 part 번호를 붙인 열 이름 생성
                key = name if index == 1 else f"{name}_part_{index}"
                # 현재 시작 위치부터 최대 문자 수만큼 문자열을 잘라 해당 열에 저장
                cells[key] = value[start:start + TEXT_LIMIT]
        # 긴 문자열 분할이 필요하지 않은 값에 대한 처리 분기
        else:
            # 변환된 값을 원래 열 이름의 셀에 저장
            cells[name] = value
    # CSV 한 행으로 저장할 수 있도록 정리한 셀 딕셔너리 반환
    return cells


# 저장된 CSV의 열 이름과 모든 행을 읽는 함수 정의
def read_rows(path):
    """UTF-8 BOM CSV의 열 목록과 호출별 행 읽기, 셀 안 줄바꿈 보존."""
    # UTF-8 BOM을 처리하고 CSV 줄바꿈을 유지하는 읽기 방식으로 파일 열기, 블록 종료 시 자동 닫기
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        # 첫 행의 열 이름을 각 행의 딕셔너리 키로 사용하는 CSV 읽기 객체 생성
        reader = csv.DictReader(handle)
        # 열 이름 목록과 남은 모든 행을 읽어 만든 리스트를 함께 반환
        return reader.fieldnames, list(reader)


# 여러 CSV 열로 나뉜 긴 문자열을 원래 순서로 합치는 함수 정의
def restore_text(row, field):
    """같은 행의 원문과 part_2 이후 열을 번호순으로 연결한 전체 문자열 반환."""
    # 첫 번째 열 값을 조각 목록에 넣고 해당 열이 없으면 빈 문자열 사용
    parts = [row.get(field, "")]
    # 두 번째 조각 열부터 확인하기 위한 번호 초기화
    index = 2
    # 현재 번호에 해당하는 추가 조각 열이 있는 동안 반복
    while f"{field}_part_{index}" in row:
        # 현재 조각 열의 문자열을 복원용 목록 끝에 추가
        parts.append(row[f"{field}_part_{index}"])
        # 다음 조각 열을 확인하기 위해 번호를 1 증가
        index += 1
    # 조각 사이에 문자를 끼우지 않고 이어 붙인 원래 문자열 반환
    return "".join(parts)


# 시험 하나의 CSV 생성과 결과 행 추가를 함께 관리하는 클래스 정의
class TrialCsv:
    """호출 한 번당 한 행의 결과 파일 생성과 원자적 저장.

    새 파일은 x 모드로 생성, 이후 호출은 최신 파일의 기존 행 뒤에 추가.
    사용자 채점과 추가 열 보존, 저장 실패 또는 동시 편집 감지 시 예외 전달.
    """

    # 새 CSV 객체 생성 시 저장 경로와 시험 정보로 열 구조를 준비하는 초기화 메서드 정의
    def __init__(self, path, trial):
        """선택 문항의 입력 열과 결과 열로 빈 CSV 생성, 모델 호출 전 이름 충돌 검사."""
        # 저장 경로를 Path 객체로 바꾸어 현재 객체의 path 속성에 보관
        self.path = Path(path)
        # 시험의 모든 문항에서 입력 열 이름을 수집하고 처음 등장한 순서를 유지한 채 중복 제거 시작
        input_fields = list(dict.fromkeys(key for case in trial.get("cases", [])
                                        # 각 문항 입력을 펼쳐 얻은 열 이름을 수집하여 최종 입력 열 목록 구성
                                        for key in flatten_input(case["input"])))
        # 시작 및 종료 시각, 공통 지시문과 입력 자료를 CSV 앞쪽 열로 배치
        self.fields = (["started_at", "finished_at", "system_instruction"] + input_fields
                       # 파싱한 출력, 추론 원문, 해석하지 못한 응답과 요청 설정을 뒤쪽 열로 연결
                       + OUTPUT_FIELDS + ["thinking", "unparsed_response"] + SETTING_FIELDS
                       # 측정값, 채점 칸과 오류 내용을 마지막 열로 연결하여 전체 열 순서 확정
                       + METRIC_FIELDS + REVIEW_FIELDS + ["error_message"])
        # 저장 폴더가 없으면 상위 폴더까지 생성하고 이미 있으면 그대로 사용
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 같은 파일이 있으면 실패하는 x 모드와 Excel에서 한글을 읽기 쉬운 UTF-8 BOM 인코딩으로 새 파일 열기
        with self.path.open("x", encoding="utf-8-sig", newline="") as handle:
            # 정해진 열 순서를 사용하여 CSV 첫 줄에 열 이름 기록
            csv.DictWriter(handle, fieldnames=self.fields).writeheader()
            # Python 내부의 쓰기 버퍼에 남은 내용을 운영체제로 전달
            handle.flush()
            # 파일 번호를 이용해 운영체제에 파일 내용의 저장 동기화 요청
            os.fsync(handle.fileno())

    # 기존 CSV의 내용과 채점 값을 유지하면서 결과 한 행을 추가하는 메서드 정의
    def append(self, row):
        """새 호출 행 추가, 긴 원문용 추가 열 생성과 기존 채점 내용 보존.

        최신 파일을 읽어 임시 파일 완성 후 교체, 미완성 파일의 노출 방지.
        저장 중 외부 변경 또는 열 손상 발견 시 중단, 다음 호출 진행 금지.
        동일 응답의 반복도 실제 별도 호출이므로 내용 중복을 이유로 제거하지 않는 기준.
        """
        # 추가 저장 전 CSV 파일 전체를 바이트로 읽어 기존 상태 보관
        original = self.path.read_bytes()
        # BOM을 제거해 문자열로 해석하고 메모리 파일을 통해 CSV 읽기 객체 생성
        reader = csv.DictReader(io.StringIO(original.decode("utf-8-sig"), newline=""))
        # 기존 열 이름과 기존 결과 행을 각각 읽어 보관
        headers, existing = reader.fieldnames, list(reader)
        # 열 이름 부재, 중복 열 또는 필수 열 삭제 여부 검사
        if (not headers or len(headers) != len(set(headers)) or not set(self.fields).issubset(headers)
                # 열 이름 수보다 값이 많아 이름 없는 열이 생긴 기존 행이 있는지 추가 검사
                or any(None in item for item in existing)):
            # CSV 구조가 예상과 달라 안전하게 추가하기 어려운 경우 저장 중단 오류 발생
            raise ValueError("CSV 필수 열 삭제 또는 손상, 저장 중단")
        # 새 결과 행의 복합 값과 긴 문자열을 CSV 셀 형태로 변환
        cells = _cells(row)
        # 기존에 없던 추가 출력이나 문자열 조각의 열 이름을 맨 뒤에 추가
        headers += [key for key in cells if key not in headers]
        # CSV와 같은 폴더에 임시 파일을 생성하고 열린 파일 번호와 경로를 각각 받기
        descriptor, temporary = tempfile.mkstemp(prefix=self.path.stem[:30], suffix=".tmp", dir=self.path.parent)
        # 임시 파일을 통한 저장과 교체를 수행하면서 마지막 정리 작업을 보장하는 구간 시작
        try:
            # 임시 파일 번호를 UTF-8 BOM 텍스트 쓰기 객체로 바꾸고 블록 종료 시 자동 닫기
            with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
                # 기존 열과 새 열을 모두 포함하는 CSV 쓰기 객체 생성
                writer = csv.DictWriter(handle, fieldnames=headers)
                # 임시 파일의 첫 줄에 전체 열 이름 기록
                writer.writeheader()
                # 기존 응답과 채점 내용이 담긴 모든 행을 먼저 기록
                writer.writerows(existing)
                # 현재 호출의 결과를 마지막 행으로 추가 기록
                writer.writerow(cells)
                # 임시 파일의 쓰기 버퍼 내용을 운영체제로 전달
                handle.flush()
                # 임시 파일의 내용이 저장되도록 운영체제에 동기화 요청
                os.fsync(handle.fileno())
            # 임시 파일을 작성하는 동안 원본 CSV가 다른 프로그램에서 변경되었는지 비교
            if self.path.read_bytes() != original:
                # 외부 수정 내용을 덮어쓰지 않도록 파일 교체를 중단하는 오류 발생
                raise RuntimeError("CSV 저장 중 외부 수정 감지, 재확인 필요")
            # 완성된 임시 파일로 원본 CSV를 교체하여 새 행이 포함된 결과 반영
            os.replace(temporary, self.path)
        # 저장 성공 여부와 관계없이 남은 임시 파일을 정리하는 구간 시작
        finally:
            # 교체 실패 등으로 임시 파일이 아직 남아 있는지 확인
            if os.path.exists(temporary):
                # 이번 저장에서 만든 임시 파일만 삭제
                os.unlink(temporary)


# 모델 호출 기록, 문항, 시험 조건과 자동 검사 결과를 CSV 한 행으로 구성하는 함수 정의
def result_row(record, case, trial, evaluation):
    """실제 요청과 응답, 측정값을 한 호출의 CSV 행으로 변환.

    지시문과 입력은 실제 요청 메시지에서 추출, 계획값으로 응답을 교정하지 않는 기준.
    정상 JSON은 다섯 출력 필드로 분리, 파싱 불가 원문은 unparsed_response에 보존.
    서버 시간의 ns→초, 모델 적재량의 byte→MiB 변환, 미측정값의 0 대체 없음.
    사실 채점은 공란 유지, 기대 설명과 입력 위치만 검토를 위해 함께 제공.
    """
    # 실제로 보낸 요청 딕셔너리를 읽고 기록이 없으면 빈 딕셔너리 사용
    request = record.get("request") or {}
    # Ollama의 messages 또는 OpenAI의 input에서 실제 전송한 대화 목록 읽기
    messages = request.get("messages", request.get("input", []))
    # 시스템 역할의 메시지에서 공통 지시문을 찾고 없으면 시험에 보관된 지시문 사용
    system = next((m["content"] for m in messages if m.get("role") == "system"), trial.get("instruction", ""))
    # 사용자 역할의 메시지에서 실제 입력 JSON 문자열을 찾고 없으면 None 사용
    user = next((m["content"] for m in messages if m.get("role") == "user"), None)
    # 실제 요청의 입력 JSON을 Python 자료로 변환하고 요청 기록이 없을 때만 문항 입력 사용
    actual_input = json.loads(user) if user is not None else case["input"]
    # 출력 형식과 거래 제한에 관한 자동 검사 결과를 읽고 없으면 빈 딕셔너리 사용
    assessment = evaluation.get("assessment") or {}
    # 실제 요청에서 Ollama 실행 옵션을 읽고 없으면 빈 딕셔너리 사용
    options = request.get("options") or {}
    # 서버가 반환한 종료 및 처리 시간 등의 메타데이터를 읽고 없으면 빈 딕셔너리 사용
    meta = record.get("response_metadata") or {}
    # API 토큰 사용량을 읽고 없으면 빈 딕셔너리 사용
    usage = record.get("usage") or {}
    # 응답 후 조회한 적재 모델 목록에서 현재 시험 모델을 찾기 위한 순회 시작
    loaded = next((m for m in (record.get("loaded_models") or {}).get("models", [])
                   # 이름 또는 모델 태그가 일치하는 첫 모델 정보를 선택하고 없으면 빈 딕셔너리 사용
                   if (m.get("name") or m.get("model")) == trial["model"]), {})
    # 최종 답변 문자열을 읽고 답변이 없으면 빈 문자열 사용
    content = record.get("content", "")
    # 최종 답변을 JSON으로 해석할 때 발생할 수 있는 오류를 처리하는 구간 시작
    try:
        # 중복 키 등을 검사하면서 최종 답변을 Python 딕셔너리로 변환
        parsed = parse_response(content)
    # 잘못된 JSON, 잘못된 자료형 또는 지나치게 깊은 중첩으로 인한 오류 처리
    except (ValueError, TypeError, RecursionError):
        # 파싱 실패를 표시하여 응답 원문을 별도 열에 남기도록 None 지정
        parsed = None
    # 호출 시작부터 종료까지 실제로 측정한 전체 시간을 읽기
    wall = record.get("wall_seconds")
    # 호출 시작과 종료 시각을 포함하는 결과 행 딕셔너리 생성
    row = {"started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
           # 공통 지시문과 펼친 입력 값을 결과 행에 포함, ** 기호로 딕셔너리 항목 합치기
           "system_instruction": system, **flatten_input(actual_input),
           # 파싱한 답변의 다섯 필드를 각각 별도 열에 넣고 파싱 실패 시 빈 값 지정
           **{key: parsed.get(key) if parsed is not None else None for key in OUTPUT_FIELDS},
           # 추론 원문을 저장하고 JSON 해석 실패 시에만 최종 답변 전체를 별도 원문 열에 보존
           "thinking": record.get("thinking", ""), "unparsed_response": content if parsed is None else "",
           # 문맥 크기와 생성 한도를 저장하고 OpenAI 요청은 max_output_tokens 값 사용
           "num_ctx": options.get("num_ctx"), "num_predict": options.get("num_predict", request.get("max_output_tokens")),
           # Ollama의 추론 여부 또는 OpenAI의 추론 노력 수준을 저장
           "think": request.get("think", (request.get("reasoning") or {}).get("effort")),
           # 시험 설정에 기록한 호출 제한 시간을 저장
           "timeout_seconds": trial["settings"].get("timeout"),
           # OpenAI 사용량 또는 Ollama 메타데이터에서 입력 토큰 수 선택
           "input_tokens": usage.get("input_tokens", meta.get("prompt_eval_count")),
           # OpenAI 사용량 또는 Ollama 메타데이터에서 실제 생성 토큰 수 선택
           "generated_tokens": usage.get("output_tokens", meta.get("eval_count")),
           # 서버가 제공한 캐시 입력 토큰 수를 저장하고 값이 없으면 미측정 상태 유지
           "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", meta.get("prompt_eval_cached_count")),
           # OpenAI가 구분하여 제공한 추론 토큰 수를 저장하고 없으면 미측정 상태 유지
           "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
           # 전체 소요 시간과 첫 토큰이 도착한 시간을 저장
           "wall_seconds": wall, "first_token_seconds": record.get("first_token_seconds"),
           # 최종 답변 내용이 처음 도착한 시간을 저장
           "first_content_seconds": record.get("first_content_seconds"),
           # 호출 중 관측한 GPU 전체 메모리 사용량 최댓값을 저장
           "gpu_peak_mib": record.get("peak_gpu_used_mib"),
           # 응답 후 모델의 GPU 적재 바이트 수를 1,048,576으로 나누어 MiB로 변환
           "model_gpu_mib": loaded["size_vram"] / 1048576 if "size_vram" in loaded else None,
           # 응답 후 모델 전체 크기를 바이트에서 MiB로 변환하고 값이 없으면 None 유지
           "model_total_mib": loaded["size"] / 1048576 if "size" in loaded else None,
           # 호출 중 관측한 GPU 사용률 최댓값을 저장
           "gpu_peak_percent": record.get("peak_gpu_utilization_percent"),
           # 호출 중 관측한 시스템 메모리 사용량과 CPU 사용률 최댓값을 저장
           "ram_peak_mib": record.get("peak_ram_used_mib"), "cpu_peak_percent": record.get("peak_cpu_utilization_percent"),
           # 기록된 API 비용에서 미국 달러 금액을 읽어 저장
           "cost_usd": (record.get("cost") or {}).get("usd"),
           # 지정된 JSON 구조와 거래 규칙의 자동 검사 결과를 서로 다른 열에 저장
           "json_valid": assessment.get("schema_valid"), "trading_valid": assessment.get("trading_valid"),
           # 최종 답변 완료, JSON 구조 통과와 전체 시간 300초 이하를 모두 충족하는지 계산
           "json_within_300_seconds": (bool(record.get("final_response_complete") and assessment.get("schema_valid") and wall <= 300)
                                       # 전체 시간이 없으면 시간 제한 판정도 비워 두고 설명 점수는 채점 전 빈 값으로 초기화
                                       if wall is not None else None), "score": None}
    # 미리 정한 모든 설정 열 이름을 하나씩 확인
    for name in SETTING_FIELDS:
        # 앞에서 결과 행에 넣지 않은 설정 열인지 확인
        if name not in row:
            # Ollama 옵션 또는 요청 최상위 항목에서 해당 설정 값을 읽어 추가
            row[name] = options.get(name, request.get(name))
    # 서버 전체 처리 시간과 로딩 시간의 출력 열 이름 및 원본 필드 이름을 쌍으로 지정
    for name, source in (("server_seconds", "total_duration"), ("load_seconds", "load_duration"),
                         # 입력 처리 시간과 생성 시간도 같은 변환 대상에 포함하여 반복
                         ("input_seconds", "prompt_eval_duration"), ("generation_seconds", "eval_duration")):
        # 서버가 반환한 나노초 단위 시간을 10억으로 나누어 초로 변환하고 누락값은 None 유지
        row[name] = meta[source] / 1e9 if meta.get(source) is not None else None
    # 실제 생성 토큰 수를 생성에 걸린 초로 나누어 초당 생성 토큰 수 계산
    row["tokens_per_second"] = (meta["eval_count"] / (meta["eval_duration"] / 1e9)
                                # 생성 시간이 0이 아니고 생성량이 제공된 경우에만 계산하고 나머지는 None 지정
                                if meta.get("eval_duration") and meta.get("eval_count") is not None else None)
    # 예상하지 않은 추가 출력도 버리지 않고 별도 열로 보존, 원본의 형식 오류 은폐 방지
    # 최종 답변을 JSON 객체로 해석할 수 있었는지 확인
    if parsed is not None:
        # 요구한 다섯 필드 밖의 추가 출력도 output_extra 접두어의 열로 보존
        row.update({f"output_extra_{key}": value for key, value in parsed.items() if key not in OUTPUT_FIELDS})
    # 호출 중 발생한 오류 각각을 JSON 문자열로 바꾸어 오류 목록 생성
    errors = [json_text(error) for error in record.get("errors", [])]
    # 필드 누락과 자료형 오류 및 거래 규칙 위반의 구체적 근거 보존
    # 형식 실패를 공란이나 정상 응답으로 바꾸지 않고 실제 원문과 함께 확인하는 구성
    # 응답 자동 검사에서 발견한 오류에 검사 오류임을 나타내는 접두어를 붙여 목록에 추가
    errors.extend("Response validation: " + json_text(error) for error in assessment.get("errors", []))
    # 자동 검사 자체를 수행하는 과정에서 오류가 발생했는지 확인
    if evaluation.get("grading_error"):
        # 자동 검사 실행 오류의 내용을 오류 목록에 추가
        errors.append("Automatic check error: " + evaluation["grading_error"])
    # 실제로 보낸 요청이 시험 계획의 요청과 달랐는지 확인
    if evaluation.get("request_mismatch"):
        # 계획과 실제 요청의 불일치를 알리는 영어 안내 문구를 오류 목록에 추가
        errors.append("Actual request differs from the planned request.")
    # 여러 오류 메시지를 줄바꿈으로 연결하여 오류 내용 열에 저장
    row["error_message"] = "\n".join(errors)
    # 문항별 세 평가 항목을 채점 칸에 배치하기 위해 1부터 3까지 반복
    for index in range(1, 4):
        # 현재 문항에 정의된 채점 기준 목록을 읽고 없으면 빈 목록 사용
        rubric = case.get("rubric", [])
        # 사람이 읽는 항목 번호에서 1을 뺀 리스트 위치의 기준을 선택하고 없으면 빈 딕셔너리 사용
        rule = rubric[index - 1] if len(rubric) >= index else {}
        # 기대 설명, 입력 경로, 판정, 점수, 인용문과 판정 이유의 채점 열을 하나씩 준비
        for name in ("expected", "source_paths", "verdict", "score", "quote", "rationale"):
            # 입력 경로 열에는 여러 원본 필드 경로를 줄바꿈으로 연결하여 저장
            row[f"review_{index}_{name}"] = ("\n".join(rule.get("source_paths", [])) if name == "source_paths"
                                             # 기대 설명 열에는 문항의 기준을 넣고 실제 판정 및 점수와 근거 칸은 검토를 위해 비워 두기
                                             else rule.get("expected") if name == "expected" else None)
    # 입력, 출력, 측정값, 자동 검사 결과와 채점 칸이 담긴 최종 결과 행 반환
    return row
