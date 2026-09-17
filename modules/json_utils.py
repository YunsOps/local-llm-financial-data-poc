"""
입력 전달과 SQLite 저장의 공통 JSON 변환

역할:
    - 한글과 숫자 자료형을 보존한 정렬 직렬화
    - 같은 입력의 해시 비교를 위한 일관된 문자열 생성
    - 중복 키와 비정상 숫자의 수신 차단

실제 문항과 지시문의 보관 위치: poc_cases.json
"""

import json


def _to_json(value):
    """
    입력 해시와 모델 전달에 공통으로 사용하는 JSON 문자열 변환

    입력: JSON으로 표현 가능한 자료 객체
    반환: 키 정렬과 UTF-8 전달에 사용할 문자열
    기준: NaN과 Infinity 직렬화 거부, 한글 이스케이프 없음
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _reject_constant(value):
    """
    JSON에 포함된 NaN과 Infinity의 사용 차단

    입력: JSON 파서에서 전달한 NaN 또는 Infinity 표기
    처리: ValueError 발생
    목적: 정상 숫자로 오인한 계산과 저장 방지
    """
    raise ValueError(f"허용하지 않는 JSON 값: {value}")


def _unique_object(pairs):
    """
    같은 필드 이름이 중복된 JSON 객체의 사용 차단

    입력: JSON 객체의 키와 값 쌍 목록
    반환: 중복 키가 없는 사전
    예외: 같은 필드가 두 번 등장한 경우의 ValueError
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"중복된 JSON 필드: {key}")
        result[key] = value
    return result
