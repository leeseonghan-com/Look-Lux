"""프로젝트 관리"""
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from database import engine, Project, Vendor, Expense, Attendance, User
from template_utils import templates

router = APIRouter()


VAT_RATE = 0.1  # 한국 부가세 10% 고정

# 프로젝트 카테고리 옵션
CATEGORY_OPTIONS = ["음향", "조명", "영상", "납품", "무대", "특수효과"]
CATEGORY_ICONS = {
    "음향": "🎵", "조명": "💡", "영상": "🎬",
    "납품": "📦", "무대": "🎭", "특수효과": "✨",
}


def auto_status(project) -> tuple[str, str]:
    """행사일 기준 자동 상태 계산 (DB 수정 안 함, 표시용)
    반환: (진행상태, 정산상태)

    규칙:
    - status가 '취소'인 경우 → 그대로 유지
    - 행사일 없음 → 기존 status 그대로, 정산상태도 그대로
    - 행사일이 오늘 이후 → '진행중' (단 사용자가 '준비중'으로 명시 설정한 경우 유지)
    - 행사일이 오늘 이전 → '완료' + 정산상태는 무조건 '미수금'으로 자동 전환
      (단 paid_date가 있거나 settlement_status가 '입금완료'면 입금완료 유지)
    """
    if project.status == "취소":
        return ("취소", project.settlement_status or "미수금")
    if not project.event_date:
        return (project.status or "준비중", project.settlement_status or "미수금")

    today = date.today()
    is_past = project.event_date < today
    is_today = project.event_date == today

    # 진행상태
    if is_past:
        new_status = "완료"
    elif is_today:
        # 행사 당일은 무조건 진행중
        new_status = "진행중"
    else:
        # 행사일이 미래: 기본 진행중 (단 '준비중'을 명시 설정한 경우 유지)
        new_status = "진행중" if project.status != "준비중" else "준비중"

    # 정산상태: 행사일 지나면 미수금이 기본, 입금된 건 유지
    if is_past:
        if project.paid_date or project.settlement_status == "입금완료":
            new_settle = "입금완료"
        else:
            new_settle = "미수금"
    else:
        new_settle = project.settlement_status or "미수금"

    return (new_status, new_settle)


def unified_status(project) -> dict:
    """진행상태 + 정산상태를 하나의 통합 상태로 변환 (사용자 친화 표시용).

    반환: {"label": str, "color": str, "bg": str, "key": str}
    - 준비중: 행사 전, 사용자가 명시 설정
    - 진행중: 행사 임박/당일
    - 미수금: 행사 종료 + 미입금 ⚠️
    - 입금완료: 행사 종료 + 입금 ✓
    - 취소: 취소됨
    """
    st, settle = auto_status(project)
    if st == "취소":
        return {"label": "취소", "color": "#DC2626", "bg": "#FEE2E2", "key": "취소"}
    if st == "준비중":
        return {"label": "준비중", "color": "#6B7280", "bg": "#F3F4F6", "key": "준비중"}
    if st == "진행중":
        return {"label": "진행중", "color": "#2563EB", "bg": "#DBEAFE", "key": "진행중"}
    # 완료된 경우 → 정산상태로 분기
    if settle == "입금완료":
        return {"label": "✓ 입금완료", "color": "#059669", "bg": "#D1FAE5", "key": "입금완료"}
    return {"label": "⚠ 미수금", "color": "#C2410C", "bg": "#FED7AA", "key": "미수금"}


def _to_int(value, default: int = 0) -> int:
    """문자열 금액(콤마/공백 포함 가능)을 안전하게 int로 변환"""
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
        return s.get(User, uid)


def _user_with_perm(request: Request, key: str = "projects"):
    """권한 체크 포함 사용자 조회"""
    user = _user(request)
    from permissions import has_permission
    if not has_permission(user, key):
        raise HTTPException(403, "프로젝트 접근 권한이 없습니다.")
    return user


def _require_edit_perm(request: Request):
    """프로젝트 수정/삭제 권한 체크"""
    user = _user(request)
    from permissions import has_permission
    if not has_permission(user, "projects_edit"):
        raise HTTPException(403, "프로젝트 수정 권한이 없습니다.")
    return user


def _calc_vat(amount: int, mode: str) -> tuple[int, int, int]:
    """입력 금액과 모드(supply/total/cash)로부터 (공급가, 부가세, 총액) 계산

    - mode='supply': amount가 공급가액 → 부가세는 10% 추가
    - mode='total':  amount가 총액 → 공급가/부가세 분리 (총액 ÷ 1.1)
    - mode='cash':   현금거래(무증빙) → 부가세 없음, 입력값이 그대로 매출
    """
    if amount <= 0:
        return 0, 0, 0
    if mode in ("cash", "exempt"):  # exempt는 옛 호환
        # 현금거래(무증빙): 세금계산서 없는 현금 거래
        return int(amount), 0, int(amount)
    if mode == "total":
        supply = round(amount / (1 + VAT_RATE))
        vat = amount - supply
        return int(supply), int(vat), int(amount)
    else:  # supply
        supply = int(amount)
        vat = round(supply * VAT_RATE)
        total = supply + vat
        return supply, int(vat), int(total)


@router.get("/projects", response_class=HTMLResponse)
def project_list(
    request: Request,
    cats: str = "",
    q: str = "",
    sort: str = "created_desc",   # 정렬 기준
    month: str = "",              # YYYY-MM 필터 (행사일 기준)
    year: str = "",               # YYYY 필터
):
    """프로젝트 목록.
    cats: 쉼표로 구분된 카테고리 필터
    q: 키워드 검색
    sort: created_desc(최신 등록), event_desc(행사일 최신), event_asc(행사일 오래된), code_asc(코드), name_asc(이름)
    month: YYYY-MM 형식 — 해당 월의 행사만
    year: YYYY 형식 — 해당 년도의 행사만
    """
    user = _user_with_perm(request, "projects")
    selected_cats = [c.strip() for c in cats.split(",") if c.strip()] if cats else []
    search_q = (q or "").strip().lower()

    with Session(engine, expire_on_commit=False) as s:
        # 정렬 기준별 order_by
        stmt = select(Project)
        sort = (sort or "created_desc").strip()
        if sort == "event_desc":
            stmt = stmt.order_by(Project.event_date.desc().nullslast() if hasattr(Project.event_date.desc(), 'nullslast') else Project.event_date.desc(), Project.created_at.desc())
        elif sort == "event_asc":
            stmt = stmt.order_by(Project.event_date.asc().nullslast() if hasattr(Project.event_date.asc(), 'nullslast') else Project.event_date.asc(), Project.created_at.desc())
        elif sort == "code_asc":
            stmt = stmt.order_by(Project.code.asc())
        elif sort == "code_desc":
            stmt = stmt.order_by(Project.code.desc())
        elif sort == "name_asc":
            stmt = stmt.order_by(Project.name.asc())
        else:  # created_desc (기본)
            sort = "created_desc"
            stmt = stmt.order_by(Project.created_at.desc())
        all_projects = s.exec(stmt).all()

        # 월/년 필터 (행사일 기준)
        if month:  # "YYYY-MM"
            try:
                y, m = month.split("-")
                y_i, m_i = int(y), int(m)
                all_projects = [p for p in all_projects if p.event_date and p.event_date.year == y_i and p.event_date.month == m_i]
            except (ValueError, TypeError):
                pass
        elif year:  # "YYYY"
            try:
                y_i = int(year)
                all_projects = [p for p in all_projects if p.event_date and p.event_date.year == y_i]
            except (ValueError, TypeError):
                pass
        # 거래처 사전 (검색용)
        vendors_map = {v.id: v.name for v in s.exec(select(Vendor)).all()}
        # 키워드 필터
        if search_q:
            filtered = []
            for p in all_projects:
                vn = vendors_map.get(p.vendor_id, "") if p.vendor_id else ""
                searchable = " ".join([
                    p.name or "", p.code or "", vn,
                    p.location or "", p.memo or "",
                    p.special_notes or "",
                ]).lower()
                if search_q in searchable:
                    filtered.append(p)
            all_projects = filtered
        rows = []
        # 카테고리별 카운터 (필터 바에 표시)
        cat_counts = {c: 0 for c in CATEGORY_OPTIONS}
        cat_counts["미분류"] = 0

        for p in all_projects:
            project_cats = [c for c in (p.categories or "").split(",") if c.strip()]
            # 카운터
            if project_cats:
                for c in project_cats:
                    if c in cat_counts:
                        cat_counts[c] += 1
            else:
                cat_counts["미분류"] += 1
            # 필터 (OR: 선택한 카테고리 중 하나라도 포함되면)
            if selected_cats:
                if not any(c in project_cats for c in selected_cats):
                    continue

            v = s.get(Vendor, p.vendor_id) if p.vendor_id else None
            exp = sum(e.amount for e in s.exec(select(Expense).where(Expense.project_id == p.id)).all())
            wage = sum(a.total_wage for a in s.exec(select(Attendance).where(Attendance.project_id == p.id)).all())
            # 자동 계산된 상태 (DB 미수정, 표시용)
            auto_st, auto_settle = auto_status(p)
            unified = unified_status(p)
            rows.append({
                "id": p.id, "code": p.code, "name": p.name,
                "vendor_name": v.name if v else "",
                "event_date": p.event_date, "revenue": p.revenue,
                "supply_amount": p.supply_amount or p.revenue,
                "expense": exp, "wage": wage,
                "profit": (p.supply_amount or p.revenue) - exp - wage,
                "status": auto_st,
                "settlement_status": auto_settle,
                "unified": unified,  # 통합 상태 (label/color/bg/key)
                "stored_status": p.status,  # DB 원본 값
                "stored_settle": p.settlement_status,
                "is_auto_changed": (auto_st != p.status) or (auto_settle != p.settlement_status),
                "cash_no_invoice": bool(p.cash_no_invoice),
                "vat_mode": p.vat_mode or "supply",
                "categories": project_cats,
            })

        # 필터링된 결과 집계
        filtered_count = len(rows)
        filtered_revenue = sum(r["revenue"] for r in rows)
        filtered_supply = sum(r["supply_amount"] for r in rows)
        filtered_profit = sum(r["profit"] for r in rows)

    return templates.TemplateResponse(request, "projects.html", {
        "user": user, "rows": rows,
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
        "selected_cats": selected_cats,
        "cat_counts": cat_counts,
        "filtered_count": filtered_count,
        "filtered_revenue": filtered_revenue,
        "filtered_supply": filtered_supply,
        "filtered_profit": filtered_profit,
        "total_count": len(all_projects),
        "search_q": search_q,
    })


@router.get("/projects/new", response_class=HTMLResponse)
def project_new(request: Request):
    user = _user_with_perm(request, "projects")
    with Session(engine, expire_on_commit=False) as s:
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        # 자동 코드 생성
        year = date.today().year
        count = len(s.exec(select(Project)).all()) + 1
        code = f"P-{year}-{count:03d}"
    return templates.TemplateResponse(request, "project_new.html", {"user": user, "vendors": vendors,
        "today": date.today(), "default_code": code,
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
    })


def _parse_date_safe(s: str):
    """빈 문자열/잘못된 포맷 모두 None으로 안전하게 처리"""
    if not s or not str(s).strip():
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _unique_project_code(s: Session, code: str, exclude_id: int = None) -> str:
    """프로젝트 코드 충돌 시 자동으로 -A, -B ... 접미사 부여하여 충돌 회피"""
    base = (code or "").strip() or "P-AUTO"
    # 후보 0번은 원본 그대로
    candidate = base
    suffix_idx = 0
    while True:
        q = select(Project).where(Project.code == candidate)
        existing = s.exec(q).first()
        # 충돌 없음 또는 자기 자신만 충돌 → OK
        if not existing or (exclude_id is not None and existing.id == exclude_id):
            return candidate
        suffix_idx += 1
        candidate = f"{base}-{chr(64 + suffix_idx) if suffix_idx <= 26 else suffix_idx}"


@router.post("/projects/new")
def project_create(
    request: Request,
    code: str = Form(...), name: str = Form(...),
    vendor_id: Optional[str] = Form(None), event_date: str = Form(""),
    location: str = Form(""),
    amount_input: str = Form("0"),  # 콤마 포함 가능 → 서버에서 파싱
    vat_mode: str = Form("supply"),
    status: str = Form("준비중"),
    payment_method: str = Form(""),
    cash_no_invoice: str = Form(""),
    tax_excluded_note: str = Form(""),
    special_notes: str = Form(""),
    memo: str = Form(""),
    categories_csv: str = Form(""),  # 쉼표 구분된 카테고리 (JS가 hidden input으로 채움)
):
    _user(request)
    amount_int = _to_int(amount_input, 0)
    supply, vat, total = _calc_vat(amount_int, vat_mode)
    try:
        v_id = int(vendor_id) if vendor_id and vendor_id.strip() else None
    except (ValueError, TypeError):
        v_id = None
    is_cash_no_invoice = (cash_no_invoice == "yes") or (vat_mode == "cash")
    cats = ",".join([c.strip() for c in (categories_csv or "").split(",") if c.strip()])
    with Session(engine, expire_on_commit=False) as s:
        # 코드 충돌 자동 회피
        final_code = _unique_project_code(s, code)
        p = Project(
            code=final_code, name=name.strip(), vendor_id=v_id,
            event_date=_parse_date_safe(event_date),
            location=location.strip(),
            revenue=total,
            supply_amount=supply,
            vat_amount=vat,
            vat_mode=vat_mode if vat_mode in ("supply", "total", "cash") else "supply",
            status=status if status in ("준비중","진행중","완료","취소") else "준비중",
            payment_method=payment_method,
            cash_no_invoice=is_cash_no_invoice,
            tax_excluded_note=(tax_excluded_note or "").strip() if is_cash_no_invoice else "",
            special_notes=special_notes,
            memo=memo,
            categories=cats,
        )
        try:
            s.add(p)
            s.commit()
        except Exception as ex:
            s.rollback()
            raise HTTPException(400, f"프로젝트 저장 실패: {type(ex).__name__} — {str(ex)[:200]}")
    return RedirectResponse("/projects?created=1", status_code=303)


# ============================================================
# 자동 상태 일괄 동기화 (DB 실제 수정)
# ============================================================
@router.post("/projects/auto-sync")
def projects_auto_sync(request: Request):
    """모든 프로젝트의 진행/정산 상태를 행사일 기준으로 자동 갱신.
    auto_status() 계산 결과를 DB에 실제로 반영."""
    _user(request)
    updated = 0
    with Session(engine, expire_on_commit=False) as s:
        for p in s.exec(select(Project)).all():
            auto_st, auto_settle = auto_status(p)
            changed = False
            if p.status != auto_st:
                p.status = auto_st
                changed = True
            if p.settlement_status != auto_settle:
                p.settlement_status = auto_settle
                changed = True
            if changed:
                s.add(p)
                updated += 1
        s.commit()
    return RedirectResponse(f"/projects?auto_synced={updated}", status_code=303)


# ============================================================
# 프로젝트 일괄 수정 (정산상태 / 진행상태)
# ============================================================
@router.post("/projects/bulk-update")
def projects_bulk_update(
    request: Request,
    project_ids: str = Form(...),  # "1,2,3"
    field: str = Form(...),
    value: str = Form(""),
):
    """선택된 프로젝트들의 정산상태/진행상태 일괄 변경"""
    _user(request)
    try:
        ids = [int(x) for x in project_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "잘못된 ID 형식")
    if field not in ("settlement_status", "status"):
        raise HTTPException(400, f"허용되지 않은 필드: {field}")

    updated = 0
    today = date.today()
    with Session(engine, expire_on_commit=False) as s:
        for pid in ids:
            p = s.get(Project, pid)
            if not p:
                continue
            setattr(p, field, value)
            # 입금완료로 바뀌면 paid_date도 자동 기록 (이미 있으면 유지)
            if field == "settlement_status" and value == "입금완료" and not p.paid_date:
                p.paid_date = today
            s.add(p)
            updated += 1
        s.commit()
    return RedirectResponse(f"/projects?bulk_updated={updated}", status_code=303)


# ============================================================
# 거래처별 집계 페이지
# ============================================================
@router.get("/projects/by-vendor", response_class=HTMLResponse)
def projects_by_vendor(request: Request):
    """거래처별 프로젝트 건수·매출 집계"""
    user = _user_with_perm(request, "projects")
    with Session(engine, expire_on_commit=False) as s:
        projects = s.exec(select(Project)).all()
        vendors = {v.id: v for v in s.exec(select(Vendor)).all()}

        # 거래처별 그룹화
        groups = {}  # vendor_id (또는 0=미지정) -> {name, count, supply, revenue, profit, unpaid_count, unpaid_amount}
        for p in projects:
            vid = p.vendor_id or 0
            if vid not in groups:
                v_name = vendors[vid].name if vid in vendors else "거래처 미지정"
                groups[vid] = {
                    "vendor_id": vid, "vendor_name": v_name,
                    "biz_number": vendors[vid].biz_number if vid in vendors else "",
                    "count": 0, "supply": 0, "revenue": 0,
                    "expense": 0, "wage": 0, "profit": 0,
                    "unpaid_count": 0, "unpaid_amount": 0,
                    "categories_set": set(),
                }
            g = groups[vid]
            g["count"] += 1
            g["supply"] += (p.supply_amount or p.revenue)
            g["revenue"] += p.revenue
            exp = sum(e.amount for e in s.exec(select(Expense).where(Expense.project_id == p.id)).all())
            wage = sum(a.total_wage for a in s.exec(select(Attendance).where(Attendance.project_id == p.id)).all())
            g["expense"] += exp
            g["wage"] += wage
            g["profit"] += (p.supply_amount or p.revenue) - exp - wage
            # 자동 전환 상태 기준으로 미수금 집계 (행사일 지났는데 DB는 '준비중'인 경우 등 포함)
            _, auto_settle = auto_status(p)
            if auto_settle == "미수금":
                g["unpaid_count"] += 1
                g["unpaid_amount"] += p.revenue
            for c in (p.categories or "").split(","):
                c = c.strip()
                if c:
                    g["categories_set"].add(c)

        # set → list 변환, 매출 큰 순 정렬
        rows = []
        for g in groups.values():
            g["categories"] = sorted(g["categories_set"])
            del g["categories_set"]
            rows.append(g)
        rows.sort(key=lambda r: r["revenue"], reverse=True)

        total_count = sum(r["count"] for r in rows)
        total_supply = sum(r["supply"] for r in rows)
        total_revenue = sum(r["revenue"] for r in rows)
        total_profit = sum(r["profit"] for r in rows)

    return templates.TemplateResponse(request, "projects_by_vendor.html", {
        "user": user, "rows": rows,
        "total_count": total_count, "total_supply": total_supply,
        "total_revenue": total_revenue, "total_profit": total_profit,
        "category_icons": CATEGORY_ICONS,
    })


@router.get("/projects/by-vendor/{vid}", response_class=HTMLResponse)
def projects_by_vendor_detail(request: Request, vid: int):
    """특정 거래처의 프로젝트 목록"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        if vid == 0:
            vendor = None
            projects = s.exec(select(Project).where(Project.vendor_id == None).order_by(Project.event_date.desc())).all()
        else:
            vendor = s.get(Vendor, vid)
            if not vendor:
                raise HTTPException(404)
            projects = s.exec(select(Project).where(Project.vendor_id == vid).order_by(Project.event_date.desc())).all()

        rows = []
        for p in projects:
            exp = sum(e.amount for e in s.exec(select(Expense).where(Expense.project_id == p.id)).all())
            wage = sum(a.total_wage for a in s.exec(select(Attendance).where(Attendance.project_id == p.id)).all())
            auto_st, auto_settle = auto_status(p)
            rows.append({
                "id": p.id, "code": p.code, "name": p.name,
                "event_date": p.event_date, "revenue": p.revenue,
                "supply": p.supply_amount or p.revenue,
                "expense": exp, "wage": wage,
                "profit": (p.supply_amount or p.revenue) - exp - wage,
                "status": auto_st, "settlement_status": auto_settle,
                "categories": [c for c in (p.categories or "").split(",") if c.strip()],
            })

        total_count = len(rows)
        total_supply = sum(r["supply"] for r in rows)
        total_revenue = sum(r["revenue"] for r in rows)
        total_profit = sum(r["profit"] for r in rows)
        unpaid_count = sum(1 for r in rows if r["settlement_status"] == "미수금")
        unpaid_amount = sum(r["revenue"] for r in rows if r["settlement_status"] == "미수금")

    return templates.TemplateResponse(request, "projects_by_vendor_detail.html", {
        "user": user, "vendor": vendor, "vid": vid, "rows": rows,
        "total_count": total_count, "total_supply": total_supply,
        "total_revenue": total_revenue, "total_profit": total_profit,
        "unpaid_count": unpaid_count, "unpaid_amount": unpaid_amount,
        "category_icons": CATEGORY_ICONS,
    })


@router.get("/projects/{pid}", response_class=HTMLResponse)
def project_detail(request: Request, pid: int):
    user = _user_with_perm(request, "projects")
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404)
        v = s.get(Vendor, p.vendor_id) if p.vendor_id else None
        # 연결된 견적서 조회 (project_id로 역참조)
        from database import Quote
        linked_quote = s.exec(select(Quote).where(Quote.project_id == pid)).first()
        linked_quote_dict = {
            "id": linked_quote.id, "quote_number": linked_quote.quote_number,
            "total": linked_quote.total, "quote_date": linked_quote.quote_date,
        } if linked_quote else None
        expenses_raw = s.exec(
            select(Expense).where(Expense.project_id == pid).order_by(Expense.expense_date.desc(), Expense.id.desc())
        ).all()
        attendances = s.exec(
            select(Attendance).where(Attendance.project_id == pid).order_by(Attendance.work_date.desc(), Attendance.id.desc())
        ).all()
        # worker name 조인
        from database import Worker
        att_rows = []
        for a in attendances:
            w = s.get(Worker, a.worker_id)
            att_rows.append({
                "id": a.id, "date": a.work_date, "worker_name": w.name if w else "?",
                "days": a.days, "daily_wage": a.daily_wage,
                "bonus_amount": a.bonus_amount or 0,
                "bonus_memo": a.bonus_memo or "",
                "total_wage": a.total_wage,
                "pay_status": a.pay_status,
            })
        expenses = [{
            "id": e.id, "category": e.category, "description": e.description,
            "amount": e.amount, "receipt_image": e.receipt_image,
            "expense_date": e.expense_date,
            "vendor_name": e.vendor_name or "",
        } for e in expenses_raw]
        total_expense = sum(e["amount"] for e in expenses)
        total_wage = sum(a["total_wage"] for a in att_rows)
        profit = p.revenue - total_expense - total_wage
        # 행사일 기준 자동 상태 (표시용, DB 미수정)
        auto_st, auto_settle = auto_status(p)
        unified = unified_status(p)
        pd = {
            "id": p.id, "code": p.code, "name": p.name,
            "event_date": p.event_date, "location": p.location, "revenue": p.revenue,
            "supply_amount": p.supply_amount, "vat_amount": p.vat_amount,
            "vat_mode": p.vat_mode or "supply",
            # 화면에는 자동 계산 결과를 우선 표시
            "status": auto_st,
            "settlement_status": auto_settle,
            "unified": unified,
            # DB 원본 (수정 폼/대조용)
            "stored_status": p.status,
            "stored_settle": p.settlement_status,
            "is_auto_changed": (auto_st != p.status) or (auto_settle != p.settlement_status),
            "invoice_date": p.invoice_date, "settle_due_date": p.settle_due_date,
            "paid_date": p.paid_date, "payment_method": p.payment_method,
            "payment_memo": p.payment_memo,
            "cash_no_invoice": bool(p.cash_no_invoice),
            "tax_excluded_note": p.tax_excluded_note or "",
            "special_notes": p.special_notes, "memo": p.memo,
            "categories": [c for c in (p.categories or "").split(",") if c.strip()],
        }
        vd = {"id": v.id, "name": v.name} if v else None
    return templates.TemplateResponse(request, "project_detail.html", {
        "user": user, "p": pd, "vendor": vd,
        "expenses": expenses, "attendances": att_rows,
        "total_expense": total_expense, "total_wage": total_wage, "profit": profit,
        "linked_quote": linked_quote_dict,
    })


@router.post("/projects/{pid}/settle")
def mark_settled(request: Request, pid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if p:
            p.settlement_status = "입금완료" if p.settlement_status == "미수금" else "미수금"
            s.add(p)
            s.commit()
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@router.get("/projects/{pid}/edit", response_class=HTMLResponse)
def project_edit(request: Request, pid: int):
    user = _require_edit_perm(request)
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404)
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        pd = {"id": p.id, "code": p.code, "name": p.name, "vendor_id": p.vendor_id,
              "event_date": p.event_date, "location": p.location, "revenue": p.revenue,
              "supply_amount": p.supply_amount, "vat_amount": p.vat_amount,
              "vat_mode": p.vat_mode or "supply",
              "status": p.status, "settlement_status": p.settlement_status,
              "invoice_date": p.invoice_date, "settle_due_date": p.settle_due_date,
              "paid_date": p.paid_date, "payment_method": p.payment_method,
              "payment_memo": p.payment_memo,
              "cash_no_invoice": bool(p.cash_no_invoice),
              "tax_excluded_note": p.tax_excluded_note or "",
              "special_notes": p.special_notes, "memo": p.memo,
              "categories": [c for c in (p.categories or "").split(",") if c.strip()]}
        vendors_data = [{"id": v.id, "name": v.name} for v in vendors]
    return templates.TemplateResponse(request, "project_edit.html", {
        "user": user, "p": pd, "vendors": vendors_data,
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
    })


@router.post("/projects/{pid}/edit")
def project_update(
    request: Request, pid: int,
    code: str = Form(...), name: str = Form(...),
    vendor_id: Optional[str] = Form(None), event_date: str = Form(""),
    location: str = Form(""),
    amount_input: str = Form("0"),  # 콤마 포함 가능
    vat_mode: str = Form("supply"),
    status: str = Form("준비중"),
    settlement_status: str = Form("미수금"),
    invoice_date: str = Form(""),
    settle_due_date: str = Form(""),
    paid_date: str = Form(""),
    payment_method: str = Form(""),
    payment_memo: str = Form(""),
    cash_no_invoice: str = Form(""),
    tax_excluded_note: str = Form(""),
    special_notes: str = Form(""),
    memo: str = Form(""),
    categories_csv: str = Form(""),
):
    _user(request)
    amount_int = _to_int(amount_input, 0)
    supply, vat, total = _calc_vat(amount_int, vat_mode)
    try:
        v_id = int(vendor_id) if vendor_id and vendor_id.strip() else None
    except (ValueError, TypeError):
        v_id = None
    cats = ",".join([c.strip() for c in (categories_csv or "").split(",") if c.strip()])
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404, "해당 프로젝트를 찾을 수 없습니다.")
        # 코드가 다른 프로젝트와 충돌하면 자동 회피 (-A, -B …)
        new_code = (code or "").strip()
        if new_code and new_code != p.code:
            p.code = _unique_project_code(s, new_code, exclude_id=pid)
        elif not new_code:
            # 빈 코드는 기존 코드 유지
            pass
        p.categories = cats
        p.name = name.strip()
        p.vendor_id = v_id
        p.event_date = _parse_date_safe(event_date)
        p.location = location.strip()
        p.revenue = total
        p.supply_amount = supply
        p.vat_amount = vat
        p.vat_mode = vat_mode if vat_mode in ("supply", "total", "cash") else "supply"
        p.status = status if status in ("준비중","진행중","완료","취소") else p.status
        p.settlement_status = settlement_status if settlement_status in ("미수금","입금완료") else p.settlement_status
        p.invoice_date = _parse_date_safe(invoice_date)
        p.settle_due_date = _parse_date_safe(settle_due_date)
        p.paid_date = _parse_date_safe(paid_date)
        p.payment_method = payment_method
        p.payment_memo = payment_memo
        is_cash = (cash_no_invoice == "yes") or (vat_mode == "cash")
        p.cash_no_invoice = is_cash
        p.tax_excluded_note = (tax_excluded_note or "").strip() if is_cash else ""
        p.special_notes = special_notes
        p.memo = memo
        # 입금일이 있으면 자동으로 settlement_status를 입금완료로
        if p.paid_date and p.settlement_status != "입금완료":
            p.settlement_status = "입금완료"
        try:
            s.add(p)
            s.commit()
        except Exception as ex:
            s.rollback()
            raise HTTPException(400, f"프로젝트 저장 실패: {type(ex).__name__} — {str(ex)[:200]}")
    return RedirectResponse(f"/projects/{pid}?updated=1", status_code=303)


@router.post("/projects/{pid}/delete")
def project_delete(request: Request, pid: int):
    """프로젝트 삭제 — 관련 데이터까지 안전하게 정리

    - 비용(Expense), 알바근태(Attendance): NOT NULL 외래키 → 함께 삭제
    - 정직원 출퇴근(WorkerCheckin): Nullable → project_id = None 으로 분리 (기록은 보존)
    - 견적서(Quote): Nullable → project_id = None 으로 분리 (견적은 보존)
    - 장비 대여 이력(RentalLog): Nullable → project_id = None 으로 분리
    """
    _user(request)
    from database import Expense, Attendance, WorkerCheckin, Quote, RentalLog
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            return RedirectResponse("/projects?deleted=notfound", status_code=303)

        # 1) 비용 삭제 (NOT NULL FK)
        for e in s.exec(select(Expense).where(Expense.project_id == pid)).all():
            s.delete(e)

        # 2) 알바 근태 삭제 (NOT NULL FK)
        for a in s.exec(select(Attendance).where(Attendance.project_id == pid)).all():
            s.delete(a)

        # 3) 정직원 출퇴근은 기록 보존 — project_id만 끊음
        for ci in s.exec(select(WorkerCheckin).where(WorkerCheckin.project_id == pid)).all():
            ci.project_id = None
            s.add(ci)

        # 4) 견적서 연결 해제 — 견적은 남기고 link만 끊음
        for q in s.exec(select(Quote).where(Quote.project_id == pid)).all():
            q.project_id = None
            s.add(q)

        # 5) 장비 대여 이력 연결 해제
        try:
            for rl in s.exec(select(RentalLog).where(RentalLog.project_id == pid)).all():
                rl.project_id = None
                s.add(rl)
        except Exception:
            # RentalLog 테이블이 아직 없는 환경에서는 무시
            pass

        # 6) 프로젝트 삭제
        s.delete(p)
        s.commit()
    return RedirectResponse("/projects?deleted=1", status_code=303)
