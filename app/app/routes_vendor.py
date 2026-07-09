"""거래처 관리"""
from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select, or_

from database import engine, Vendor, User
from template_utils import templates

router = APIRouter()


# ============================================================
# 거래처 검색 JSON API (자동완성용)
# ============================================================
@router.get("/api/vendors/search")
def vendor_search(request: Request, q: str = "", limit: int = 10):
    """이름·사업자번호·연락처·담당자 부분일치 검색.
    빈 쿼리면 최근 사용한 거래처 우선."""
    if not request.session.get("user_id"):
        raise HTTPException(401)
    q = (q or "").strip()
    with Session(engine, expire_on_commit=False) as s:
        stmt = select(Vendor)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(
                Vendor.name.ilike(like),
                Vendor.biz_number.ilike(like),
                Vendor.phone.ilike(like),
                Vendor.contact_person.ilike(like),
            ))
        stmt = stmt.order_by(Vendor.name).limit(max(1, min(50, limit)))
        rows = s.exec(stmt).all()
    return JSONResponse([{
        "id": v.id,
        "name": v.name,
        "biz_number": v.biz_number or "",
        "phone": v.phone or "",
        "contact_person": v.contact_person or "",
        "vendor_type": v.vendor_type,
        "address": v.address or "",
    } for v in rows])


# ============================================================
# 거래처 빠른 등록 (검색하다가 없을 때 즉시 추가)
# ============================================================
@router.post("/api/vendors/quick-create")
@router.post("/api/vendors/quick-add")
def vendor_quick_add(
    request: Request,
    name: str = Form(""),
    biz_number: str = Form(""),
    phone: str = Form(""),
    contact_person: str = Form(""),
):
    if not request.session.get("user_id"):
        return JSONResponse({"ok": False, "error": "로그인이 필요합니다"}, status_code=401)
    name = (name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "거래처명을 입력하세요"}, status_code=400)
    with Session(engine, expire_on_commit=False) as s:
        # 중복 체크 (이름 일치)
        existing = s.exec(select(Vendor).where(Vendor.name == name)).first()
        if existing:
            return JSONResponse({"ok": True, "id": existing.id, "name": existing.name,
                                 "existed": True})
        v = Vendor(name=name, biz_number=biz_number.strip(),
                   phone=phone.strip(), contact_person=contact_person.strip(),
                   vendor_type="consumer")
        s.add(v); s.commit(); s.refresh(v)
        return JSONResponse({"ok": True, "id": v.id, "name": v.name, "existed": False})


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        user = s.get(User, uid)
    if user:
        from permissions import has_permission
        if not has_permission(user, "vendors"):
            raise HTTPException(403, "거래처 접근 권한이 없습니다.")
    return user


@router.get("/vendors", response_class=HTMLResponse)
def vendor_list(request: Request, q: str = ""):
    user = _user(request)
    search_q = (q or "").strip().lower()
    with Session(engine, expire_on_commit=False) as s:
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        if search_q:
            vendors = [v for v in vendors if any(search_q in (str(x) or "").lower() for x in [
                v.name, v.contact_person, v.phone, v.email, v.biz_number, v.address, v.memo,
            ])]
        rows = [{
            "id": v.id, "name": v.name, "contact_person": v.contact_person,
            "phone": v.phone, "email": v.email, "vendor_type": v.vendor_type,
            "biz_number": v.biz_number, "address": v.address, "memo": v.memo,
        } for v in vendors]
    return templates.TemplateResponse(request, "vendors.html", {
        "user": user, "vendors": rows, "search_q": search_q,
    })


@router.post("/vendors/new")
def vendor_create(
    request: Request,
    name: str = Form(...), contact_person: str = Form(""),
    phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), vendor_type: str = Form("consumer"),
    biz_number: str = Form(""), memo: str = Form(""),
):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        v = Vendor(name=name, contact_person=contact_person, phone=phone, email=email,
                   address=address, vendor_type=vendor_type, biz_number=biz_number, memo=memo)
        s.add(v)
        s.commit()
    return RedirectResponse("/vendors", status_code=303)


@router.get("/vendors/{vid}/edit", response_class=HTMLResponse)
def vendor_edit(request: Request, vid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        v = s.get(Vendor, vid)
        if not v:
            raise HTTPException(404)
        vd = {"id": v.id, "name": v.name, "contact_person": v.contact_person,
              "phone": v.phone, "email": v.email, "vendor_type": v.vendor_type,
              "biz_number": v.biz_number, "address": v.address, "memo": v.memo}
    return templates.TemplateResponse(request, "vendor_edit.html", {"user": user, "v": vd})


@router.post("/vendors/{vid}/edit")
def vendor_update(
    request: Request, vid: int,
    name: str = Form(...), contact_person: str = Form(""),
    phone: str = Form(""), email: str = Form(""),
    address: str = Form(""), vendor_type: str = Form("consumer"),
    biz_number: str = Form(""), memo: str = Form(""),
):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        v = s.get(Vendor, vid)
        if not v:
            raise HTTPException(404)
        v.name = name; v.contact_person = contact_person; v.phone = phone
        v.email = email; v.address = address; v.vendor_type = vendor_type
        v.biz_number = biz_number; v.memo = memo
        s.add(v)
        s.commit()
    return RedirectResponse("/vendors", status_code=303)


@router.post("/vendors/{vid}/delete")
def vendor_delete(request: Request, vid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        v = s.get(Vendor, vid)
        if v:
            s.delete(v)
            s.commit()
    return RedirectResponse("/vendors", status_code=303)
