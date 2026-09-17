"""
PoC의 공통 출력 형식 정의

역할:
    - 두 로컬 모델과 Cloud에 적용할 다섯 필드 JSON Schema 생성
    - 형식 제약과 문장 사실 확인의 구분
    - 지시문 원문은 poc_cases.json에서 별도 관리
"""

import copy


def get_response_schema():
    """
    모든 PoC 모델에 공통 적용하는 다섯 필드 JSON Schema 반환

    기준:
        - 매 호출마다 새 객체 반환, 호출자의 변경이 다음 호출에 영향을 주지 않는 구성
        - 추가 필드 금지, 결정과 비중 및 근거와 회고의 필수 반환
        - 비중과 가용 잔고의 관계는 evaluate_case_response에서 별도 검사
        - 문장의 사실 정확성은 원본과 계산식의 별도 대조 대상
        - 영문과 한국어 지시문은 poc_cases.json에 보관
    """
    text = {"type": "string", "minLength": 1, "pattern": r"\S"}
    percentage = {"type": "number", "minimum": 0, "maximum": 100}
    properties = {
        "decision": {"type": "string", "enum": ["buy", "sell", "hold"]},
        "buy_allocation_percentage": copy.deepcopy(percentage),
        "sell_allocation_percentage": copy.deepcopy(percentage),
        "reason": copy.deepcopy(text), "reflection_log": copy.deepcopy(text),
    }
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}
