"""권한 시스템 — 직원별 메뉴/금액 표시 권한 제어

- admin: 모든 권한 자동 부여
- staff: permissions_json에서 명시된 권한만 사용 가능
"""
import json
from typing import Optional


# 권한 키 정의 — 그룹별 (직원 관리 화면에서 그룹 단위로 표시)
PERMISSION_GROUPS = [
    ("📂 메뉴 접근", [
        ("projects", "프로젝트 조회·등록", "프로젝트 목록·캘린더·인력 배치"),
        ("projects_edit", "프로젝트 수정·삭제", "등록된 프로젝트 정보 변경"),
        ("quotes", "견적서", "견적서 조회·작성·출력 (견적 단가 포함)"),
        ("products", "단가표·재고/장비", "물품 단가·재고·장비 관리 (단가 포함)"),
        ("attendance", "근태 (알바 일당)", "알바 근무 기록 등록·조회"),
        ("expenses", "하청/외주 지급", "외주 발주 내역 등록·조회"),
        ("vendors", "거래처", "거래처 등록·조회"),
        ("workers", "근무자 목록", "이름·구분만 표시 (개인정보는 관리자 전용)"),
    ]),
    ("💰 프로젝트 매출·정산", [
        ("view_revenue", "매출 금액 보기", "프로젝트별 매출·공급가·부가세, 견적·단가 금액"),
        ("view_settlement", "결제완료 / 미수금 보기", "프로젝트 입금 상태와 미수금 목록"),
        ("manage_settlement", "입금완료 처리", "미수금 ↔ 입금완료 변경"),
    ]),
    ("🏗 거래처 외주 지급", [
        ("view_sub_pay", "외주 지급 금액·내역 보기", "발주 금액, 지급/미지급 내역"),
        ("manage_sub_pay", "외주 지급완료 처리", "미지급 ↔ 지급완료 변경"),
    ]),
    ("🧢 알바 급여", [
        ("view_wage_pay", "알바 급여 금액·내역 보기", "일당·오퍼비·급여 합계, 지급 내역"),
        ("manage_wage_pay", "알바 급여 지급완료 처리", "미지급 ↔ 지급완료 변경"),
    ]),
]
PERMISSION_KEYS = {k: label for _, items in PERMISSION_GROUPS for k, label, _d in items}

# 빠른 설정 프리셋
PERMISSION_PRESETS = {
    "field": ("현장 직원", ["projects", "attendance"]),
    "office": ("사무·경리", ["projects", "projects_edit", "quotes", "products", "attendance", "expenses",
                            "vendors", "workers", "view_revenue", "view_settlement", "manage_settlement",
                            "view_sub_pay", "manage_sub_pay", "view_wage_pay", "manage_wage_pay"]),
    "sales": ("영업 담당", ["projects", "projects_edit", "quotes", "products", "vendors",
                          "view_revenue", "view_settlement"]),
}

# 신규 직원 기본 권한 (현장 직원 수준, 금액 조회 불가)
DEFAULT_STAFF_PERMISSIONS = {k: (k in ("projects", "attendance", "expenses")) for k in PERMISSION_KEYS}

# 구버전 키 → 신규 키 자동 변환 (기존 직원 권한 보존)
_LEGACY_MAP = {
    "view_revenue": ["view_amounts"],
    "view_settlement": ["view_amounts"],
    "view_sub_pay": ["view_amounts", "view_expenses"],
    "view_wage_pay": ["view_amounts", "view_wages"],
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
    # 구버전 저장값 호환: 신규 키가 저장된 적 없으면 옛 키에서 변환
    for nk, olds in _LEGACY_MAP.items():
        if nk not in data:
            result[nk] = any(bool(data.get(o)) for o in olds)
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
    if key == "quote_price":
        # 견적 단가(견적서·단가표 금액) = 견적서 또는 단가표 권한이 있으면 자동 허용
        p = get_permissions(user)
        return bool(p.get("quotes") or p.get("products") or p.get("view_revenue"))
    perms = get_permissions(user)
    return perms.get(key, False)


def is_admin(user) -> bool:
    """관리자 여부"""
    return bool(user and (user.role or "").lower() == "admin")


def can_view_amounts(user) -> bool:
    """매출 금액(프로젝트 매출·견적·단가) 조회 권한"""
    return has_permission(user, "view_revenue")


def money_flags(user) -> dict:
    """화면별 금액 표시 판정 — request.state 에 그대로 실림"""
    rev = has_permission(user, "view_revenue")
    sub = has_permission(user, "view_sub_pay")
    wage = has_permission(user, "view_wage_pay")
    return {
        "can_view_amounts": rev,
        "can_view_revenue": rev,
        "can_view_settle": has_permission(user, "view_settlement"),
        "can_manage_settle": has_permission(user, "manage_settlement"),
        "can_view_sub": sub,
        "can_manage_sub": has_permission(user, "manage_sub_pay"),
        "can_view_wage": wage,
        "can_manage_wage": has_permission(user, "manage_wage_pay"),
        "can_view_profit": rev and sub and wage,   # 순이익은 매출·외주·급여 모두 볼 때만
        "can_view_quote_price": has_permission(user, "quote_price"),
    }


def require(request_or_user, key: str, msg: str = "권한이 없습니다."):
    """라우트 가드 — 권한 없으면 403"""
    from fastapi import HTTPException
    user = request_or_user
    if hasattr(request_or_user, "state"):
        user = getattr(request_or_user.state, "user", None)
    if not has_permission(user, key):
        raise HTTPException(403, msg)
    return user


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


# ════════════════════════════════════════════════════════════
# 본인 근무자 연결 / 재직 여부 (출퇴근·개인정보 권한 판정용)
# ════════════════════════════════════════════════════════════
def is_employed(worker) -> bool:
    """재직 중인지 — 비활성·퇴사·퇴사일 경과 시 False"""
    if not worker or not getattr(worker, "is_active", True):
        return False
    if (getattr(worker, "employment_status", "재직") or "재직") == "퇴사":
        return False
    rd = getattr(worker, "resign_date", None)
    if rd:
        from datetime import date as _date
        if rd <= _date.today():
            return False
    return True


def my_worker(session, user):
    """로그인 계정에 연결된 근무자.
    연결이 없으면 '이름이 같은 재직 정직원 1명'일 때만 자동 연결."""
    if not user:
        return None
    from database import Worker, User
    from sqlmodel import select
    wid = getattr(user, "worker_id", None)
    if wid:
        return session.get(Worker, wid)
    cands = [w for w in session.exec(select(Worker).where(Worker.name == (user.name or "").strip())).all()
             if (w.employee_type or "") == "정직원" and is_employed(w)]
    if len(cands) == 1:
        u = session.get(User, user.id)
        if u:
            u.worker_id = cands[0].id
            session.add(u)
            session.commit()
        return cands[0]
    return None
