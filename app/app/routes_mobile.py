"""모바일 PWA 전용 라우트 — /m/ prefix"""
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select
import os
import uuid

from database import (
    engine, User, Project, Equipment, EquipmentUnit, MaintenanceLog,
    Worker, Attendance, Expense, Vendor,
)
from template_utils import templates

router = APIRouter(prefix="/m")


def _user(request: Request) -> User:
    u = request.state.user
    if not u:
        raise HTTPException(status_code=303, headers={"Location": "/login?next=/m/"})
    return u


# ============================================================
# 모바일 홈 (간소화된 대시보드)
# ============================================================
@router.get("/", response_class=HTMLResponse)
def mobile_home(request: Request):
    user = _user(request)
    today = date.today()
    with Session(engine, expire_on_commit=False) as s:
        active_projects = s.exec(
            select(Project).where(Project.status == "진행중").order_by(Project.event_date.desc())
        ).all()
        active_count = len(active_projects)
        # 오늘 근태
        today_att = s.exec(
            select(Attendance).where(Attendance.work_date == today)
        ).all()
        today_workers = len(today_att)
        # 미수금 프로젝트
        unpaid = s.exec(
            select(Project).where(Project.settlement_status == "미수금")
        ).all()
        unpaid_count = len(unpaid)
        # 매출 / 부가세 합계 (전체)
        all_projects = s.exec(select(Project)).all()
        total_supply = sum((p.supply_amount or p.revenue) for p in all_projects)
        total_vat = sum((p.vat_amount or 0) for p in all_projects)
        total_revenue = sum(p.revenue for p in all_projects)
        # 장비 상태
        units = s.exec(select(EquipmentUnit)).all()
        eq_on_rent = sum(1 for u in units if u.status == "대여중")
        eq_repair = sum(1 for u in units if u.status == "수리중")
        eq_total = len(units)

    return templates.TemplateResponse(request, "mobile/home.html", {
        "user": user,
        "active_count": active_count,
        "today_workers": today_workers,
        "unpaid_count": unpaid_count,
        "total_supply": total_supply,
        "total_vat": total_vat,
        "total_revenue": total_revenue,
        "eq_on_rent": eq_on_rent,
        "eq_repair": eq_repair,
        "eq_total": eq_total,
        "active_projects": active_projects[:5],
    })


# ============================================================
# QR 스캐너
# ============================================================
@router.get("/scan", response_class=HTMLResponse)
def mobile_scan(request: Request):
    _user(request)
    return templates.TemplateResponse(request, "mobile/scan.html", {})


@router.get("/scan/lookup")
def mobile_scan_lookup(request: Request, code: str):
    """스캔된 자산코드로 장비 개체 조회 → 리다이렉트"""
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        # asset_code 또는 qr_data로 검색
        unit = s.exec(
            select(EquipmentUnit).where(EquipmentUnit.asset_code == code)
        ).first()
        if not unit:
            # qr_data 필드로도 검색 시도
            try:
                unit = s.exec(
                    select(EquipmentUnit).where(EquipmentUnit.qr_data == code)
                ).first()
            except Exception:
                pass
        if not unit:
            return JSONResponse({"ok": False, "message": f"코드 '{code}'에 해당하는 장비가 없습니다."}, status_code=404)
        return JSONResponse({"ok": True, "url": f"/m/equipment/{unit.id}", "asset_code": unit.asset_code})


# ============================================================
# 장비 상세 (모바일)
# ============================================================
@router.get("/equipment/{uid}", response_class=HTMLResponse)
def mobile_equipment(request: Request, uid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        unit = s.get(EquipmentUnit, uid)
        if not unit:
            raise HTTPException(404)
        eq = s.get(Equipment, unit.equipment_id)
        maint = s.exec(
            select(MaintenanceLog).where(MaintenanceLog.unit_id == uid)
            .order_by(MaintenanceLog.log_date.desc())
        ).all()
    return templates.TemplateResponse(request, "mobile/equipment_detail.html", {
        "user": user, "unit": unit, "eq": eq, "maint": maint,
    })


@router.post("/equipment/{uid}/maintenance")
def mobile_add_maintenance(request: Request, uid: int,
                            action_type: str = Form(...),
                            note: str = Form(""),
                            cost: int = Form(0)):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        unit = s.get(EquipmentUnit, uid)
        if not unit:
            raise HTTPException(404)
        m = MaintenanceLog(
            unit_id=uid, log_date=date.today(),
            log_type=action_type, title=action_type, description=note, cost=cost,
        )
        s.add(m)
        # 상태 자동 갱신
        if action_type in ("수리", "고장", "AS"):
            unit.status = "수리중"
        elif action_type == "점검":
            unit.last_check_date = date.today()
        s.add(unit)
        s.commit()
    return RedirectResponse(f"/m/equipment/{uid}", status_code=303)


# ============================================================
# 장비 리스트 (모바일)
# ============================================================
@router.get("/equipment", response_class=HTMLResponse)
def mobile_equipment_list(request: Request, q: str = "", expand: int = 0):
    """모바일 장비 목록 — 동일 장비는 한 종류로 묶어서 표시 (#2 요청사항).
    expand=장비ID 클릭하면 그 종류의 개체 목록 펼침.
    """
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        all_eqs = s.exec(select(Equipment).where(Equipment.is_active == True).order_by(Equipment.name)).all()
        all_units = s.exec(select(EquipmentUnit)).all()
        units_by_eq = {}
        for u in all_units:
            units_by_eq.setdefault(u.equipment_id, []).append(u)

        eq_rows = []
        for e in all_eqs:
            units = units_by_eq.get(e.id, [])
            total = len(units)
            available = sum(1 for u in units if u.status == "보유중")
            on_rent = sum(1 for u in units if u.status == "대여중")
            in_repair = sum(1 for u in units if u.status == "수리중")
            row = {
                "id": e.id, "name": e.name, "model": e.model,
                "category": e.category, "spec": e.spec,
                "total": total, "available": available,
                "on_rent": on_rent, "in_repair": in_repair,
                "units": [],  # expand 시 채움
            }
            if expand == e.id:
                row["units"] = [{
                    "id": u.id, "asset_code": u.asset_code,
                    "status": u.status, "location": u.location,
                    "serial_number": u.serial_number,
                } for u in sorted(units, key=lambda x: x.asset_code)]

            if q:
                hay = f"{row['name']} {row['model']} {row['category']} {row['spec']}".lower()
                # 개체 코드도 검색 대상에 포함
                hay += " " + " ".join(u.asset_code.lower() for u in units)
                if q.lower() not in hay:
                    continue
            eq_rows.append(row)
    return templates.TemplateResponse(request, "mobile/equipment_list.html", {
        "user": user, "eq_rows": eq_rows, "q": q, "expand_id": expand,
    })


# ============================================================
# 근태 (모바일) — 정직원 출퇴근 + 알바 일당
# ============================================================
@router.get("/attendance", response_class=HTMLResponse)
def mobile_attendance(request: Request, tab: str = "fulltime"):
    """모바일 근태 — 기본 탭은 정직원 출퇴근 (가장 자주 쓰는 기능)"""
    user = _user(request)
    today = date.today()
    with Session(engine, expire_on_commit=False) as s:
        from database import WorkerCheckin
        # 정직원 오늘 출퇴근 현황
        fulltime_workers = s.exec(
            select(Worker).where(
                Worker.is_active == True,
                Worker.employee_type == "정직원"
            ).order_by(Worker.name)
        ).all()
        workers_today = []
        for w in fulltime_workers:
            ci = s.exec(
                select(WorkerCheckin).where(
                    WorkerCheckin.worker_id == w.id,
                    WorkerCheckin.work_date == today
                )
            ).first()
            workers_today.append({
                "id": w.id, "name": w.name, "phone": w.phone,
                "check_in": ci.check_in if ci else None,
                "check_out": ci.check_out if ci else None,
            })

        # 알바 일당 (오늘 등록)
        alba_workers = s.exec(
            select(Worker).where(
                Worker.is_active == True,
                Worker.employee_type == "알바"
            ).order_by(Worker.name)
        ).all()
        projects = s.exec(
            select(Project).where(Project.status.in_(["진행중", "준비중"])).order_by(Project.event_date.desc())
        ).all()
        today_alba = s.exec(
            select(Attendance).where(Attendance.work_date == today)
        ).all()
        today_alba_rows = []
        for a in today_alba:
            w = s.get(Worker, a.worker_id)
            p = s.get(Project, a.project_id)
            today_alba_rows.append({
                "id": a.id,
                "worker_name": w.name if w else "?",
                "project_name": p.name if p else "?",
                "days": a.days,
                "total_wage": a.total_wage,
            })

    return templates.TemplateResponse(request, "mobile/attendance.html", {
        "user": user, "tab": tab,
        "workers_today": workers_today,
        "alba_workers": [{"id": w.id, "name": w.name, "default_daily_wage": w.default_daily_wage} for w in alba_workers],
        "projects": [{"id": p.id, "name": p.name} for p in projects],
        "today_alba_rows": today_alba_rows,
        "today": today,
    })


@router.post("/attendance/fulltime/checkin")
def mobile_ft_checkin(request: Request, worker_id: int = Form(...)):
    """모바일 정직원 출근"""
    _user(request)
    from database import WorkerCheckin
    today = date.today()
    now = datetime.now()
    with Session(engine, expire_on_commit=False) as s:
        existing = s.exec(
            select(WorkerCheckin).where(
                WorkerCheckin.worker_id == worker_id,
                WorkerCheckin.work_date == today
            )
        ).first()
        if existing:
            if not existing.check_in:
                existing.check_in = now
                s.add(existing)
        else:
            s.add(WorkerCheckin(worker_id=worker_id, work_date=today, check_in=now))
        s.commit()
    return RedirectResponse("/m/attendance?tab=fulltime&checkin=1", status_code=303)


@router.post("/attendance/fulltime/checkout")
def mobile_ft_checkout(request: Request, worker_id: int = Form(...)):
    """모바일 정직원 퇴근"""
    _user(request)
    from database import WorkerCheckin
    today = date.today()
    now = datetime.now()
    with Session(engine, expire_on_commit=False) as s:
        existing = s.exec(
            select(WorkerCheckin).where(
                WorkerCheckin.worker_id == worker_id,
                WorkerCheckin.work_date == today
            )
        ).first()
        if existing:
            existing.check_out = now
            s.add(existing)
        else:
            s.add(WorkerCheckin(worker_id=worker_id, work_date=today, check_out=now))
        s.commit()
    return RedirectResponse("/m/attendance?tab=fulltime&checkout=1", status_code=303)


@router.post("/attendance/checkin")
def mobile_checkin(request: Request,
                    worker_id: int = Form(...),
                    project_id: int = Form(...),
                    hours: float = Form(8),
                    bonus_amount: str = Form("0"),
                    bonus_memo: str = Form("")):
    """알바 일당 등록 (기존 로직 유지) + 추가금액(오퍼비) 지원"""
    _user(request)
    today = date.today()
    try:
        bonus = int(str(bonus_amount).replace(",", "").strip() or 0)
    except (ValueError, TypeError):
        bonus = 0
    with Session(engine, expire_on_commit=False) as s:
        w = s.get(Worker, worker_id)
        if not w:
            raise HTTPException(404, "근무자 없음")
        days_val = hours / 8.0
        wage_total = int(w.default_daily_wage * days_val)
        a = Attendance(
            worker_id=worker_id, project_id=project_id,
            work_date=today, days=days_val, daily_wage=w.default_daily_wage,
            total_wage=wage_total + bonus,
            bonus_amount=bonus,
            bonus_memo=(bonus_memo or "").strip(),
            pay_status="미지급",
        )
        s.add(a); s.commit()
    return RedirectResponse("/m/attendance?tab=alba", status_code=303)


# ============================================================
# 비용 빠른 등록 (모바일) + OCR
# ============================================================
@router.get("/expense/new", response_class=HTMLResponse)
def mobile_expense_new(request: Request):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        # 진행중 + 준비중 + 최근 완료 프로젝트 (모바일은 최신순)
        projects_raw = s.exec(
            select(Project)
            .where(Project.status.in_(["준비중", "진행중", "완료"]))
            .order_by(Project.created_at.desc(), Project.id.desc())
        ).all()
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        vendors_map = {v.id: v.name for v in vendors}
        projects = [{
            "id": p.id, "name": p.name, "code": p.code,
            "event_date": p.event_date,
            "vendor_name": vendors_map.get(p.vendor_id, "") if p.vendor_id else "",
        } for p in projects_raw]
    # OCR 사용 가능 여부 안내
    try:
        from ocr_utils import is_ocr_available
        ocr_ready = is_ocr_available()
    except Exception:
        ocr_ready = False
    return templates.TemplateResponse(request, "mobile/expense_new.html", {
        "user": user, "projects": projects, "vendors": vendors,
        "ocr_ready": ocr_ready,
    })


@router.post("/expense/ocr")
async def mobile_expense_ocr(request: Request, receipt: UploadFile = File(...)):
    """영수증 이미지를 받아 OCR로 금액·날짜·상호명을 추출 + 사진을 즉시 저장.
    저장된 파일명을 응답에 포함 → 폼 submit 시 이 파일명으로 expense 레코드에 연결.
    이로써 OCR + 사진 보존이 한 번에 처리됨."""
    _user(request)
    content = await receipt.read()
    # 사진을 임시 저장 (실제 expense 저장 시 이 파일명을 hidden으로 전달받음)
    saved_filename = ""
    try:
        _dd = os.environ.get("DATA_DIR", "data")
        os.makedirs(f"{_dd}/receipts", exist_ok=True)
        ext = os.path.splitext(receipt.filename or "receipt.jpg")[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
            ext = ".jpg"
        saved_filename = f"{uuid.uuid4().hex}{ext}"
        save_path = f"{_dd}/receipts/{saved_filename}"
        with open(save_path, "wb") as f:
            f.write(content)
        # 이미지 리사이즈 (선택) — 실패해도 원본 보존
        try:
            from PIL import Image
            img = Image.open(save_path)
            img.thumbnail((1600, 1600), Image.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(save_path, quality=85, optimize=True)
        except Exception:
            pass
    except Exception as e:
        print(f"[OCR] 사진 저장 실패: {e}")
        saved_filename = ""

    # OCR 분석
    try:
        from ocr_utils import extract_receipt_data
        result = extract_receipt_data(content)
    except Exception as e:
        return JSONResponse({"ok": True, "error": str(e),
                             "amount": 0, "date": "", "vendor": "",
                             "saved_filename": saved_filename})
    # OCR 결과에 저장된 파일명 포함
    result["saved_filename"] = saved_filename
    return JSONResponse(result)


@router.post("/expense/new")
async def mobile_expense_create(request: Request,
                                  project_id: str = Form("0"),
                                  category: str = Form("기타"),
                                  amount: str = Form("0"),
                                  description: str = Form(""),
                                  vendor_id: Optional[str] = Form(None),
                                  vendor_name: str = Form(""),
                                  has_evidence: str = Form("yes"),
                                  tax_excluded_note: str = Form(""),
                                  expense_date: str = Form(""),
                                  receipt_filename: str = Form(""),  # OCR 시 저장된 파일명
                                  receipt: UploadFile = File(None)):
    """모바일 비용 등록 — 입력 안전 처리. 폼 검증 실패가 500을 일으키지 않게 한다."""
    user = _user(request)
    # 안전한 amount/project_id 파싱
    try:
        amount_i = int(str(amount).replace(',', '').replace(' ', '').strip() or 0)
    except (ValueError, TypeError):
        amount_i = 0
    try:
        pid = int(str(project_id).strip())
    except (ValueError, TypeError):
        pid = 0
    if pid <= 0:
        # 잘못된 프로젝트 → 같은 화면으로 되돌림
        return RedirectResponse("/m/expense/new?err=project", status_code=303)

    # 영수증 처리 — 우선순위:
    # 1) receipt_filename (OCR 시 이미 저장된 파일) → 그대로 사용 (사진 보존!)
    # 2) receipt 새 업로드 → 저장
    receipt_path = ""
    _dd = os.environ.get("DATA_DIR", "data")
    if receipt_filename and (receipt_filename or "").strip():
        # OCR 단계에서 저장된 파일 — 존재 확인 후 사용
        saved_path = f"{_dd}/receipts/{receipt_filename.strip()}"
        if os.path.exists(saved_path):
            receipt_path = receipt_filename.strip()
    if not receipt_path and receipt and receipt.filename:
        # OCR을 안 거쳤거나 OCR 저장 실패한 경우 폼의 파일 사용
        os.makedirs(f"{_dd}/receipts", exist_ok=True)
        ext = os.path.splitext(receipt.filename)[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
            ext = ".jpg"
        fname = f"{uuid.uuid4().hex}{ext}"
        try:
            with open(f"{_dd}/receipts/{fname}", "wb") as f:
                content = await receipt.read()
                f.write(content)
            receipt_path = fname
            try:
                from PIL import Image
                img = Image.open(f"{_dd}/receipts/{fname}")
                img.thumbnail((1600, 1600), Image.LANCZOS)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(f"{_dd}/receipts/{fname}", quality=85, optimize=True)
            except Exception:
                pass
        except Exception:
            receipt_path = ""

    # 거래처: vendor_id가 있으면 마스터에서 이름 가져오고, 없으면 직접 입력값(vendor_name) 사용
    try:
        v_id = int(vendor_id) if vendor_id and str(vendor_id).strip() else None
    except (ValueError, TypeError):
        v_id = None
    final_vendor_name = (vendor_name or "").strip()
    with Session(engine, expire_on_commit=False) as s:
        if v_id:
            v = s.get(Vendor, v_id)
            if v:
                final_vendor_name = v.name
            else:
                v_id = None
        # expense_date 파싱 (OCR로 채워진 날짜 우선)
        parsed_date = date.today()
        if expense_date:
            try:
                parsed_date = datetime.strptime(expense_date.strip(), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                parsed_date = date.today()
        try:
            e = Expense(
                project_id=pid,
                category=(category or "기타").strip(),
                amount=amount_i,
                description=(description or "").strip(),
                vendor_id=v_id, vendor_name=final_vendor_name,
                expense_date=parsed_date, receipt_image=receipt_path,
                has_evidence=(has_evidence != "no"),
                tax_excluded_note=tax_excluded_note.strip() if has_evidence == "no" else "",
                registered_by=user.id if user else None,
            )
            s.add(e); s.commit()
        except Exception as ex:
            # DB 저장 실패 시도 500 대신 에러 페이지로
            return RedirectResponse(f"/m/expense/new?err=save", status_code=303)
    return RedirectResponse("/m/?expense_saved=1", status_code=303)


# ============================================================
# 프로젝트 리스트/상세 (모바일)
# ============================================================
@router.get("/projects", response_class=HTMLResponse)
def mobile_projects(request: Request):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        projects = s.exec(select(Project).order_by(Project.event_date.desc())).all()
    return templates.TemplateResponse(request, "mobile/projects.html", {
        "user": user, "projects": projects,
    })


# ============================================================
# 견적서 (모바일)
# ============================================================
@router.get("/quotes", response_class=HTMLResponse)
def mobile_quotes(request: Request):
    user = _user(request)
    from permissions import has_permission
    if not has_permission(user, "quotes"):
        raise HTTPException(403, "견적서 권한이 없습니다.")
    from database import Quote
    with Session(engine, expire_on_commit=False) as s:
        quotes = s.exec(select(Quote).order_by(Quote.quote_date.desc())).all()
        rows = [{
            "id": q.id, "quote_number": q.quote_number,
            "quote_date": q.quote_date, "quote_type": q.quote_type,
            "vendor_name": q.vendor_name, "project_name": q.project_name,
            "total": q.total, "status": q.status,
        } for q in quotes]
    return templates.TemplateResponse(request, "mobile/quotes.html", {
        "user": user, "quotes": rows,
    })


@router.get("/quote/new", response_class=HTMLResponse)
def mobile_quote_new(request: Request):
    user = _user(request)
    from permissions import has_permission
    if not has_permission(user, "quotes"):
        raise HTTPException(403, "견적서 권한이 없습니다.")
    from database import Quote, get_company_settings
    with Session(engine, expire_on_commit=False) as s:
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        # 다음 견적번호
        year = date.today().year
        count = len(s.exec(select(Quote)).all()) + 1
        default_no = f"Q-{year}-{count:03d}"
    cs = get_company_settings()
    return templates.TemplateResponse(request, "mobile/quote_new.html", {
        "user": user,
        "vendors": [{"id": v.id, "name": v.name} for v in vendors],
        "default_no": default_no,
        "today": date.today(),
        "valid_until": date.today() + timedelta(days=cs.get("default_valid_days", 30)),
    })


@router.post("/quote/new")
async def mobile_quote_create(request: Request):
    user = _user(request)
    from permissions import has_permission
    if not has_permission(user, "quotes"):
        raise HTTPException(403)
    from database import Quote, QuoteItem
    import json as _json

    form = await request.form()
    items_json = form.get("items_json", "[]")
    try:
        items = _json.loads(items_json)
    except Exception:
        items = []

    subtotal = sum(int(it.get("amount", 0) or 0) for it in items)
    try:
        discount = int(str(form.get("discount", "0")).replace(',', '').strip() or 0)
    except (ValueError, TypeError):
        discount = 0

    # 부가세 모드 분기
    vat_mode = form.get("vat_mode", "supply")
    if vat_mode == "cash":
        after_discount = max(0, subtotal - discount)
        vat = 0
        total = after_discount
    elif vat_mode == "total":
        total = max(0, subtotal - discount)
        after_discount = round(total / 1.1)
        vat = total - after_discount
    else:
        after_discount = max(0, subtotal - discount)
        vat = round(after_discount * 0.1)
        total = after_discount + vat

    v_id = form.get("vendor_id")
    v_id = int(v_id) if v_id and str(v_id).strip() else None
    vendor_name = ""
    if v_id:
        with Session(engine, expire_on_commit=False) as s:
            v = s.get(Vendor, v_id)
            if v: vendor_name = v.name
    if not vendor_name:
        vendor_name = form.get("vendor_name") or ""

    quote_date_s = form.get("quote_date", "")
    event_date_s = form.get("event_date", "")
    valid_until_s = form.get("valid_until", "")

    with Session(engine, expire_on_commit=False) as s:
        q = Quote(
            quote_number=form.get("quote_number"),
            quote_date=datetime.strptime(quote_date_s, "%Y-%m-%d").date() if quote_date_s else date.today(),
            event_date=datetime.strptime(event_date_s, "%Y-%m-%d").date() if event_date_s else None,
            quote_type=form.get("quote_type", "납품"),
            rental_days=int(form.get("rental_days", 1) or 1),
            vendor_id=v_id, vendor_name=vendor_name,
            project_name=form.get("project_name", ""),
            subtotal=subtotal, discount=discount, after_discount=after_discount,
            vat=vat, total=total, vat_mode=vat_mode,
            valid_until=datetime.strptime(valid_until_s, "%Y-%m-%d").date() if valid_until_s else None,
            status="견적",
            created_by=user.id if user else None,
        )
        s.add(q); s.commit(); s.refresh(q)
        for idx, it in enumerate(items, 1):
            s.add(QuoteItem(
                quote_id=q.id, seq=idx,
                name=it.get("name", ""), spec=it.get("spec", ""),
                quantity=float(it.get("quantity", 1) or 1),
                unit=it.get("unit", "EA"),
                unit_price=int(it.get("unit_price", 0) or 0),
                amount=int(it.get("amount", 0) or 0),
            ))
        s.commit()
        new_id = q.id
    return RedirectResponse(f"/quotes/{new_id}", status_code=303)


CATEGORY_OPTIONS = ["음향", "조명", "영상", "납품", "무대", "특수효과"]
CATEGORY_ICONS = {
    "음향": "🎵", "조명": "💡", "영상": "🎬",
    "납품": "📦", "무대": "🎭", "특수효과": "✨",
}


@router.get("/project/new", response_class=HTMLResponse)
def mobile_project_new(request: Request):
    """모바일 프로젝트 등록 폼"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        # 다음 프로젝트 코드 자동 생성
        year = date.today().year
        count = len(s.exec(select(Project)).all()) + 1
        suggested_code = f"P-{year}-{count:03d}"
    return templates.TemplateResponse(request, "mobile/project_new.html", {
        "user": user,
        "vendors": [{"id": v.id, "name": v.name} for v in vendors],
        "suggested_code": suggested_code,
        "today": date.today(),
        "is_edit": False, "p": None,
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
    })


def _m_parse_date(s):
    if not s or not str(s).strip():
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _m_safe_int(value, default=0):
    if value is None:
        return default
    try:
        return int(str(value).replace(",", "").replace(" ", "").strip() or default)
    except (ValueError, TypeError):
        return default


def _m_unique_code(s, code, exclude_id=None):
    """모바일에서도 코드 충돌 자동 회피"""
    base = (code or "").strip() or "P-AUTO"
    candidate = base
    suffix_idx = 0
    while True:
        existing = s.exec(select(Project).where(Project.code == candidate)).first()
        if not existing or (exclude_id is not None and existing.id == exclude_id):
            return candidate
        suffix_idx += 1
        candidate = f"{base}-{chr(64 + suffix_idx) if suffix_idx <= 26 else suffix_idx}"


@router.post("/project/new")
async def mobile_project_create(request: Request):
    user = _user(request)
    form = await request.form()

    amount_int = _m_safe_int(form.get("amount_input", "0"))
    vat_mode = form.get("vat_mode", "supply")
    if vat_mode not in ("supply", "total", "cash"):
        vat_mode = "supply"
    if vat_mode == "total":
        total = amount_int
        supply = round(total / 1.1)
        vat = total - supply
    elif vat_mode == "cash":
        supply = amount_int; vat = 0; total = amount_int
    else:
        supply = amount_int; vat = round(supply * 0.1); total = supply + vat

    cats = form.getlist("categories")
    cats_str = ",".join([c.strip() for c in cats if c.strip()])

    vendor_id = form.get("vendor_id", "")
    try:
        v_id = int(vendor_id) if vendor_id and vendor_id.strip() else None
    except (ValueError, TypeError):
        v_id = None

    with Session(engine, expire_on_commit=False) as s:
        final_code = _m_unique_code(s, form.get("code", ""))
        p = Project(
            code=final_code,
            name=(form.get("name", "") or "").strip(),
            vendor_id=v_id,
            event_date=_m_parse_date(form.get("event_date", "")),
            location=(form.get("location", "") or "").strip(),
            revenue=total, supply_amount=supply, vat_amount=vat, vat_mode=vat_mode,
            memo=form.get("memo", "") or "", status="진행중",
            cash_no_invoice=(vat_mode == "cash"),
            categories=cats_str,
        )
        try:
            s.add(p); s.commit(); s.refresh(p)
            new_id = p.id
        except Exception as ex:
            s.rollback()
            raise HTTPException(400, f"프로젝트 저장 실패: {type(ex).__name__} — {str(ex)[:200]}")
    return RedirectResponse(f"/m/project/{new_id}", status_code=303)


@router.get("/project/{pid}/edit", response_class=HTMLResponse)
def mobile_project_edit(request: Request, pid: int):
    """모바일 프로젝트 수정 폼"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404)
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        pd = {
            "id": p.id, "code": p.code, "name": p.name,
            "vendor_id": p.vendor_id, "event_date": p.event_date,
            "location": p.location, "revenue": p.revenue,
            "supply_amount": p.supply_amount or p.revenue,
            "vat_mode": p.vat_mode or "supply",
            "memo": p.memo or "",
            "status": p.status,
            "categories": [c.strip() for c in (p.categories or "").split(",") if c.strip()],
        }
    return templates.TemplateResponse(request, "mobile/project_new.html", {
        "user": user,
        "vendors": [{"id": v.id, "name": v.name} for v in vendors],
        "is_edit": True, "p": pd,
        "today": date.today(),
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
    })


@router.post("/project/{pid}/edit")
async def mobile_project_update(request: Request, pid: int):
    _user(request)
    form = await request.form()

    amount_int = _m_safe_int(form.get("amount_input", "0"))
    vat_mode = form.get("vat_mode", "supply")
    if vat_mode not in ("supply", "total", "cash"):
        vat_mode = "supply"
    if vat_mode == "total":
        total = amount_int; supply = round(total / 1.1); vat = total - supply
    elif vat_mode == "cash":
        supply = amount_int; vat = 0; total = amount_int
    else:
        supply = amount_int; vat = round(supply * 0.1); total = supply + vat

    cats = form.getlist("categories")
    cats_str = ",".join([c.strip() for c in cats if c.strip()])

    vendor_id = form.get("vendor_id", "")
    try:
        v_id = int(vendor_id) if vendor_id and vendor_id.strip() else None
    except (ValueError, TypeError):
        v_id = None

    status = form.get("status", "진행중")
    if status not in ("준비중","진행중","완료","취소"):
        status = "진행중"

    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
        # 코드 충돌 자동 회피
        new_code = (form.get("code", "") or "").strip()
        if new_code and new_code != p.code:
            p.code = _m_unique_code(s, new_code, exclude_id=pid)
        p.name = (form.get("name", "") or "").strip()
        p.vendor_id = v_id
        p.event_date = _m_parse_date(form.get("event_date", ""))
        p.location = (form.get("location", "") or "").strip()
        p.revenue = total; p.supply_amount = supply; p.vat_amount = vat; p.vat_mode = vat_mode
        p.memo = form.get("memo", "") or ""
        p.status = status
        p.cash_no_invoice = (vat_mode == "cash")
        p.categories = cats_str
        try:
            s.add(p); s.commit()
        except Exception as ex:
            s.rollback()
            raise HTTPException(400, f"프로젝트 저장 실패: {type(ex).__name__} — {str(ex)[:200]}")
    return RedirectResponse(f"/m/project/{pid}", status_code=303)


@router.get("/project/{pid}", response_class=HTMLResponse)
def mobile_project_detail(request: Request, pid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404)
        expenses = s.exec(select(Expense).where(Expense.project_id == pid)).all()
        attendances = s.exec(select(Attendance).where(Attendance.project_id == pid)).all()
        total_expense = sum(e.amount for e in expenses)
        total_wage = sum(a.total_wage for a in attendances)
    return templates.TemplateResponse(request, "mobile/project_detail.html", {
        "user": user, "p": p, "expenses": expenses, "attendances": attendances,
        "total_expense": total_expense, "total_wage": total_wage,
    })


@router.post("/project/{pid}/delete")
def mobile_project_delete(request: Request, pid: int):
    """모바일 프로젝트 삭제 — 관련 데이터까지 안전하게 정리.

    PC 라우트(`/projects/{pid}/delete`)와 동일한 로직:
    - 비용/알바근태: 함께 삭제 (NOT NULL FK)
    - 정직원 출퇴근/견적서/대여이력: project_id만 None으로 (기록 보존)
    """
    _user(request)
    from database import WorkerCheckin, Quote
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            return RedirectResponse("/m/projects?deleted=notfound", status_code=303)

        # 비용 삭제
        for e in s.exec(select(Expense).where(Expense.project_id == pid)).all():
            s.delete(e)
        # 알바 근태 삭제
        for a in s.exec(select(Attendance).where(Attendance.project_id == pid)).all():
            s.delete(a)
        # 정직원 출퇴근은 보존 (project_id 끊기)
        for ci in s.exec(select(WorkerCheckin).where(WorkerCheckin.project_id == pid)).all():
            ci.project_id = None
            s.add(ci)
        # 견적서 연결 해제
        for q in s.exec(select(Quote).where(Quote.project_id == pid)).all():
            q.project_id = None
            s.add(q)
        # 대여이력 — 테이블이 있는 경우만
        try:
            from database import RentalLog
            for rl in s.exec(select(RentalLog).where(RentalLog.project_id == pid)).all():
                rl.project_id = None
                s.add(rl)
        except Exception:
            pass

        s.delete(p)
        s.commit()
    return RedirectResponse("/m/projects?deleted=1", status_code=303)


# ============================================================
# 비용 수정/삭제 (모바일)
# ============================================================
@router.get("/expense/{eid}/edit", response_class=HTMLResponse)
def mobile_expense_edit(request: Request, eid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if not e:
            raise HTTPException(404)
        projects = s.exec(
            select(Project).order_by(Project.event_date.desc())
        ).all()
        vendors = s.exec(select(Vendor)).all()
        ed = {
            "id": e.id, "project_id": e.project_id,
            "category": e.category, "amount": e.amount,
            "description": e.description, "vendor_name": e.vendor_name,
            "expense_date": e.expense_date,
            "receipt_image": e.receipt_image,
            "has_evidence": e.has_evidence,
            "tax_excluded_note": e.tax_excluded_note,
        }
    return templates.TemplateResponse(request, "mobile/expense_edit.html", {
        "user": user, "e": ed, "projects": projects, "vendors": vendors,
    })


@router.post("/expense/{eid}/edit")
async def mobile_expense_update(request: Request, eid: int):
    _user(request)
    form = await request.form()
    amount = int(str(form.get("amount", "0")).replace(',', '').strip() or 0)
    project_id = int(form.get("project_id"))
    has_evidence = form.get("has_evidence", "yes")
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if not e:
            raise HTTPException(404)
        e.project_id = project_id
        e.category = form.get("category", "기타")
        e.amount = amount
        e.description = form.get("description", "")
        e.has_evidence = (has_evidence != "no")
        e.tax_excluded_note = form.get("tax_excluded_note", "").strip() if has_evidence == "no" else ""
        s.add(e); s.commit()
    return RedirectResponse(f"/m/project/{project_id}", status_code=303)


@router.post("/expense/{eid}/delete")
def mobile_expense_delete(request: Request, eid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(Expense, eid)
        if e:
            project_id = e.project_id
            s.delete(e)
            s.commit()
            return RedirectResponse(f"/m/project/{project_id}", status_code=303)
    return RedirectResponse("/m/projects", status_code=303)


# ============================================================
# 오프라인 페이지
# ============================================================
@router.get("/offline", response_class=HTMLResponse)
def mobile_offline(request: Request):
    return templates.TemplateResponse(request, "mobile/offline.html", {})
