"""
모델의 최종 응답에 대한 형식과 거래 제한 검사

처리 순서:
    1. 원문과 별도 채점 자료의 연결
    2. JSON 객체, 필드, 값의 자료형 검사
    3. 결정과 비중, 가용 잔고 및 최소 금액의 검산

범위: 문장 의미와 수익성의 자동 채점 제외, 원본 보정 및 실제 주문 없음
"""

import copy
import hashlib
import json
# 주문 금액의 경계값에서 이진 부동소수점 오차를 피하기 위한 십진수 계산
from decimal import Decimal, localcontext

from .data_evaluation_cases import _reject_constant, _unique_object


def _product(*values):
    """
    긴 소수 비중의 경계값 반올림 방지를 위한 유효 자릿수 확보 후 곱셈

    입력: 곱셈에 사용할 숫자 목록
    반환: 필요한 유효 자릿수를 확보한 Decimal 곱셈값
    목적: 최소 주문 금액 경계에서 부동소수점 반올림에 의한 오판 방지
    """
    numbers = [Decimal(str(value)) for value in values]
    with localcontext() as context:
        context.prec = max(28, sum(len(number.as_tuple().digits) for number in numbers))
        result = Decimal(1)
        for number in numbers:
            result *= number
        return result


def evaluate_case_response(case, raw_response, dataset_hash):
    """
    주어진 독립 사례의 최종 응답 형식과 거래 가능 여부를 검사하는 함수

    매개변수:
        case (dict): 별도 정답과 입력 해시를 포함한 평가용 사례
        dataset_hash (str): 사례를 보관한 전체 자료의 식별값
        raw_response (str): 추론 내용을 제외한 최종 응답 원문, 응답 없음은 빈 문자열

    반환값:
        dict: 원문과 해시, 자동 검사 결과, 별도 사실 대조 자료와 검토 항목

    주의사항:
        - 코드 블록 제거, 키 교정, hold 대체 등 원문 보정 없음
        - 자동 통과는 형식과 계산 가능한 거래 규칙의 통과에 한정
        - API 성공, 정상 종료, 입력 잘림 여부는 호출 기록으로 별도 확인 필요
        - 기대 사실과 검토 항목은 평가자 전용 자료이며 모델 전달 금지
        - 실제 주문, 모델 호출, DB 저장 및 환경 변수 접근 없음

    입력: 고정 사례, 최종 응답 원문, 자료 식별 해시
    반환: json_valid, schema_valid, trading_valid와 오류 및 검토 자료
    효과: 네트워크와 DB 접근 없음, 원문 내용의 보정 없음
    """
    if not isinstance(raw_response, str):
        raise TypeError("최종 응답 원문은 문자열로 전달 필요")
    case_id = case["case_id"]
    result = {
        "case_id": case_id, "dataset_hash": dataset_hash,
        "input_hash": case["input_hash"], "raw_response": raw_response,
        "response_hash": hashlib.sha256(raw_response.encode("utf-8")).hexdigest(),
        "json_valid": False, "schema_valid": False, "trading_valid": None,
        "errors": [], "review_status": "pending",
        "expected": copy.deepcopy(case["expected"]), "review_items": [],
    }

    # ------------------------------ * 1. 사람 검토 항목 준비 * ------------------------------
    # 요구한 설명의 포함 여부와 의미 검토를 구분한 목록 구성
    # 같은 사실의 여러 검토 항목 중복을 사실 정확도의 분모로 사용하지 않는 기준
    for prefix, category, items in (
        ("R", "required", case["input"]["required_observations"]),
        ("C", "meaning", case["expected"]["review_checks"]),
    ):
        for number, instruction in enumerate(items, 1):
            result["review_items"].append({
                "id": f"{prefix}{number:02d}", "category": category,
                "instruction": instruction, "verdict": "pending",
            })

    # ------------------------------ * 2. JSON 원문 검사 * ------------------------------
    # Decimal 파싱을 통한 1e9999의 Infinity 변환 방지와 소수 경계값 보존
    # 객체 중복 키, NaN, Infinity, 외부 문장 및 여러 JSON 객체의 거부
    try:
        parsed = json.loads(raw_response, parse_float=Decimal, parse_int=Decimal,
                            parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        if not isinstance(parsed, dict):
            raise ValueError("최상위 JSON 객체 필요")
    except (ValueError, RecursionError) as error:
        result["errors"].append({"code": "json", "detail": str(error)})
        return result
    result["json_valid"] = True

    # ------------------------------ * 3. 필드와 값 검사 * ------------------------------
    required_keys = {"decision", "buy_allocation_percentage", "sell_allocation_percentage",
                     "reason", "reflection_log"}
    if set(parsed) != required_keys:
        result["errors"].append({
            "code": "keys", "missing": sorted(required_keys - set(parsed)),
            "extra": sorted(set(parsed) - required_keys),
        })
    if parsed.get("decision") not in ("buy", "sell", "hold"):
        result["errors"].append({"code": "decision", "detail": "buy, sell, hold 중 하나 필요"})
    for field in ("buy_allocation_percentage", "sell_allocation_percentage"):
        value = parsed.get(field)
        if not isinstance(value, Decimal) or not value.is_finite() or not 0 <= value <= 100:
            result["errors"].append({"code": "percentage", "field": field,
                                     "detail": "0 이상 100 이하의 JSON 숫자 필요"})
    for field in ("reason", "reflection_log"):
        if not isinstance(parsed.get(field), str) or not parsed[field].strip():
            result["errors"].append({"code": "text", "field": field,
                                     "detail": "공백 이외의 내용이 있는 문자열 필요"})
    if result["errors"]:
        return result
    result["schema_valid"] = True

    # ------------------------------ * 4. 결정과 잔고 및 최소 주문 검사 * ------------------------------
    # 매수 비중은 수수료를 포함한 가용 현금의 사용 비율
    # 나눗셈 반올림 대신 부등식 양변의 곱셈을 통한 최소 주문 경계값 비교
    decision = parsed["decision"]
    buy = parsed["buy_allocation_percentage"]
    sell = parsed["sell_allocation_percentage"]
    consistent = ((decision == "buy" and buy > 0 and sell == 0)
                  or (decision == "sell" and sell > 0 and buy == 0)
                  or (decision == "hold" and buy == 0 and sell == 0))
    if not consistent:
        result["errors"].append({"code": "allocation", "detail": "결정과 매수 또는 매도 비중 불일치"})
    if decision not in case["expected"]["allowed_decisions"]:
        result["errors"].append({"code": "unavailable_decision", "detail": "정보 또는 가용 잔고 조건 위반"})
    state = case["input"]["strategy_state"]
    rules = case["input"]["trading_rules"]
    minimum = Decimal(str(rules["minimum_order_krw"]))
    if decision == "buy":
        cash = Decimal(str(state["cash_available_krw"]))
        fee = Decimal(str(rules["fee_rate"]))
        if _product(cash, buy) < _product(minimum, 1 + fee, 100):
            result["errors"].append({"code": "minimum_buy", "detail": "수수료 제외 주문 금액의 최솟값 미달"})
    elif decision == "sell":
        orderbook = case["input"]["market_input"]["orderbook"]
        if orderbook is None:
            result["errors"].append({"code": "missing_bid", "detail": "매도 주문 금액 계산에 필요한 매수 호가 누락"})
        else:
            btc = Decimal(str(state["btc_available"]))
            bid = Decimal(str(orderbook["bid_price"]))
            if _product(btc, sell, bid) < _product(minimum, 100):
                result["errors"].append({"code": "minimum_sell", "detail": "매도 주문 금액의 최솟값 미달"})
    result["trading_valid"] = not result["errors"]
    return result
