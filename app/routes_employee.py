"""직원 관리 라우트 — 관리자가 직원 계정 추가/수정/권한 설정"""
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from database import engine, User, Worker, hash_password
from permissions import (
    PERMISSION_KEYS, DEFAULT_STAFF_PERMISSIONS,
    get_permissions, get_stored_permissions, set_permissions, is_admin,
)
from template_utils import templates

router = APIRouter()


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        return s.get(User, uid)


def _require_admin(request: Request):
    user = _user(request)
    if not is_admin(user):
        raise HTTPException(403, "관리자 권한이 필요합니다.")
    return user


@router.get("/employees", response_class=HTMLResponse)
def employee_list(request: Request):
    """직원 목록 (관리자만 접근 가능)"""
    admin = _require_admin(request)
    with Session(engine, expire_on_commit=False) as s:
        users = s.exec(select(User).where(User.is_active == True).order_by(User.created_at)).all()
        rows = []
        for u in users:
            perms = get_permissions(u)
            perm_count = sum(1 for v in perms.values() if v)
            rows.append({
                "id": u.id, "username": u.username, "name": u.name,
                "role": u.role, "phone": u.phone,
                "employee_type": u.employee_type,
                "perm_count": perm_count,
                "perm_total": len(PERMISSION_KEYS),
                "is_self": u.id == admin.id,
            })
    return templates.TemplateResponse(request, "employees.html", {
        "user": admin, "rows": rows,
    })


@router.get("/employees/new", response_class=HTMLResponse)
def employee_new(request: Request):
    admin = _require_admin(request)
    return templates.TemplateResponse(request, "employee_form.html", {
        "user": admin, "e": None,
        "perms": DEFAULT_STAFF_PERMISSIONS,
        "permission_keys": PERMISSION_KEYS,
    })


@router.post("/employees/new")
async def employee_create(request: Request):
    admin = _require_admin(request)
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()
    name = (form.get("name") or "").strip()

    if not username or not password or not name:
        raise HTTPException(400, "아이디, 비밀번호, 이름을 모두 입력해주세요.")
    if len(password) < 4:
        raise HTTPException(400, "비밀번호는 4자 이상이어야 합니다.")

    with Session(engine, expire_on_commit=False) as s:
        # 중복 검사
        existing = s.exec(select(User).where(User.username == username)).first()
        if existing:
            raise HTTPException(400, f"아이디 '{username}'은 이미 사용 중입니다.")

        new_user = User(
            username=username,
            password_hash=hash_password(password),
            name=name,
            phone=form.get("phone") or "",
            role=form.get("role") or "staff",
            employee_type=form.get("employee_type") or "정직원",
            is_active=True,
        )
        # 권한 설정 — perms_submitted 마커가 있을 때만 폼 값 반영 (안전장치)
        if form.get("perms_submitted") == "1":
            perms = {k: (form.get(f"perm_{k}") == "on") for k in PERMISSION_KEYS}
            set_permissions(new_user, perms)
        else:
            # 마커 없으면 기본 권한 적용 (안전한 fallback)
            set_permissions(new_user, DEFAULT_STAFF_PERMISSIONS)
        s.add(new_user)
        s.commit()
    return RedirectResponse("/employees?created=1", status_code=303)


@router.get("/employees/{eid}/edit", response_class=HTMLResponse)
def employee_edit(request: Request, eid: int):
    admin = _require_admin(request)
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(User, eid)
        if not u:
            raise HTTPException(404)
        # ★ 수정 폼에서는 사용자가 명시적으로 저장한 권한 상태 그대로 표시
        #   (admin이라도 저장된 체크박스 상태를 유지 → staff로 강등 시 그대로 살아남)
        perms = get_stored_permissions(u)
    return templates.TemplateResponse(request, "employee_form.html", {
        "user": admin,
        "e": {
            "id": u.id, "username": u.username, "name": u.name,
            "role": u.role, "phone": u.phone,
            "employee_type": u.employee_type,
            "is_self": u.id == admin.id,
        },
        "perms": perms,
        "permission_keys": PERMISSION_KEYS,
    })


@router.post("/employees/{eid}/edit")
async def employee_update(request: Request, eid: int):
    admin = _require_admin(request)
    form = await request.form()
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(User, eid)
        if not u:
            raise HTTPException(404)
        u.name = (form.get("name") or u.name).strip()
        u.phone = form.get("phone") or ""
        u.employee_type = form.get("employee_type") or u.employee_type

        # 비밀번호 변경 (입력한 경우에만)
        new_pw = (form.get("password") or "").strip()
        if new_pw:
            if len(new_pw) < 4:
                raise HTTPException(400, "비밀번호는 4자 이상이어야 합니다.")
            u.password_hash = hash_password(new_pw)

        # 역할 변경 (자기 자신은 못 바꿈)
        if u.id != admin.id:
            new_role = (form.get("role") or u.role).strip().lower()
            if new_role in ("admin", "staff"):
                u.role = new_role

        # 권한 설정 — perms_submitted 마커가 있을 때만 갱신 (안전장치)
        # admin은 어차피 모든 권한이 자동 부여되지만, 다시 staff로 강등되었을 때를 대비해
        # 저장된 권한 JSON도 폼 값 그대로 반영해 둔다.
        if form.get("perms_submitted") == "1":
            perms = {k: (form.get(f"perm_{k}") == "on") for k in PERMISSION_KEYS}
            set_permissions(u, perms)

        s.add(u)
        s.commit()
    return RedirectResponse("/employees?updated=1", status_code=303)


@router.post("/employees/{eid}/delete")
def employee_delete(request: Request, eid: int):
    admin = _require_admin(request)
    if eid == admin.id:
        raise HTTPException(400, "자기 자신은 삭제할 수 없습니다.")
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(User, eid)
        if u:
            u.is_active = False
            s.add(u)
            s.commit()
    return RedirectResponse("/employees?deleted=1", status_code=303)
