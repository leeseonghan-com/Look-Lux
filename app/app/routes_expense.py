"""비용처리 + 영수증 사진 업로드"""
import os
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select
from PIL import Image

from database import engine, Expense, Project, User, Vendor
from template_utils import templates

router = APIRouter()
RECEIPT_DIR = Path(os.environ.get("DATA_DIR", "data")) / "receipts"


def _to_int(value, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = str(value).strip().replace(",", "").replace(" ", "")
    if not s:
        return default
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        user = s.get(User, uid)
    if user:
        from permissions import has_permission
        if not has_permission(user, "expenses"):
            raise HTTPException(403, "비용 처리 권한이 없습니다.")
    return user


@router.get("/expenses", response_class=HTMLResponse)
def expense_list(request: Request, project_id: str = "", q: str = ""):
    """비용 목록 — project_id 필터 + q 키워드 검색 (내용·거래처·항목·메모)"""
    user = _user(request)
    try:
        pid = int(project_id) if project_id and str(project_id).strip() else None
    except (ValueError, TypeError):
        pid = None
    search_q = (q or "").strip()
    with Session(engine, expire_on_commit=False) as s:
        stmt = select(Expense).order_by(Expense.expense_date.desc())
        if pid:
            stmt = stmt.where(Expense.project_id == pid)
        items = s.exec(stmt).all()
        # 키워드 검색 — 메모리 필터 (소규모 데이터 가정, 안전)
        if search_q:
            ql = search_q.lower()
            projects_map = {p.id: p.name for p in s.exec(select(Project)).all()}
            filtered = []
            for e in items:
                project_name = (projects_map.get(e.project_id, "") or "").lower()
                if any(ql in (str(v) or "").lower() for v in [
                    e.description or "", getattr(e, "vendor_name", "") or "",
                    e.category or "", getattr(e, "tax_excluded_note", "") or "",
                    project_name, str(e.amount or ""),
                ]):
                    filtered.append(e)
            items = filtered
        rows = []
        for e in items:
            p = s.get(Project, e.project_id) if e.project_id else None
            rows.append({
                "id": e.id, "date": e.expense_date,
                "project_name": p.name if p else "?",
                "category": e.category or "기타",
                "vendor": getattr(e, "vendor_name", "") or "",
                "description": e.description or "",
                "amount": e.amount or 0,
                "payment_method": e.payment_method or "",
                "receipt": e.receipt_image or "",
                "has_evidence": getattr(e, "has_evidence", True) if getattr(e, "has_evidence", True) is not None else True,
                "tax_excluded_note": getattr(e, "tax_excluded_note", "") or "",
            })
        total = sum(r["amount"] for r in rows)
        # 프로젝트 — 최신순 정렬 (created_at desc, id desc)
        projects_raw = s.exec(
            select(Project).order_by(Project.created_at.desc(), Project.id.desc())
        ).all()
        vendors_map = {v.id: v.name for v in s.exec(select(Vendor)).all()}
        projects = [{
            "id": p.id, "name": p.name, "code": p.code or "",
            "event_date": p.event_date,
            "vendor_name": vendors_map.get(p.vendor_id, "") if p.vendor_id else "",
        } for p in projects_raw]
        # 현재 선택된 프로젝트 정보 (검색 박스 표시용)
        selected_project = None
        if pid:
            for p in projects:
                if p["id"] == pid:
                    selected_project = p
                    break
    return templates.TemplateResponse(request, "expenses.html", {"user": user, "rows": rows, "total": total,
        "projects": projects, "selected_project_id": pid,
        "selected_project": selected_project, "search_q": search_q,
    })


@router.get("/expenses/by-vendor", response_class=HTMLResponse)
def expenses_by_vendor(request: Request):
    """거래처별 비용 지출 집계 — 구버전 DB(vendor_id 컬럼 없음)에서도 안전 작동"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        all_exp = s.exec(select(Expense)).all()
        vendors = {v.id: v for v in s.exec(select(Vendor)).all()}

        # 거래처별 집계
        groups = {}
        UNKNOWN_KEY = (-1, "(거래처 미지정)")

        for e in all_exp:
            # ★ 구 DB 호환: getattr로 안전 접근 (vendor_id 컬럼 없으면 None)
            e_vendor_id = getattr(e, "vendor_id", None)
            e_vendor_name = getattr(e, "vendor_name", "") or ""
            e_amount = getattr(e, "amount", 0) or 0
            e_category = getattr(e, "category", "") or "기타"
            e_has_evidence = getattr(e, "has_evidence", True)
            e_date = getattr(e, "expense_date", None)

            if e_vendor_id and e_vendor_id in vendors:
                key = (e_vendor_id, vendors[e_vendor_id].name)
            elif e_vendor_name.strip():
                key = (None, e_vendor_name.strip())
            else:
                key = UNKNOWN_KEY

            g = groups.setdefault(key, {
                "vendor_id": key[0], "vendor_name": key[1],
                "count": 0, "total_amount": 0,
                "by_category": {},
                "has_evidence_count": 0, "no_evidence_count": 0,
                "latest_date": None,
            })
            g["count"] += 1
            g["total_amount"] += e_amount
            g["by_category"][e_category] = g["by_category"].get(e_category, 0) + e_amount
            if e_has_evidence:
                g["has_evidence_count"] += 1
            else:
                g["no_evidence_count"] += 1
            if not g["latest_date"] or (e_date and e_date > g["latest_date"]):
                g["latest_date"] = e_date

        # 금액 큰 순 정렬
        rows = sorted(groups.values(), key=lambda x: x["total_amount"], reverse=True)
        total_all = sum(r["total_amount"] for r in rows)
        total_count = sum(r["count"] for r in rows)

    return templates.TemplateResponse(request, "expenses_by_vendor.html", {
        "user": user, "rows": rows,
        "total_all": total_all, "total_count": total_count,
    })


@router.get("/expenses/by-vendor/{vid}", response_class=HTMLResponse)
def expenses_by_vendor_detail(request: Request, vid: int):
    """특정 거래처의 비용 내역 상세"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        vendor = s.get(Vendor, vid)
        if not vendor:
            raise HTTPException(404, "거래처를 찾을 수 없습니다.")
        items = s.exec(
            select(Expense).where(Expense.vendor_id == vid)
            .order_by(Expense.expense_date.desc())
        ).all()
        rows = []
        for e in items:
            p = s.get(Project, e.project_id)
            rows.append({
                "id": e.id, "date": e.expense_date,
                "project_name": p.name if p else "?",
                "project_id": e.project_id,
                "category": e.category,
                "description": e.description, "amount": e.amount,
                "payment_method": e.payment_method,
                "has_evidence": e.has_evidence if e.has_evidence is not None else True,
            })
        total = sum(r["amount"] for r in rows)
    return templates.TemplateResponse(request, "expenses_by_vendor_detail.html", {
        "user": user, "vendor": vendor, "rows": rows, "total": total,
    })


@router.get("/expenses/new", response_class=HTMLResponse)
def expense_new(request: Request, project_id: Optional[int] = None):
    """비용 등록 — ?project_id=N 으로 들어오면 해당 프로젝트가 자동 선택됨"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        projects_raw = s.exec(
            select(Project)
            .where(Project.status.in_(["준비중", "진행중", "완료"]))
            .order_by(Project.created_at.desc(), Project.id.desc())
        ).all()
        # 프로젝트별 거래처명까지 함께 — 검색 시 표시용
        vendors_map = {v.id: v.name for v in s.exec(select(Vendor)).all()}
        projects = [{
            "id": p.id, "name": p.name, "code": p.code,
            "event_date": p.event_date,
            "vendor_name": vendors_map.get(p.vendor_id, "") if p.vendor_id else "",
        } for p in projects_raw]
        # 거래처 마스터 목록 (비용용)
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        vendors_data = [{"id": v.id, "name": v.name} for v in vendors]
        # 미리 선택할 프로젝트
        preselect = None
        if project_id:
            for p in projects:
                if p["id"] == project_id:
                    preselect = p
                    break
    return templates.TemplateResponse(request, "expense_new.html", {
        "user": user, "projects": projects, "vendors": vendors_data,
        "today": date.today(),
        "preselect_project": preselect,
    })


@router.post("/expenses/new")
async def expense_create(
    request: Request,
    project_id: int = Form(...),
    expense_date: str = Form(...),
    category: str = Form(...),
    vendor_id: str = Form(""),       # 거래처 마스터 ID (선택)
    vendor_name: str = Form(""),     # 직접 입력값 (vendor_id 없을 때)
    description: str = Form(...),
    amount: str = Form("0"),
    payment_method: str = Form("현금"),
    has_evidence: str = Form("yes"),
    tax_excluded_note: str = Form(""),
    receipt: UploadFile = File(None),
):
    user = _user(request)
    amount = _to_int(amount, 0)
    receipt_filename = ""

    # vendor_id 파싱 (빈 문자열이면 None)
    try:
        v_id = int(vendor_id) if vendor_id and vendor_id.strip() else None
    except (ValueError, TypeError):
        v_id = None
    if receipt and receipt.filename:
        # 이미지 저장 + 리사이즈
        ext = os.path.splitext(receipt.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
            ext = ".jpg"
        receipt_filename = f"{uuid.uuid4().hex}{ext}"
        save_path = RECEIPT_DIR / receipt_filename
        save_path.parent.mkdir(parents=True, exist_ok=True)
        content = await receipt.read()
        save_path.write_bytes(content)
        # 리사이즈 (긴 변 1600px)
        try:
            img = Image.open(save_path)
            img.thumbnail((1600, 1600), Image.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(save_path, quality=85, optimize=True)
        except Exception:
            pass

    # 날짜 안전 파싱
    parsed_date = None
    if expense_date:
        try:
            parsed_date = datetime.strptime(expense_date.strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            parsed_date = None
    if not parsed_date:
        parsed_date = date.today()
    with Session(engine, expire_on_commit=False) as s:
        # vendor_id가 있으면 거래처 이름을 마스터에서 가져와 캐시
        final_vendor_name = (vendor_name or "").strip()
        if v_id:
            v_obj = s.get(Vendor, v_id)
            if v_obj:
                final_vendor_name = v_obj.name
            else:
                v_id = None  # 잘못된 ID
        e = Expense(
            project_id=project_id,
            expense_date=parsed_date,
            category=(category or "기타").strip(),
            vendor_id=v_id, vendor_name=final_vendor_name,
            description=(description or "").strip(), amount=amount,
            payment_method=(payment_method or "현금").strip(),
            receipt_image=receipt_filename,
            has_evidence=(has_evidence != "no"),
            tax_excluded_note=tax_excluded_note.strip() if has_evidence == "no" else "",
            registered_by=user.id if user else None,
        )
        s.add(e)
        s.commit()
    return RedirectResponse("/expenses", status_code=303)


@router.get("/expenses/{eid}/edit", response_class=HTMLResponse)
def expense_edit(request: Request, eid: int):
    """비용 수정 화면 — 모든 필드를 안전하게 기본값으로 채워서 템플릿 렌더 오류 방지"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if not e:
            raise HTTPException(404, "해당 비용 내역을 찾을 수 없습니다.")
        # 모든 필드에 안전한 기본값 — None / 누락 컬럼 / 잘못된 타입에도 견고
        ed = {
            "id": e.id,
            "project_id": e.project_id or 0,
            "expense_date": e.expense_date if e.expense_date else date.today(),
            "category": (e.category or "기타"),
            "vendor_id": getattr(e, "vendor_id", None),       # DB에 컬럼 없으면 None
            "vendor_name": (getattr(e, "vendor_name", "") or ""),
            "description": (e.description or ""),
            "amount": int(e.amount or 0),
            "payment_method": (e.payment_method or "현금"),
            "receipt_image": (e.receipt_image or ""),
            "has_evidence": True if getattr(e, "has_evidence", True) in (True, 1, None) else False,
            "tax_excluded_note": (getattr(e, "tax_excluded_note", "") or ""),
        }
        # 프로젝트/거래처 목록 — 빈 결과여도 정상 렌더
        try:
            projects = s.exec(select(Project).order_by(Project.created_at.desc(), Project.id.desc())).all()
        except Exception:
            projects = s.exec(select(Project)).all()
        projects_data = [{"id": p.id, "name": p.name or ""} for p in projects]
        # 현재 expense의 project가 목록에 없는 경우(취소/숨김 등) 추가
        if ed["project_id"] and not any(p["id"] == ed["project_id"] for p in projects_data):
            p_curr = s.get(Project, ed["project_id"])
            if p_curr:
                projects_data.insert(0, {"id": p_curr.id, "name": (p_curr.name or "") + " (현재)"})
        try:
            vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        except Exception:
            vendors = []
        vendors_data = [{"id": v.id, "name": v.name or ""} for v in vendors]
    return templates.TemplateResponse(request, "expense_edit.html", {
        "user": user, "e": ed, "projects": projects_data, "vendors": vendors_data,
    })


@router.post("/expenses/{eid}/edit")
async def expense_update(
    request: Request, eid: int,
    project_id: str = Form("0"),
    expense_date: str = Form(""),
    category: str = Form("기타"),
    vendor_id: str = Form(""),
    vendor_name: str = Form(""),
    description: str = Form(""),
    amount: str = Form("0"),
    payment_method: str = Form("현금"),
    has_evidence: str = Form("yes"),
    tax_excluded_note: str = Form(""),
    receipt: UploadFile = File(None),
    remove_receipt: str = Form(""),
):
    """비용 수정 처리 — 모든 입력에 안전 기본값 적용. 잘못된 입력은 400으로 변환 (500 방지)."""
    _user(request)
    amount = _to_int(amount, 0)
    # project_id 안전 파싱
    try:
        pid = int(str(project_id).strip())
    except (ValueError, TypeError):
        raise HTTPException(400, "프로젝트를 선택해주세요.")
    # 날짜 안전 파싱
    parsed_date = None
    if expense_date:
        try:
            parsed_date = datetime.strptime(expense_date.strip(), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            parsed_date = None
    if not parsed_date:
        parsed_date = date.today()
    # vendor_id 안전 파싱
    try:
        v_id = int(vendor_id) if vendor_id and str(vendor_id).strip() else None
    except (ValueError, TypeError):
        v_id = None
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if not e:
            raise HTTPException(404, "해당 비용 내역을 찾을 수 없습니다.")
        # vendor_id → vendor_name 자동 동기화
        final_vendor_name = (vendor_name or "").strip()
        if v_id:
            v_obj = s.get(Vendor, v_id)
            if v_obj:
                final_vendor_name = v_obj.name
            else:
                v_id = None
        e.project_id = pid
        e.expense_date = parsed_date
        e.category = (category or "기타").strip()
        e.vendor_id = v_id
        e.vendor_name = final_vendor_name
        e.description = (description or "").strip()
        e.amount = amount
        e.payment_method = (payment_method or "현금").strip()
        e.has_evidence = (has_evidence != "no")
        e.tax_excluded_note = tax_excluded_note.strip() if has_evidence == "no" else ""

        # 영수증 사진 처리
        if remove_receipt == "yes" and e.receipt_image:
            try:
                (RECEIPT_DIR / e.receipt_image).unlink(missing_ok=True)
            except Exception:
                pass
            e.receipt_image = ""
        if receipt and receipt.filename:
            # 기존 영수증 삭제
            if e.receipt_image:
                try:
                    (RECEIPT_DIR / e.receipt_image).unlink(missing_ok=True)
                except Exception:
                    pass
            ext = os.path.splitext(receipt.filename)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
                ext = ".jpg"
            new_name = f"{uuid.uuid4().hex}{ext}"
            save_path = RECEIPT_DIR / new_name
            save_path.parent.mkdir(parents=True, exist_ok=True)
            content = await receipt.read()
            save_path.write_bytes(content)
            try:
                img = Image.open(save_path)
                img.thumbnail((1600, 1600), Image.LANCZOS)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(save_path, quality=85, optimize=True)
            except Exception:
                pass
            e.receipt_image = new_name

        s.add(e)
        s.commit()
    return RedirectResponse("/expenses", status_code=303)


@router.post("/expenses/{eid}/delete")
def expense_delete(request: Request, eid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if e:
            if e.receipt_image:
                try:
                    (RECEIPT_DIR / e.receipt_image).unlink(missing_ok=True)
                except Exception:
                    pass
            s.delete(e)
            s.commit()
    return RedirectResponse("/expenses", status_code=303)
