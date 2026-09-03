"""하청/외주 지급 관리 (v2026.07 재정의)

기존 '비용처리(밥값/기름값/재료비)' → '거래처에 지급해야 하는 하청·외주비'로 성격 전환.
인건비(Attendance)와 동일한 지급 관리 패턴:
  - pay_status: 미지급 / 지급완료
  - pay_date: 실제 지급일
  - 목록에서 원클릭 지급 토글
"""
import os
import uuid
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select
from PIL import Image

from database import engine, Expense, Project, User, Vendor, SUBCONTRACT_ROLES
from template_utils import templates

router = APIRouter()
RECEIPT_DIR = Path(os.environ.get("DATA_DIR", "data")) / "receipts"


def _to_int(value, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    s = str(value).strip().replace(",", "").replace(" ", "").replace("원", "")
    if not s:
        return default
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def _parse_date(v, default=None):
    """안전 날짜 파싱"""
    if not v or not str(v).strip():
        return default
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return default


VAT_RATE = 0.1  # 부가세 10%


def _calc_vat(amount: int, mode: str):
    """입력 금액 + 부가세 모드 → (공급가, 부가세, 총액)
    supply : 입력값=공급가 → 부가세 10% 추가
    total  : 입력값=총액   → 공급가/부가세 분리
    none   : 부가세 없음
    """
    amount = int(amount or 0)
    if amount <= 0:
        return 0, 0, 0
    if mode == "total":
        supply = round(amount / (1 + VAT_RATE))
        vat = amount - supply
        return int(supply), int(vat), amount
    if mode == "none":
        return amount, 0, amount
    # supply (기본)
    vat = round(amount * VAT_RATE)
    return amount, int(vat), amount + int(vat)


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        user = s.get(User, uid)
    if user:
        from permissions import has_permission
        if not has_permission(user, "expenses"):
            raise HTTPException(403, "하청/외주 지급 관리 권한이 없습니다.")
    return user


# ============================================================
# 목록
# ============================================================

@router.get("/expenses", response_class=HTMLResponse)
def expense_list(request: Request, project_id: str = "", q: str = "", pay: str = ""):
    """하청/외주 지급 목록
    project_id: 프로젝트 필터
    q: 키워드 검색
    pay: '미지급' / '지급완료' 필터
    """
    user = _user(request)
    try:
        pid = int(project_id) if project_id and str(project_id).strip() else None
    except (ValueError, TypeError):
        pid = None
    search_q = (q or "").strip()
    pay_filter = (pay or "").strip()

    with Session(engine, expire_on_commit=False) as s:
        stmt = select(Expense).order_by(Expense.expense_date.desc(), Expense.id.desc())
        if pid:
            stmt = stmt.where(Expense.project_id == pid)
        items = s.exec(stmt).all()

        projects_map = {p.id: p for p in s.exec(select(Project)).all()}
        vendors_map = {v.id: v for v in s.exec(select(Vendor)).all()}

        # 키워드 검색
        if search_q:
            ql = search_q.lower()
            filtered = []
            for e in items:
                p = projects_map.get(e.project_id)
                searchable = " ".join([
                    e.description or "", e.vendor_name or "",
                    e.category or "", getattr(e, "spec_detail", "") or "",
                    getattr(e, "pay_memo", "") or "",
                    p.name if p else "", str(e.amount or ""),
                ]).lower()
                if ql in searchable:
                    filtered.append(e)
            items = filtered

        # 지급상태 필터
        if pay_filter in ("미지급", "지급완료"):
            items = [e for e in items if (getattr(e, "pay_status", "미지급") or "미지급") == pay_filter]

        rows = []
        for e in items:
            p = projects_map.get(e.project_id)
            v = vendors_map.get(e.vendor_id) if e.vendor_id else None
            pay_st = getattr(e, "pay_status", "미지급") or "미지급"
            due = getattr(e, "pay_due_date", None)
            rows.append({
                "id": e.id,
                "date": e.expense_date,
                "project_id": e.project_id,
                "project_name": p.name if p else "?",
                "project_code": p.code if p else "",
                "category": e.category or "기타",
                "vendor_name": (v.name if v else (e.vendor_name or "미지정")),
                "vendor_phone": v.phone if v else "",
                "description": e.description or "",
                "spec_detail": getattr(e, "spec_detail", "") or "",
                "amount": e.amount or 0,
                "supply_amount": getattr(e, "supply_amount", 0) or 0,
                "vat_amount": getattr(e, "vat_amount", 0) or 0,
                "vat_mode": getattr(e, "vat_mode", "none") or "none",
                "payment_method": e.payment_method or "",
                "has_tax_invoice": bool(getattr(e, "has_tax_invoice", False)),
                "receipt": e.receipt_image or "",
                "pay_status": pay_st,
                "pay_due_date": due,
                "pay_date": getattr(e, "pay_date", None),
                "pay_memo": getattr(e, "pay_memo", "") or "",
                "is_overdue": bool(due and due < date.today() and pay_st == "미지급"),
            })

        total = sum(r["amount"] for r in rows)
        total_supply = sum(r["supply_amount"] for r in rows)
        total_vat = sum(r["vat_amount"] for r in rows)
        unpaid_rows = [r for r in rows if r["pay_status"] == "미지급"]
        unpaid_total = sum(r["amount"] for r in unpaid_rows)
        unpaid_count = len(unpaid_rows)
        paid_total = total - unpaid_total
        paid_count = len(rows) - unpaid_count
        overdue_count = sum(1 for r in rows if r["is_overdue"])

        # 프로젝트 목록 (필터용)
        projects_raw = s.exec(
            select(Project).order_by(Project.created_at.desc(), Project.id.desc())
        ).all()
        projects = [{
            "id": p.id, "name": p.name, "code": p.code or "",
            "event_date": p.event_date,
            "vendor_name": vendors_map[p.vendor_id].name if (p.vendor_id and p.vendor_id in vendors_map) else "",
        } for p in projects_raw]
        selected_project = next((p for p in projects if p["id"] == pid), None) if pid else None

    response = templates.TemplateResponse(request, "expenses.html", {
        "user": user, "rows": rows, "total": total,
        "projects": projects, "selected_project_id": pid,
        "selected_project": selected_project, "search_q": search_q,
        "pay_filter": pay_filter,
        "unpaid_total": unpaid_total, "unpaid_count": unpaid_count,
        "paid_total": paid_total, "paid_count": paid_count,
        "total_supply": total_supply, "total_vat": total_vat,
        "overdue_count": overdue_count,
        "today": date.today(),
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


# ============================================================
# 거래처별 미지급 집계
# ============================================================

@router.get("/expenses/unpaid", response_class=HTMLResponse)
def expenses_unpaid(request: Request):
    """거래처별 미지급 집계 — 누구에게 얼마 줘야 하는지 한눈에"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        items = s.exec(
            select(Expense).where(Expense.pay_status == "미지급")
            .order_by(Expense.expense_date.desc())
        ).all()
        vendors_map = {v.id: v for v in s.exec(select(Vendor)).all()}
        projects_map = {p.id: p for p in s.exec(select(Project)).all()}

        groups = {}
        for e in items:
            vid = e.vendor_id or 0
            vname = (vendors_map[vid].name if vid in vendors_map
                     else (e.vendor_name or "거래처 미지정"))
            g = groups.setdefault(vid, {
                "vendor_id": vid, "vendor_name": vname,
                "vendor_phone": vendors_map[vid].phone if vid in vendors_map else "",
                "vendor_bank": vendors_map[vid].memo if vid in vendors_map else "",
                "count": 0, "total_amount": 0, "rows": [],
                "earliest_due": None, "overdue_count": 0,
            })
            p = projects_map.get(e.project_id)
            due = getattr(e, "pay_due_date", None)
            is_overdue = bool(due and due < date.today())
            g["count"] += 1
            g["total_amount"] += (e.amount or 0)
            if is_overdue:
                g["overdue_count"] += 1
            if due and (g["earliest_due"] is None or due < g["earliest_due"]):
                g["earliest_due"] = due
            g["rows"].append({
                "id": e.id, "date": e.expense_date,
                "project_id": e.project_id,
                "project_name": p.name if p else "?",
                "category": e.category or "기타",
                "description": e.description or "",
                "spec_detail": getattr(e, "spec_detail", "") or "",
                "amount": e.amount or 0,
                "supply_amount": getattr(e, "supply_amount", 0) or 0,
                "vat_amount": getattr(e, "vat_amount", 0) or 0,
                "pay_due_date": due,
                "pay_memo": getattr(e, "pay_memo", "") or "",
                "has_tax_invoice": bool(getattr(e, "has_tax_invoice", False)),
                "is_overdue": is_overdue,
            })

        vendor_groups = sorted(groups.values(), key=lambda x: x["total_amount"], reverse=True)
        grand_total = sum(g["total_amount"] for g in vendor_groups)
        grand_count = sum(g["count"] for g in vendor_groups)
        overdue_total = sum(
            it["amount"] for g in vendor_groups for it in g["rows"] if it["is_overdue"]
        )

    response = templates.TemplateResponse(request, "expenses_unpaid.html", {
        "user": user, "vendor_groups": vendor_groups,
        "grand_total": grand_total, "grand_count": grand_count,
        "overdue_total": overdue_total,
        "today": date.today(),
    })
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


# ============================================================
# 지급 상태 토글 (인건비와 동일 UX)
# ============================================================

@router.post("/expenses/{eid}/toggle-pay")
def expense_toggle_pay(request: Request, eid: int):
    """미지급 ↔ 지급완료 토글"""
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if not e:
            raise HTTPException(404)
        current = getattr(e, "pay_status", "미지급") or "미지급"
        if current == "미지급":
            e.pay_status = "지급완료"
            e.pay_date = date.today()
        else:
            e.pay_status = "미지급"
            e.pay_date = None
        s.add(e)
        s.commit()
    referer = request.headers.get("referer", "/expenses")
    return RedirectResponse(referer, status_code=303)


@router.post("/expenses/bulk-pay")
def expenses_bulk_pay(request: Request, expense_ids: str = Form(...), action: str = Form("pay")):
    """선택 항목 일괄 지급완료 / 미지급 처리"""
    _user(request)
    try:
        ids = [int(x) for x in expense_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "잘못된 ID 형식")
    updated = 0
    with Session(engine, expire_on_commit=False) as s:
        for eid in ids:
            e = s.get(Expense, eid)
            if not e:
                continue
            if action == "pay":
                e.pay_status = "지급완료"
                e.pay_date = date.today()
            else:
                e.pay_status = "미지급"
                e.pay_date = None
            s.add(e)
            updated += 1
        s.commit()
    return RedirectResponse(f"/expenses?bulk={updated}", status_code=303)


# ============================================================
# 등록
# ============================================================

@router.get("/expenses/new", response_class=HTMLResponse)
def expense_new(request: Request, project_id: Optional[int] = None):
    """하청/외주 지급 등록 화면"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        projects_raw = s.exec(
            select(Project)
            .where(Project.status.in_(["준비중", "진행중", "완료"]))
            .order_by(Project.created_at.desc(), Project.id.desc())
        ).all()
        vendors_map = {v.id: v.name for v in s.exec(select(Vendor)).all()}
        projects = [{
            "id": p.id, "name": p.name, "code": p.code,
            "event_date": p.event_date,
            "vendor_name": vendors_map.get(p.vendor_id, "") if p.vendor_id else "",
        } for p in projects_raw]
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        vendors_data = [{"id": v.id, "name": v.name, "phone": v.phone or ""} for v in vendors]
        preselect = next((p for p in projects if p["id"] == project_id), None) if project_id else None

    return_to = f"/projects/{project_id}" if project_id else ""
    return templates.TemplateResponse(request, "expense_new.html", {
        "user": user, "projects": projects, "vendors": vendors_data,
        "today": date.today(),
        "preselect_project": preselect,
        "return_to": return_to,
        "roles": SUBCONTRACT_ROLES,
    })


@router.post("/expenses/new")
async def expense_create(
    request: Request,
    project_id: str = Form(""),
    expense_date: str = Form(""),
    category: str = Form("기타"),
    vendor_id: str = Form(""),
    vendor_name: str = Form(""),
    description: str = Form(""),
    amount: str = Form("0"),
    vat_mode: str = Form("supply"),
    spec_detail: str = Form(""),
    payment_method: str = Form("계좌이체"),
    has_tax_invoice: str = Form(""),
    pay_status: str = Form("미지급"),
    pay_due_date: str = Form(""),
    pay_memo: str = Form(""),
    return_to: str = Form(""),
    receipt: UploadFile = File(None),
):
    """하청/외주 지급 등록"""
    user = _user(request)

    # 프로젝트 필수
    try:
        pid = int(str(project_id).strip()) if project_id and str(project_id).strip() else 0
    except (ValueError, TypeError):
        pid = 0
    if pid <= 0:
        raise HTTPException(400, "프로젝트를 선택해주세요.")

    if not (description or "").strip():
        raise HTTPException(400, "작업 내용을 입력해주세요.")

    amt_input = _to_int(amount, 0)
    vmode = vat_mode if vat_mode in ("supply", "total", "none") else "supply"
    supply_amt, vat_amt, total_amt = _calc_vat(amt_input, vmode)

    # 거래처
    try:
        v_id = int(vendor_id) if vendor_id and str(vendor_id).strip() else None
    except (ValueError, TypeError):
        v_id = None

    # 날짜
    exp_date = _parse_date(expense_date, date.today())
    due_date = _parse_date(pay_due_date, None)

    # 지급 상태
    final_pay_status = pay_status if pay_status in ("미지급", "지급완료") else "미지급"
    final_pay_date = date.today() if final_pay_status == "지급완료" else None

    # 영수증/계약서 이미지
    receipt_filename = ""
    if receipt and receipt.filename:
        ext = os.path.splitext(receipt.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
            ext = ".jpg"
        receipt_filename = f"{uuid.uuid4().hex}{ext}"
        save_path = RECEIPT_DIR / receipt_filename
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

    with Session(engine, expire_on_commit=False) as s:
        final_vendor_name = (vendor_name or "").strip()
        if v_id:
            v_obj = s.get(Vendor, v_id)
            if v_obj:
                final_vendor_name = v_obj.name
            else:
                v_id = None

        e = Expense(
            project_id=pid,
            expense_date=exp_date,
            category=(category or "기타").strip(),
            vendor_id=v_id,
            vendor_name=final_vendor_name,
            description=(description or "").strip(),
            amount=total_amt,
            vat_mode=vmode,
            supply_amount=supply_amt,
            vat_amount=vat_amt,
            spec_detail=(spec_detail or "").strip(),
            payment_method=(payment_method or "계좌이체").strip(),
            has_tax_invoice=(has_tax_invoice == "yes"),
            receipt_image=receipt_filename,
            pay_status=final_pay_status,
            pay_due_date=due_date,
            pay_date=final_pay_date,
            pay_memo=(pay_memo or "").strip(),
            registered_by=user.id if user else None,
        )
        try:
            s.add(e)
            s.commit()
        except Exception as ex:
            s.rollback()
            raise HTTPException(400, f"저장 실패: {type(ex).__name__} — {str(ex)[:200]}")

    if return_to and return_to.startswith("/"):
        return RedirectResponse(return_to, status_code=303)
    return RedirectResponse("/expenses", status_code=303)


# ============================================================
# 수정
# ============================================================

@router.get("/expenses/{eid}/edit", response_class=HTMLResponse)
def expense_edit(request: Request, eid: int):
    """하청/외주 지급 수정 화면"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if not e:
            raise HTTPException(404, "해당 지급 내역을 찾을 수 없습니다.")
        ed = {
            "id": e.id,
            "project_id": e.project_id or 0,
            "expense_date": e.expense_date or date.today(),
            "category": e.category or "기타",
            "vendor_id": getattr(e, "vendor_id", None),
            "vendor_name": getattr(e, "vendor_name", "") or "",
            "description": e.description or "",
            "amount": int(e.amount or 0),
            "vat_mode": getattr(e, "vat_mode", "none") or "none",
            "supply_amount": int(getattr(e, "supply_amount", 0) or 0),
            "vat_amount": int(getattr(e, "vat_amount", 0) or 0),
            "spec_detail": getattr(e, "spec_detail", "") or "",
            "payment_method": e.payment_method or "계좌이체",
            "has_tax_invoice": bool(getattr(e, "has_tax_invoice", False)),
            "receipt_image": e.receipt_image or "",
            "pay_status": getattr(e, "pay_status", "미지급") or "미지급",
            "pay_due_date": getattr(e, "pay_due_date", None),
            "pay_date": getattr(e, "pay_date", None),
            "pay_memo": getattr(e, "pay_memo", "") or "",
        }
        projects_raw = s.exec(
            select(Project).order_by(Project.created_at.desc(), Project.id.desc())
        ).all()
        projects_data = [{"id": p.id, "name": p.name or "", "code": p.code or ""} for p in projects_raw]
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        vendors_data = [{"id": v.id, "name": v.name or ""} for v in vendors]

    return templates.TemplateResponse(request, "expense_edit.html", {
        "user": user, "e": ed, "projects": projects_data, "vendors": vendors_data,
        "roles": SUBCONTRACT_ROLES,
    })


@router.post("/expenses/{eid}/edit")
async def expense_update(
    request: Request, eid: int,
    project_id: str = Form(""),
    expense_date: str = Form(""),
    category: str = Form("기타"),
    vendor_id: str = Form(""),
    vendor_name: str = Form(""),
    description: str = Form(""),
    amount: str = Form("0"),
    vat_mode: str = Form("supply"),
    spec_detail: str = Form(""),
    payment_method: str = Form("계좌이체"),
    has_tax_invoice: str = Form(""),
    pay_status: str = Form("미지급"),
    pay_due_date: str = Form(""),
    pay_memo: str = Form(""),
    receipt: UploadFile = File(None),
    remove_receipt: str = Form(""),
):
    """하청/외주 지급 수정"""
    _user(request)
    try:
        pid = int(str(project_id).strip()) if project_id and str(project_id).strip() else 0
    except (ValueError, TypeError):
        pid = 0
    if pid <= 0:
        raise HTTPException(400, "프로젝트를 선택해주세요.")

    amt_input = _to_int(amount, 0)
    vmode = vat_mode if vat_mode in ("supply", "total", "none") else "supply"
    supply_amt, vat_amt, total_amt = _calc_vat(amt_input, vmode)
    try:
        v_id = int(vendor_id) if vendor_id and str(vendor_id).strip() else None
    except (ValueError, TypeError):
        v_id = None

    exp_date = _parse_date(expense_date, date.today())
    due_date = _parse_date(pay_due_date, None)

    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if not e:
            raise HTTPException(404, "해당 지급 내역을 찾을 수 없습니다.")

        final_vendor_name = (vendor_name or "").strip()
        if v_id:
            v_obj = s.get(Vendor, v_id)
            if v_obj:
                final_vendor_name = v_obj.name
            else:
                v_id = None

        prev_status = getattr(e, "pay_status", "미지급") or "미지급"
        new_status = pay_status if pay_status in ("미지급", "지급완료") else "미지급"

        e.project_id = pid
        e.expense_date = exp_date
        e.category = (category or "기타").strip()
        e.vendor_id = v_id
        e.vendor_name = final_vendor_name
        e.description = (description or "").strip()
        e.amount = total_amt
        e.vat_mode = vmode
        e.supply_amount = supply_amt
        e.vat_amount = vat_amt
        e.spec_detail = (spec_detail or "").strip()
        e.payment_method = (payment_method or "계좌이체").strip()
        e.has_tax_invoice = (has_tax_invoice == "yes")
        e.pay_status = new_status
        e.pay_due_date = due_date
        e.pay_memo = (pay_memo or "").strip()

        # 지급일 자동 관리
        if new_status == "지급완료":
            if prev_status == "미지급" or not e.pay_date:
                e.pay_date = date.today()
        else:
            e.pay_date = None

        # 이미지 처리
        if remove_receipt == "yes" and e.receipt_image:
            try:
                (RECEIPT_DIR / e.receipt_image).unlink(missing_ok=True)
            except Exception:
                pass
            e.receipt_image = ""
        if receipt and receipt.filename:
            if e.receipt_image:
                try:
                    (RECEIPT_DIR / e.receipt_image).unlink(missing_ok=True)
                except Exception:
                    pass
            ext = os.path.splitext(receipt.filename)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
                ext = ".jpg"
            fn = f"{uuid.uuid4().hex}{ext}"
            sp = RECEIPT_DIR / fn
            sp.parent.mkdir(parents=True, exist_ok=True)
            content = await receipt.read()
            sp.write_bytes(content)
            try:
                img = Image.open(sp)
                img.thumbnail((1600, 1600), Image.LANCZOS)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(sp, quality=85, optimize=True)
            except Exception:
                pass
            e.receipt_image = fn

        try:
            s.add(e)
            s.commit()
        except Exception as ex:
            s.rollback()
            raise HTTPException(400, f"수정 실패: {type(ex).__name__} — {str(ex)[:200]}")

    return RedirectResponse("/expenses", status_code=303)


@router.post("/expenses/{eid}/delete")
def expense_delete(request: Request, eid: int):
    """지급 내역 삭제"""
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


# ============================================================
# 영수증/계약서 이미지 서빙
# ============================================================

@router.get("/receipts/{filename}")
def serve_receipt(filename: str):
    from fastapi.responses import FileResponse
    path = RECEIPT_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ============================================================
# 거래처별 집계 (기존 호환)
# ============================================================

@router.get("/expenses/by-vendor", response_class=HTMLResponse)
def expenses_by_vendor(request: Request):
    """거래처별 하청/외주 지급 집계"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        all_exp = s.exec(select(Expense)).all()
        vendors = {v.id: v for v in s.exec(select(Vendor)).all()}

        groups = {}
        for e in all_exp:
            vid = e.vendor_id or 0
            vname = (vendors[vid].name if vid in vendors
                     else (getattr(e, "vendor_name", "") or "거래처 미지정"))
            g = groups.setdefault(vid, {
                "vendor_id": vid, "vendor_name": vname,
                "count": 0, "total_amount": 0,
                "unpaid_count": 0, "unpaid_amount": 0,
                "paid_count": 0, "paid_amount": 0,
                "by_category": {},
                "latest_date": None,
            })
            amt = e.amount or 0
            cat = e.category or "기타"
            pay_st = getattr(e, "pay_status", "미지급") or "미지급"
            g["count"] += 1
            g["total_amount"] += amt
            g["by_category"][cat] = g["by_category"].get(cat, 0) + amt
            if pay_st == "미지급":
                g["unpaid_count"] += 1
                g["unpaid_amount"] += amt
            else:
                g["paid_count"] += 1
                g["paid_amount"] += amt
            if e.expense_date and (not g["latest_date"] or e.expense_date > g["latest_date"]):
                g["latest_date"] = e.expense_date

        rows = sorted(groups.values(), key=lambda x: x["total_amount"], reverse=True)
        grand_total = sum(r["total_amount"] for r in rows)
        grand_unpaid = sum(r["unpaid_amount"] for r in rows)

    return templates.TemplateResponse(request, "expenses_by_vendor.html", {
        "user": user, "rows": rows,
        "grand_total": grand_total, "grand_unpaid": grand_unpaid,
        "today": date.today(),
    })
