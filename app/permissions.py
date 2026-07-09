"""권한 시스템 — 직원별 메뉴/금액 표시 권한 제어

- admin: 모든 권한 자동 부여
- staff: permissions_json에서 명시된 권한만 사용 가능
"""
import json
from typing import Optional


# 권한 키 정의 (관리자가 직원에게 줄 수 있는 권한들)
PERMISSION_KEYS = {
    "projects": "프로젝트 조회/등록/수정",
    "projects_edit": "프로젝트 수정/삭제",
    "expenses": "비용 등록/조회",
    "attendance": "근태 등록/조회",
    "quotes": "견적서 조회/작성",
    "products": "물품 등록/조회",
    "vendors": "거래처 관리",
    "workers": "근무자 관리",
    "view_amounts": "금액 조회 (매출/이익/단가)",
    "view_expenses": "비용 금액 조회",
    "view_wages": "근무자 일당 조회",
}


# 신규 직원 기본 권한 (등록만 가능, 금액 조회 불가)
DEFAULT_STAFF_PERMISSIONS = {
    "projects": True,        # 프로젝트 조회/등록
    "projects_edit": False,  # 수정/삭제는 권한 따로
    "expenses": True,        # 비용 등록 가능
    "attendance": True,      # 근태 등록 가능
    "quotes": False,         # 견적서는 기본 차단
    "products": False,
    "vendors": False,
    "workers": False,
    "view_amounts": False,   # 금액 조회 불가
    "view_expenses": False,
    "view_wages": False,
}


def get_permissions(user, effective: bool = True) -> dict:
    """사용자의 권한 dict 반환.
    - effective=True (기본): 실제 사용 시 판정용 — admin은 모든 권한 True 반환
    - effective=False: 저장된 값 그대로 반환 (권한 수정 폼에서 이전 선택 상태 복원 시 사용)

    admin은 실제 접근 판정 시에는 모든 권한을 자동 가지지만,
    권한 수정 화면에서는 사용자가 이전에 명시적으로 저장한 체크박스 상태를 그대로 보여줘야
    'staff로 강등했을 때 되살아날 권한'을 관리자가 명확히 인지할 수 있다.
    """
    if not user:
        return {k: False for k in PERMISSION_KEYS}

    if effective and (user.role or "").lower() == "admin":
        return {k: True for k in PERMISSION_KEYS}

    # 저장된 permissions_json 파싱 (없는 키는 False)
    try:
        data = json.loads(user.permissions_json or "{}")
    except (json.JSONDecodeError, TypeError, AttributeError):
        data = {}

    result = {k: False for k in PERMISSION_KEYS}
    for k, v in data.items():
        if k in result:
            result[k] = bool(v)
    return result


def get_stored_permissions(user) -> dict:
    """수정 폼용 — 저장된 권한 상태 그대로 반환 (admin이라도 저장값 유지)"""
    return get_permissions(user, effective=False)


def has_permission(user, key: str) -> bool:
    """특정 권한 보유 여부"""
    if not user:
        return False
    if (user.role or "").lower() == "admin":
        return True
    perms = get_permissions(user)
    return perms.get(key, False)


def is_admin(user) -> bool:
    """관리자 여부"""
    return bool(user and (user.role or "").lower() == "admin")


def can_view_amounts(user) -> bool:
    """금액(매출/이익/단가) 조회 권한 여부.
    - admin: 항상 True
    - staff: view_amounts 권한이 True인 경우만
    """
    return has_permission(user, "view_amounts")


def filter_amount(user, value, fallback="—"):
    """금액 값을 권한에 따라 필터링.
    - 조회 권한 있으면 원본 반환
    - 없으면 fallback 문자열 반환
    """
    if can_view_amounts(user):
        return value
    return fallback


def set_permissions(user, perms_dict: dict):
    """직원 권한 일괄 설정 (PERMISSION_KEYS 외 키는 무시)"""
    clean = {}
    for k in PERMISSION_KEYS:
        clean[k] = bool(perms_dict.get(k, False))
    user.permissions_json = json.dumps(clean, ensure_ascii=False)
    return user


def require_perm(request, key: str):
    """라우트 가드: 권한 없으면 403 또는 로그인 페이지로.
    사용: 라우트 함수 첫 줄에 호출

        @router.get("/projects")
        def project_list(request: Request):
            require_perm(request, "projects")
            ...
    """
    from fastapi import HTTPException
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(303, headers={"Location": "/login"})
    if not has_permission(user, key):
        raise HTTPException(
            403,
            f"이 기능에 대한 권한이 없습니다. 관리자에게 권한을 요청하세요.",
        )
    return user


def require_admin_user(request):
    """관리자 전용 라우트 가드"""
    from fastapi import HTTPException
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(303, headers={"Location": "/login"})
    if not is_admin(user):
        raise HTTPException(403, "관리자 권한이 필요합니다.")
    return user
