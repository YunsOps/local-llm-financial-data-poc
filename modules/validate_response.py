"""공통 출력 스키마와 JSON 및 거래 제한 검사.

설명의 사실 정확성은 자동 채점하지 않는 범위.
현재 가상 시험은 입력 자료와 별도 정답을 함께 사용, 정답의 모델 전송 없음.
"""

# 중첩 사전까지 독립적으로 복사하는 deepcopy 사용을 위한 copy 모듈 가져오기
import copy
# 모델이 반환한 JSON 문자열을 해석하기 위한 json 모듈 가져오기
import json
# 소수 계산 오차를 줄일 Decimal과 함수 안에서만 계산 정밀도를 바꿀 localcontext 가져오기
from decimal import Decimal, localcontext


# 모든 모델에 전달할 JSON 출력 형식 사전을 만드는 함수 정의
def get_response_schema():
    """
    모든 PoC 모델에 공통 적용하는 다섯 필드 JSON Schema 반환

    기준:
        - 매 호출마다 새 객체 반환, 호출자의 변경이 다음 호출에 영향을 주지 않는 구성
        - 추가 필드 금지, 결정과 비중 및 근거와 회고의 필수 반환
        - 비중과 가용 잔고의 관계는 validate_response에서 별도 검사
        - 문장의 사실 정확성은 원본과 계산식의 별도 대조 대상
        - 영문과 한국어 지시문은 prompt.py에 보관
    """
    # 설명은 길이가 1 이상이며 공백 아닌 문자를 포함해야 한다는 문자열 규칙 정의
    text = {"type": "string", "minLength": 1, "pattern": r"\S"}
    # 매수와 매도 비율은 0~100 범위의 JSON 숫자라는 공통 규칙 정의
    percentage = {"type": "number", "minimum": 0, "maximum": 100}
    # 허용할 다섯 필드의 이름과 각 필드의 규칙을 담는 사전 구성 시작
    properties = {
        # decision 값은 buy, sell, hold 문자열 중 하나로 제한
        "decision": {"type": "string", "enum": ["buy", "sell", "hold"]},
        # 매수 비율 규칙을 독립 복사해 다른 필드의 수정이 영향을 주지 않도록 구성
        "buy_allocation_percentage": copy.deepcopy(percentage),
        # 매도 비율 규칙도 별도의 사전으로 복사
        "sell_allocation_percentage": copy.deepcopy(percentage),
        # 판단 근거와 과거 기록 검토에 비어 있지 않은 문자열 규칙을 각각 복사
        "reason": copy.deepcopy(text), "reflection_log": copy.deepcopy(text),
    # 필드별 규칙 사전 구성 종료
    }
    # 응답 최상위는 객체이며 지정하지 않은 추가 필드는 금지하는 규칙 반환 시작
    return {"type": "object", "additionalProperties": False,
            # properties의 모든 키를 required 목록으로 만들어 다섯 필드 전부 필수로 지정
            "properties": properties, "required": list(properties)}

# JSON의 NaN과 Infinity처럼 허용하지 않는 상수를 발견했을 때 호출할 함수 정의
def _reject_constant(value):
    """
    JSON에 포함된 NaN과 Infinity의 사용 차단

    입력: JSON 파서에서 전달한 NaN 또는 Infinity 표기
    처리: ValueError 발생
    목적: 정상 숫자로 오인한 계산과 저장 방지
    """
    # 문제 값을 f 문자열에 넣어 ValueError 발생, 비정상 숫자의 파싱 차단
    raise ValueError(f"허용하지 않는 JSON 값: {value}")


# 파서가 전달한 키와 값 목록에서 중복 키를 검사하는 함수 정의
def _unique_object(pairs):
    """
    같은 필드 이름이 중복된 JSON 객체의 사용 차단

    입력: JSON 객체의 키와 값 쌍 목록
    반환: 중복 키가 없는 사전
    예외: 같은 필드가 두 번 등장한 경우의 ValueError
    """
    # 중복 검사를 통과한 필드를 담을 빈 딕셔너리 생성
    result = {}
    # 키와 값의 쌍을 하나씩 분리해 반복 처리
    for key, value in pairs:
        # 동일한 키가 앞선 필드에 이미 있었는지 확인
        if key in result:
            # 중복 키를 조용히 덮어쓰지 않고 오류로 처리
            raise ValueError(f"중복된 JSON 필드: {key}")
        # 처음 등장한 키에 현재 값을 저장
        result[key] = value
    # 중복 없는 완성된 객체를 JSON 파서에 반환
    return result

# 최종 답변 원문을 파싱하는 함수 정의, * 뒤 decimal_numbers는 이름으로 전달하는 옵션
def parse_response(raw_response, *, decimal_numbers=False):
    """중복 키와 비정상 상수를 거부하며 최상위 JSON 객체만 반환.

    입력: 코드 블록이나 부가 설명을 제거하지 않은 응답 원문.
    계산용 검사에서는 Decimal 사용, 저장용 파싱에서는 일반 JSON 숫자 사용.
    목적: 최소 주문금액 경계의 정밀도 확보와 잘못된 JSON의 임의 교정 방지.
    """
    # 검산용이면 정수와 소수를 모두 Decimal로 읽도록 설정, 저장용이면 기본 숫자 파싱 사용
    options = {"parse_int": Decimal, "parse_float": Decimal} if decimal_numbers else {}
    # 원문을 JSON으로 해석하면서 객체마다 중복 키 검사 함수 적용
    parsed = json.loads(raw_response, object_pairs_hook=_unique_object,
                        # 비정상 상수 검사 함수와 선택적인 숫자 변환 인자를 **로 펼쳐 전달
                        parse_constant=_reject_constant, **options)
    # 파싱 결과가 리스트나 숫자가 아닌 최상위 딕셔너리인지 검사
    if not isinstance(parsed, dict):
        # 요구한 JSON 객체가 아닌 응답을 오류로 처리
        raise ValueError("최상위 JSON 객체 필요")
    # 원문을 임의로 교정하지 않고 파싱한 객체 반환
    return parsed


# *values로 여러 숫자를 튜플로 받아 정밀하게 곱하는 내부 함수 정의
def _product(*values):
    """
    긴 소수 비중의 경계값 반올림 방지를 위한 유효 자릿수 확보 후 곱셈

    입력: 곱셈에 사용할 숫자 목록
    반환: 필요한 유효 자릿수를 확보한 Decimal 곱셈값
    목적: 최소 주문 금액 경계에서 부동소수점 반올림에 의한 오판 방지
    """
    # 숫자를 먼저 문자열로 바꾼 뒤 Decimal로 변환해 이진 실수의 잔여 오차 유입 감소
    numbers = [Decimal(str(value)) for value in values]
    # 다른 계산의 정밀도를 바꾸지 않도록 지역 Decimal 계산 환경 시작
    with localcontext() as context:
        # 입력 숫자의 자릿수 합과 기본 28자리 중 큰 값을 곱셈 정밀도로 지정
        context.prec = max(28, sum(len(number.as_tuple().digits) for number in numbers))
        # 곱셈 누적의 시작값을 Decimal 1로 초기화
        result = Decimal(1)
        # 곱할 숫자를 앞에서부터 하나씩 순회
        for number in numbers:
            # result = result * number와 같은 축약 연산으로 곱셈 결과 누적
            result *= number
        # 지역 계산 환경에서 얻은 곱셈값 반환
        return result


# 입력과 모델 원문 및 평가용 정답을 받아 형식과 계산 가능한 거래 규칙을 검사하는 함수 정의
def validate_response(input_data, raw_response, expected):
    """최종 응답의 JSON 형식과 가상 문항의 정답 및 거래 규칙 검사.

    입력: 시장과 계좌 및 거래 규칙의 사전, 최종 응답 문자열, 별도 기대 답변.
    반환: JSON 구문, 다섯 필드 형식, 거래 제한의 통과 여부와 오류 목록.
    범위: 가용 잔액, 최소 주문금액, 수수료와 자료 누락 규칙의 계산 가능한 조건.
    정답: expected의 allowed_decisions와 반환 결정을 대조, 별도 검토 기준은 CSV에서 사용.
    제외: 설명 의미의 자동 채점과 실제 주문.
    원문: 키 교정, 코드 블록 제거 또는 실패 응답의 hold 대체 없음.
    """
    # 검사 전 JSON과 스키마는 False, 거래 판정은 아직 없다는 None, 오류 목록은 빈 리스트로 초기화
    result = {"json_valid": False, "schema_valid": False, "trading_valid": None, "errors": []}

    # ------------------------------ * 1. JSON 원문 검사 * ------------------------------
    # Decimal 파싱을 통한 1e9999의 Infinity 변환 방지와 소수 경계값 보존
    # 객체 중복 키, NaN, Infinity, 외부 문장 및 여러 JSON 객체의 거부
    # JSON 해석 실패를 결과로 남기기 위한 예외 처리 구간 시작
    try:
        # 소수 경계값을 보존하는 Decimal 모드로 최종 응답 파싱
        parsed = parse_response(raw_response, decimal_numbers=True)
    # 잘못된 JSON, 잘못된 입력 자료형과 과도하게 깊은 중첩의 오류 수신
    except (ValueError, TypeError, RecursionError) as error:
        # 오류 분류와 상세 메시지를 errors 리스트에 추가
        result["errors"].append({"code": "json", "detail": str(error)})
        # JSON부터 실패한 경우 다음 검사 없이 현재 결과 반환
        return result
    # JSON 문법 해석에 성공했다는 표시, 필수 필드 검사를 통과했다는 의미는 아님
    result["json_valid"] = True

    # ------------------------------ * 2. 필드와 값 검사 * ------------------------------
    # 허용할 필드 이름을 순서와 중복이 없는 set으로 구성 시작
    required_keys = {"decision", "buy_allocation_percentage", "sell_allocation_percentage",
                     # 판단 근거와 과거 기록 검토 필드를 더해 다섯 이름의 집합 완성
                     "reason", "reflection_log"}
    # 실제 키 집합과 필수 키 집합을 비교해 누락 또는 추가 필드 확인
    if set(parsed) != required_keys:
        # 필드 구성 오류의 상세 정보를 오류 목록에 추가 시작
        result["errors"].append({
            # 필수 집합에서 실제 집합을 뺀 누락 키를 정렬해 기록
            "code": "keys", "missing": sorted(required_keys - set(parsed)),
            # 실제 집합에서 필수 집합을 뺀 추가 키를 정렬해 기록
            "extra": sorted(set(parsed) - required_keys),
        # 키 구성 오류 사전과 append 호출 종료
        })
    # decision 값이 허용한 세 문자열 중 하나인지 확인
    if parsed.get("decision") not in ("buy", "sell", "hold"):
        # 잘못된 결정값에 대한 오류 추가
        result["errors"].append({"code": "decision", "detail": "buy, sell, hold 중 하나 필요"})
    # 매수 및 매도 비율 필드를 같은 규칙으로 검사하기 위한 반복
    for field in ("buy_allocation_percentage", "sell_allocation_percentage"):
        # get으로 필드 값 조회, 필드가 없으면 None 수신
        value = parsed.get(field)
        # Decimal 숫자인지, 유한한지와 0~100 범위인지 확인, 문자열 및 bool 등 제외
        if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 100:
            # 비율 형식 오류의 분류와 해당 필드 이름 기록
            result["errors"].append({"code": "percentage", "field": field,
                                     # 허용하는 숫자 범위를 오류 설명에 포함
                                     "detail": "0 이상 100 이하의 JSON 숫자 필요"})
    # 두 설명 필드에 같은 문자열 검사 적용
    for field in ("reason", "reflection_log"):
        # 문자열 여부와 strip으로 공백을 제거한 뒤에도 내용이 남는지 확인
        if not isinstance(parsed.get(field), str) or not parsed[field].strip():
            # 설명 문자열 오류와 해당 필드 이름 기록
            result["errors"].append({"code": "text", "field": field,
                                     # 공백만 있는 설명을 허용하지 않는다는 오류 근거 기록
                                     "detail": "공백 이외의 내용이 있는 문자열 필요"})
    # 지금까지 필드 및 값 검사에서 오류가 하나라도 발생했는지 확인
    if result["errors"]:
        # 잘못된 필드로 계산하지 않도록 거래 규칙 검사 전에 반환
        return result
    # 다섯 필드와 자료형 및 허용값의 검사 통과 표시
    result["schema_valid"] = True

    # ------------------------------ * 3. 결정과 잔고 및 최소 주문 검사 * ------------------------------
    # 매수 비중은 수수료를 포함한 가용 현금의 사용 비율
    # 나눗셈 반올림 대신 부등식 양변의 곱셈을 통한 최소 주문 경계값 비교
    # 이미 형식 검사를 마친 매매 의견 조회
    decision = parsed["decision"]
    # Decimal로 파싱된 매수 비율 조회
    buy = parsed["buy_allocation_percentage"]
    # Decimal로 파싱된 매도 비율 조회
    sell = parsed["sell_allocation_percentage"]
    # 매수 의견이면 매수 비율만 양수여야 한다는 조건 구성
    consistent = ((decision == "buy" and buy > 0 and sell == 0)
                  # 또는 매도 의견이면 매도 비율만 양수여야 한다는 조건 추가
                  or (decision == "sell" and sell > 0 and buy == 0)
                  # 또는 보류 의견이면 두 비율이 모두 0이어야 한다는 조건으로 검사식 완성
                  or (decision == "hold" and buy == 0 and sell == 0))
    # 결정과 비율의 조합이 세 허용 조합에 포함되지 않는지 확인
    if not consistent:
        # 매매 의견과 사용 비율의 불일치 기록
        result["errors"].append({"code": "allocation", "detail": "결정과 매수 또는 매도 비중 불일치"})
    # 가용 잔액과 거래 이력이 있는 계좌 사전 조회
    state = input_data["strategy_state"]
    # 수수료와 최소 주문금액 등 거래 규칙 사전 조회
    rules = input_data["trading_rules"]
    # 호가와 시세 등 시장 자료 사전 조회
    market = input_data["market_input"]
    # 현재 시험의 사전 정답과 반환 결정을 대조, 수치 검산과 설명 채점의 구분
    # 반환 결정이 가상 문항의 사전 허용 결정 목록에 포함되는지 확인
    if decision not in expected["allowed_decisions"]:
        # 문항의 잔액 또는 누락 조건에 맞지 않는 결정 기록
        result["errors"].append({"code": "unavailable_decision", "detail": "문항에서 허용한 결정의 범위 위반"})
    # 최소 주문금액을 문자열을 거쳐 Decimal로 변환
    minimum = Decimal(str(rules["minimum_order_krw"]))
    # 매수 의견인 경우에만 매수 가능 금액 검사
    if decision == "buy":
        # 묶인 현금을 포함하지 않은 가용 현금만 Decimal로 조회
        cash = Decimal(str(state["cash_available_krw"]))
        # 입력의 수수료율을 Decimal로 변환
        fee = Decimal(str(rules["fee_rate"]))
        # 현금×비율과 최소금액×(1+수수료)×100 비교, 나눗셈 반올림 없이 같은 최소 주문 조건 검사
        if _product(cash, buy) < _product(minimum, 1 + fee, 100):
            # 수수료를 제외한 실제 주문금액이 최소값보다 작은 경우 오류 기록
            result["errors"].append({"code": "minimum_buy", "detail": "수수료 제외 주문 금액의 최솟값 미달"})
    # 매도가 선택된 경우 매도 수량과 호가에 따른 주문금액 검사
    elif decision == "sell":
        # 매도 대금을 계산하는 데 필요한 호가 자료 조회
        orderbook = market["orderbook"]
        # 호가가 명시적인 누락값 None인지 확인
        if orderbook is None:
            # 매수호가가 없어 매도금액을 계산할 수 없다는 오류 기록
            result["errors"].append({"code": "missing_bid", "detail": "매도 주문 금액 계산에 필요한 매수 호가 누락"})
        # 호가 자료가 존재하는 경우 금액 검산 수행
        else:
            # 매도에 사용할 수 있는 BTC 수량을 Decimal로 조회
            btc = Decimal(str(state["btc_available"]))
            # 즉시 매도 예상금액의 기준인 매수호가를 Decimal로 조회
            bid = Decimal(str(orderbook["bid_price"]))
            # BTC×매도비율×매수호가와 최소금액×100을 비교해 주문 조건 검사
            if _product(btc, sell, bid) < _product(minimum, 100):
                # 계산한 매도금액이 최소 주문금액 미만인 경우 오류 기록
                result["errors"].append({"code": "minimum_sell", "detail": "매도 주문 금액의 최솟값 미달"})
    # 오류 리스트가 비었을 때만 거래 제한 결과를 True로 설정
    result["trading_valid"] = not result["errors"]
    # JSON, 스키마, 거래 제한의 별도 판정과 오류 목록 반환
    return result
