"""견적서 발행 (납품용 / 렌탈용 구분) + 수주 시 프로젝트 자동 연동"""
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from database import engine, Quote, QuoteItem, Vendor, User, Project, get_company_settings
from template_utils import templates

router = APIRouter()


# ============================================================
# 견적서 → 프로젝트 변환 유틸
# ============================================================

# 품목명에서 카테고리 자동 추론 (키워드 매핑)
CATEGORY_KEYWORDS = {
    "음향": ["마이크", "스피커", "믹서", "앰프", "음향", "사운드", "콘솔", "이펙터",
            "모니터스피커", "라인어레이", "서브우퍼", "다이렉트박스", "DI", "EQ"],
    "조명": ["조명", "LED", "무빙", "파라이트", "라이트", "디머", "스포트라이트",
            "팔로우", "패널라이트", "프레넬", "이펙트라이트", "스트로브"],
    "영상": ["LED스크린", "프로젝터", "스위처", "캠코더", "카메라", "영상", "비디오",
            "모니터", "디스플레이", "월", "스트리밍", "송출", "녹화", "캡처"],
    "납품": ["납품", "판매", "구매", "공급"],
    "무대": ["무대", "스테이지", "백드롭", "트러스", "리깅", "프레임", "데크",
            "단상", "포디움", "장식", "셋업"],
    "특수효과": ["특수효과", "포그", "스모크", "헤이저", "버블", "스노우", "콘페티",
              "불꽃", "CO2", "레이저", "이펙트", "스파클러"],
}


def _infer_categories(items: list) -> list[str]:
    """견적서 품목 목록에서 카테고리를 자동 추론.
    품목명 + 사양에서 키워드를 찾아 매칭."""
    found = set()
    for it in items:
        # QuoteItem 객체 또는 dict 둘 다 지원
        name = (getattr(it, "name", None) or it.get("name", "") if isinstance(it, dict) else getattr(it, "name", "")) or ""
        spec = (getattr(it, "spec", None) or it.get("spec", "") if isinstance(it, dict) else getattr(it, "spec", "")) or ""
        text = (name + " " + spec).lower()
        for cat, keywords in CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text:
                    found.add(cat)
                    break
    return sorted(found)


def _items_to_text_table(items: list) -> str:
    """견적서 품목을 프로젝트 특이사항용 텍스트 표로 변환."""
    if not items:
        return ""
    lines = ["[견적 품목]"]
    lines.append(f"{'순번':<4} {'품목명':<24} {'규격':<14} {'수량':>6} {'단위':<4} {'단가':>12} {'금액':>14}")
    lines.append("─" * 80)
    for it in items:
        if isinstance(it, dict):
            seq = it.get("seq", "")
            name = it.get("name", "")
            spec = it.get("spec", "")
            qty = it.get("quantity", 0)
            unit = it.get("unit", "")
            up = it.get("unit_price", 0)
            amt = it.get("amount", 0)
        else:
            seq = it.seq; name = it.name; spec = it.spec or ""
            qty = it.quantity; unit = it.unit; up = it.unit_price; amt = it.amount
        # 한글 폭 차이는 무시하고 단순 좌측 정렬
        qty_s = f"{qty:g}" if isinstance(qty, float) else str(qty)
        lines.append(f"{seq:<4} {name[:24]:<24} {spec[:14]:<14} {qty_s:>6} {unit:<4} {up:>12,} {amt:>14,}")
    return "\n".join(lines)


def _generate_project_code() -> str:
    """프로젝트 코드 자동 생성 (P-YYYY-NNN)"""
    year = date.today().year
    with Session(engine, expire_on_commit=False) as s:
        count = len(s.exec(select(Project)).all()) + 1
    return f"P-{year}-{count:03d}"


def _user_names(session) -> dict:
    return {u.id: (u.name or u.username) for u in session.exec(select(User)).all()}


def _user(request: Request):
    """기본 사용자 — quotes 권한 체크 포함"""
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        user = s.get(User, uid)
    if user:
        from permissions import has_permission
        if not has_permission(user, "quotes"):
            raise HTTPException(403, "견적서 접근 권한이 없습니다.")
    return user


def _quote_to_dict(q):
    return {
        "id": q.id, "quote_number": q.quote_number, "quote_date": q.quote_date,
        "event_date": getattr(q, "event_date", None),
        "quote_type": q.quote_type, "rental_days": q.rental_days,
        "vendor_id": q.vendor_id, "vendor_name": q.vendor_name,
        "vendor_contact": q.vendor_contact, "vendor_phone": q.vendor_phone,
        "project_name": q.project_name,
        "subtotal": q.subtotal, "discount": q.discount, "after_discount": q.after_discount,
        "vat": q.vat, "total": q.total, "status": q.status,
        "vat_mode": getattr(q, "vat_mode", "supply"),
        "valid_until": q.valid_until, "payment_terms": q.payment_terms,
        "delivery_terms": q.delivery_terms, "notes": q.notes,
        "project_id": getattr(q, "project_id", None),
        "created_by": getattr(q, "created_by", None),
        "updated_by": getattr(q, "updated_by", None),
        "updated_at": getattr(q, "updated_at", None),
    }


def _safe_money(v, default=0):
    """문자열/숫자/콤마 포함 모두 안전하게 int로"""
    if v is None or v == "":
        return default
    if isinstance(v, (int, float)):
        return int(v)
    try:
        cleaned = str(v).replace(",", "").replace(" ", "").replace("원", "").strip()
        return int(float(cleaned)) if cleaned else default
    except (ValueError, TypeError):
        return default


def _calc_quote_amounts(items: list, discount: int, vat_mode: str) -> dict:
    """견적서 금액 계산 — 부가세 모드에 따라 분기.

    - supply: 입력 합계가 공급가 → VAT 10% 추가
    - total: 입력 합계가 총액 (VAT 포함) → 자동 분리
    - cash: 현금 무증빙 → VAT 없음
    """
    # JS가 보낼 수 있는 모든 형식(숫자, 콤마 문자열) 안전 처리
    subtotal = sum(_safe_money(it.get("amount", 0)) for it in items)
    after_discount = max(0, subtotal - discount)

    if vat_mode == "cash":
        vat = 0
        total = after_discount
    elif vat_mode == "total":
        # after_discount 가 VAT 포함 총액
        total = after_discount
        supply = round(total / 1.1)
        vat = total - supply
        after_discount = supply  # 공급가로 재조정
    else:  # supply (기본)
        vat = round(after_discount * 0.1)
        total = after_discount + vat

    return {
        "subtotal": subtotal,
        "discount": discount,
        "after_discount": after_discount,
        "vat": vat,
        "total": total,
    }


def _items_to_list(items):
    return [{
        "id": it.id, "seq": it.seq, "name": it.name, "spec": it.spec,
        "quantity": it.quantity, "unit": it.unit,
        "unit_price": it.unit_price,
        "cost_price": getattr(it, "cost_price", 0) or 0,
        "amount": it.amount, "memo": it.memo,
    } for it in items]


@router.get("/quotes", response_class=HTMLResponse)
def quote_list(request: Request, q: str = ""):
    user = _user(request)
    search_q = (q or "").strip().lower()
    with Session(engine, expire_on_commit=False) as s:
        quotes_raw = s.exec(select(Quote).order_by(Quote.quote_date.desc())).all()
        if search_q:
            quotes_raw = [qq for qq in quotes_raw if any(search_q in (str(x) or "").lower() for x in [
                qq.quote_number, qq.vendor_name, qq.project_name, qq.notes,
                qq.vendor_contact, qq.quote_type, qq.status,
            ])]
        _names = _user_names(s)
        quotes = [{
            "author_name": _names.get(qq.created_by, ""),
            "id": qq.id, "quote_number": qq.quote_number, "quote_date": qq.quote_date,
            "quote_type": qq.quote_type,
            "vendor_name": qq.vendor_name, "project_name": qq.project_name,
            "total": qq.total, "status": qq.status,
            "project_id": getattr(qq, "project_id", None),
        } for qq in quotes_raw]
        total_count = len(quotes)
        won = [qq for qq in quotes if qq["status"] == "수주"]
        won_amount = sum(qq["total"] for qq in won)
        win_rate = (len(won) / total_count * 100) if total_count else 0
    return templates.TemplateResponse(request, "quotes.html", {
        "user": user, "quotes": quotes,
        "total_count": total_count, "won_count": len(won),
        "won_amount": won_amount, "win_rate": win_rate,
        "search_q": search_q,
    })


@router.get("/quotes/new", response_class=HTMLResponse)
def quote_new(request: Request, type: str = "납품"):
    user = _user(request)
    cs = get_company_settings()
    with Session(engine, expire_on_commit=False) as s:
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        vendors_data = [{"id": v.id, "name": v.name} for v in vendors]
        # ⭐ 견적 미연결 프로젝트 리스트 (진행중/준비중만 노출 — 완료·취소는 제외)
        open_projects = s.exec(
            select(Project).where(Project.status.in_(["준비중", "진행중"]))
            .order_by(Project.created_at.desc())
        ).all()
        projects_data = [
            {"id": p.id, "code": p.code, "name": p.name,
             "event_date": p.event_date.isoformat() if p.event_date else "",
             "vendor_id": p.vendor_id or 0}
            for p in open_projects
        ]
        year = date.today().year
        count = len(s.exec(select(Quote)).all()) + 1
        prefix = "QR" if type == "렌탈" else "Q"
        default_no = f"{prefix}-{year}-{count:03d}"
        # 회사 설정의 기본 결제/납품 조건을 미리 채움
    return templates.TemplateResponse(request, "quote_new.html", {
        "user": user, "vendors": vendors_data,
        "projects": projects_data,
        "today": date.today(),
        "valid_until": date.today() + timedelta(days=cs.get("default_valid_days", 30)),
        "default_no": default_no, "quote_type": type,
        "quote": None,
        "default_payment_terms": cs.get("default_payment_terms", ""),
        "default_delivery_terms": cs.get("default_delivery_terms", ""),
        "default_notes": cs.get("quote_footer_notes", ""),
    })


@router.post("/quotes/new")
async def quote_create(request: Request):
    user = _user(request)
    form = await request.form()
    items_json = form.get("items_json", "[]")
    try:
        items = json.loads(items_json)
    except Exception:
        items = []

    # 부가세 모드 + 금액 계산
    vat_mode = form.get("vat_mode", "supply") or "supply"
    if vat_mode not in ("supply", "total", "cash"):
        vat_mode = "supply"
    discount = _safe_int_q(form.get("discount", "0"), 0)
    from permissions import has_permission as _hpq
    _can_rev = _hpq(_user(request), "view_revenue")
    if not _can_rev:
        # ★ 금액 권한 없는 직원: 화면에 가격이 없으므로 저장된 기존 가격을 그대로 사용
        with Session(engine, expire_on_commit=False) as _s0:
            _old_q = _s0.get(Quote, qid)
            _olds = _s0.exec(select(QuoteItem).where(QuoteItem.quote_id == qid).order_by(QuoteItem.seq)).all()
        _by_name = {}
        for _o in _olds:
            _by_name.setdefault((_o.name or "").strip(), []).append(_o)
        for _it in items:
            _cands = _by_name.get((_it.get("name") or "").strip()) or []
            _o = _cands.pop(0) if _cands else None
            _up = (_o.unit_price if _o else 0) or 0
            _it["unit_price"] = _up
            _it["cost_price"] = (getattr(_o, "cost_price", 0) if _o else 0) or 0
            try:
                _qq = int(float(str(_it.get("quantity", 1) or 1).replace(",", "")))
            except (ValueError, TypeError):
                _qq = 1
            _days = (_old_q.rental_days or 1) if (_old_q and _old_q.quote_type == "렌탈") else 1
            _it["amount"] = _qq * _up * _days
        discount = (_old_q.discount if _old_q else 0) or 0
        vat_mode = (_old_q.vat_mode if _old_q else vat_mode) or vat_mode
    amounts = _calc_quote_amounts(items, discount, vat_mode)

    # 날짜 안전 파싱
    quote_date_p = _safe_date(form.get("quote_date", ""), default=date.today())
    event_date_p = _safe_date(form.get("event_date", ""), default=None)
    valid_until_p = _safe_date(form.get("valid_until", ""), default=None)

    # vendor_id 안전 파싱
    v_id_raw = (form.get("vendor_id") or "").strip()
    try:
        v_id = int(v_id_raw) if v_id_raw else None
    except (ValueError, TypeError):
        v_id = None

    # ⭐ project_id (기존 프로젝트 연결) 안전 파싱
    p_id_raw = (form.get("project_id") or "").strip()
    try:
        p_id = int(p_id_raw) if p_id_raw else None
    except (ValueError, TypeError):
        p_id = None

    with Session(engine, expire_on_commit=False) as s:
        # ⭐ 기존 프로젝트 연결한 경우: 상태를 곧바로 '수주'로, 프로젝트 정보로 폼값 보완
        pre_status = "견적"
        if p_id:
            proj = s.get(Project, p_id)
            if proj:
                pre_status = "수주"
                # 프로젝트명 폼값이 비어있으면 프로젝트에서 채움
                if not (form.get("project_name") or "").strip():
                    form_project_name = proj.name
                else:
                    form_project_name = form.get("project_name")
                # 거래처 없으면 프로젝트에서 채움
                if not v_id and proj.vendor_id:
                    v_id = proj.vendor_id
            else:
                p_id = None
                form_project_name = form.get("project_name") or ""
        else:
            form_project_name = form.get("project_name") or ""

        q = Quote(
            quote_number=(form.get("quote_number") or "").strip() or f"Q-{date.today().year}-{int(date.today().timestamp()) % 10000:04d}",
            quote_date=quote_date_p,
            event_date=event_date_p,
            quote_type=(form.get("quote_type") or "납품").strip(),
            rental_days=_safe_int_q(form.get("rental_days", "1"), 1) or 1,
            vendor_id=v_id,
            vendor_name=(form.get("vendor_name") or "").strip(),
            vendor_contact=(form.get("vendor_contact") or "").strip(),
            vendor_phone=(form.get("vendor_phone") or "").strip(),
            project_name=(form_project_name or "").strip(),
            subtotal=amounts["subtotal"], discount=amounts["discount"],
            after_discount=amounts["after_discount"],
            vat=amounts["vat"], total=amounts["total"],
            vat_mode=vat_mode,
            valid_until=valid_until_p,
            payment_terms=form.get("payment_terms") or "",
            delivery_terms=form.get("delivery_terms") or "",
            notes=form.get("notes") or "",
            status=pre_status,
            project_id=p_id,  # ⭐ 기존 프로젝트 연결
            created_by=user.id if user else None,
        )
        s.add(q)
        s.commit()
        s.refresh(q)
        new_id = q.id
        for idx, it in enumerate(items, start=1):
            # 수량은 정수만 받음 (소수점 단위 제거)
            try:
                _qty = int(float(str(it.get("quantity", 1) or 1).replace(',', '')))
            except (ValueError, TypeError):
                _qty = 1
            if _qty < 1: _qty = 1
            _up = _safe_money(it.get("unit_price", 0))
            _cp = _safe_money(it.get("cost_price", 0))
            _amt = _safe_money(it.get("amount", 0)) or (_qty * _up)
            qi = QuoteItem(
                quote_id=new_id, seq=idx,
                name=(it.get("name") or "").strip(),
                spec=(it.get("spec") or "").strip(),
                quantity=_qty,
                unit=(it.get("unit") or "EA").strip(),
                unit_price=_up,
                cost_price=_cp,
                amount=_amt,
                memo=(it.get("memo") or "").strip(),
            )
            s.add(qi)
        s.commit()

        # ⭐ 기존 프로젝트에 연결된 경우 프로젝트 정보도 동기화
        if p_id:
            proj = s.get(Project, p_id)
            if proj:
                items_for_sync = s.exec(select(QuoteItem).where(QuoteItem.quote_id == new_id)).all()
                _sync_project_from_quote(proj, q, items_for_sync)
                s.add(proj)
                s.commit()
    return RedirectResponse(f"/quotes/{new_id}", status_code=303)


@router.get("/quotes/{qid}", response_class=HTMLResponse)
def quote_view(request: Request, qid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        q = s.get(Quote, qid)
        if not q:
            raise HTTPException(404)
        items_raw = s.exec(select(QuoteItem).where(QuoteItem.quote_id == qid).order_by(QuoteItem.seq)).all()
        q_dict = _quote_to_dict(q)
        _nm = _user_names(s)
        q_dict["author_name"] = _nm.get(q.created_by, "")
        q_dict["editor_name"] = _nm.get(getattr(q, "updated_by", None), "")
        items_list = _items_to_list(items_raw)

    # ── 납품 견적서 마진 계산 (템플릿에서 Jinja set-in-loop이 outer scope 갱신 못하므로 서버에서 미리) ──
    margin_info = None
    if q_dict.get("quote_type") != "렌탈":
        total_cost = 0
        total_sell = 0
        line_details = []
        for it in items_list:
            qty = int(it.get("quantity") or 0)
            cp = int(it.get("cost_price") or 0)
            amt = int(it.get("amount") or 0)
            line_cost = cp * qty
            line_margin = amt - line_cost
            line_pct = (line_margin / amt * 100) if amt else 0
            total_cost += line_cost
            total_sell += amt
            line_details.append({
                "seq": it.get("seq"),
                "name": it.get("name"),
                "line_cost": line_cost,
                "amount": amt,
                "line_margin": line_margin,
                "line_pct": line_pct,
            })
        if total_cost > 0:
            margin_amount = total_sell - total_cost
            margin_pct = (margin_amount / total_sell * 100) if total_sell else 0
            margin_info = {
                "has_data": True,
                "total_cost": total_cost,
                "total_sell": total_sell,
                "margin_amount": margin_amount,
                "margin_pct": margin_pct,
                "lines": line_details,
            }
        else:
            margin_info = {"has_data": False}
    return templates.TemplateResponse(request, "quote_view.html", {
        "user": user, "q": q_dict, "items": items_list,
        "margin_info": margin_info,
    })


@router.get("/quotes/{qid}/edit", response_class=HTMLResponse)
def quote_edit(request: Request, qid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        q = s.get(Quote, qid)
        if not q:
            raise HTTPException(404)
        items_raw = s.exec(select(QuoteItem).where(QuoteItem.quote_id == qid).order_by(QuoteItem.seq)).all()
        q_dict = _quote_to_dict(q)
        items_list = _items_to_list(items_raw)
        vendors = s.exec(select(Vendor).order_by(Vendor.name)).all()
        vendors_data = [{"id": v.id, "name": v.name} for v in vendors]
        # ⭐ 진행중 프로젝트 리스트
        open_projects = s.exec(
            select(Project).where(Project.status.in_(["준비중", "진행중"]))
            .order_by(Project.created_at.desc())
        ).all()
        projects_data = [
            {"id": p.id, "code": p.code, "name": p.name,
             "event_date": p.event_date.isoformat() if p.event_date else "",
             "vendor_id": p.vendor_id or 0}
            for p in open_projects
        ]
    return templates.TemplateResponse(request, "quote_new.html", {
        "user": user, "vendors": vendors_data,
        "projects": projects_data,
        "today": date.today(), "valid_until": q_dict["valid_until"],
        "default_no": q_dict["quote_number"], "quote_type": q_dict["quote_type"],
        "quote": q_dict, "existing_items": items_list,
        "default_payment_terms": q_dict["payment_terms"],
        "default_delivery_terms": q_dict["delivery_terms"],
        "default_notes": q_dict["notes"],
    })


def _safe_date(s_val, default=None):
    """다양한 날짜 형식 안전 파싱 — 실패하면 default 반환"""
    if not s_val or not str(s_val).strip():
        return default
    s_val = str(s_val).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s_val, fmt).date()
        except (ValueError, TypeError):
            continue
    return default


def _safe_int_q(v, default=0):
    try:
        return int(str(v).replace(',', '').replace(' ', '').strip() or default)
    except (ValueError, TypeError):
        return default


@router.post("/quotes/{qid}/edit")
async def quote_update(request: Request, qid: int):
    """견적서 수정 — 모든 입력 안전 처리. 잘못된 형식이 500을 일으키지 않게 한다."""
    _user(request)
    form = await request.form()
    items_json = form.get("items_json", "[]")
    try:
        items = json.loads(items_json)
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []

    vat_mode = form.get("vat_mode", "supply") or "supply"
    if vat_mode not in ("supply", "total", "cash"):
        vat_mode = "supply"
    discount = _safe_int_q(form.get("discount", "0"), 0)
    amounts = _calc_quote_amounts(items, discount, vat_mode)

    # 날짜 안전 파싱
    quote_date_p = _safe_date(form.get("quote_date", ""), default=date.today())
    event_date_p = _safe_date(form.get("event_date", ""), default=None)
    valid_until_p = _safe_date(form.get("valid_until", ""), default=None)

    with Session(engine, expire_on_commit=False) as s:
        q = s.get(Quote, qid)
        if not q:
            raise HTTPException(404, "해당 견적서를 찾을 수 없습니다.")
        # 견적번호 안전 처리 (빈 값 → 기존값 유지)
        new_qn = (form.get("quote_number") or "").strip()
        if new_qn:
            q.quote_number = new_qn
        q.quote_date = quote_date_p
        q.event_date = event_date_p
        q.quote_type = (form.get("quote_type") or "납품").strip()
        q.rental_days = _safe_int_q(form.get("rental_days", "1"), 1) or 1
        # vendor_id 안전 파싱
        v_id_raw = (form.get("vendor_id") or "").strip()
        try:
            q.vendor_id = int(v_id_raw) if v_id_raw else None
        except (ValueError, TypeError):
            q.vendor_id = None
        q.vendor_name = (form.get("vendor_name") or "").strip()
        q.vendor_contact = (form.get("vendor_contact") or "").strip()
        q.vendor_phone = (form.get("vendor_phone") or "").strip()
        # ⭐ vendor_id가 없거나 이름이 다르면, 이름으로 거래처 마스터 자동 매칭
        if q.vendor_name:
            if not q.vendor_id:
                matched = s.exec(select(Vendor).where(Vendor.name == q.vendor_name)).first()
                if matched:
                    q.vendor_id = matched.id
            else:
                # 기존 vendor_id 가 있지만 이름이 다르면(사용자가 다른 이름으로 타이핑) 재매칭
                current_v = s.get(Vendor, q.vendor_id)
                if not current_v or current_v.name != q.vendor_name:
                    matched = s.exec(select(Vendor).where(Vendor.name == q.vendor_name)).first()
                    q.vendor_id = matched.id if matched else None
        q.project_name = (form.get("project_name") or "").strip()

        # ⭐ project_id (기존 프로젝트 연결 변경) 처리
        p_id_raw = (form.get("project_id") or "").strip()
        try:
            new_pid = int(p_id_raw) if p_id_raw else None
        except (ValueError, TypeError):
            new_pid = None
        # 기존 연결이 없거나 신규로 지정된 경우에만 반영 (기존 연결은 유지)
        if new_pid and not q.project_id:
            proj_link = s.get(Project, new_pid)
            if proj_link:
                q.project_id = new_pid
                # 상태가 '견적'이면 '수주'로 승격
                if q.status == "견적":
                    q.status = "수주"

        q.subtotal = amounts["subtotal"]
        q.discount = amounts["discount"]
        q.after_discount = amounts["after_discount"]
        q.vat = amounts["vat"]
        q.total = amounts["total"]
        q.vat_mode = vat_mode
        q.valid_until = valid_until_p
        q.payment_terms = form.get("payment_terms") or ""
        q.delivery_terms = form.get("delivery_terms") or ""
        q.notes = form.get("notes") or ""
        try:
            q.updated_by = _user(request).id
            q.updated_at = datetime.now()
        except Exception:
            pass
        s.add(q)

        # 기존 품목 삭제 후 재생성
        old_items = s.exec(select(QuoteItem).where(QuoteItem.quote_id == qid)).all()
        for it in old_items:
            s.delete(it)
        s.commit()

        for idx, it in enumerate(items, start=1):
            try:
                _qty = int(float(str(it.get("quantity", 1) or 1).replace(',', '')))
            except (ValueError, TypeError):
                _qty = 1
            if _qty < 1:
                _qty = 1
            _up = _safe_money(it.get("unit_price", 0))
            _cp = _safe_money(it.get("cost_price", 0))
            _amt = _safe_money(it.get("amount", 0)) or (_qty * _up)
            qi = QuoteItem(
                quote_id=qid, seq=idx,
                name=(it.get("name") or "").strip(),
                spec=(it.get("spec") or "").strip(),
                quantity=_qty,
                unit=(it.get("unit") or "EA").strip(),
                unit_price=_up,
                cost_price=_cp,
                amount=_amt,
                memo=(it.get("memo") or "").strip(),
            )
            s.add(qi)
        s.commit()

        # ⭐ 연결된 프로젝트가 있으면 금액·행사일·프로젝트명 자동 동기화
        if q.project_id:
            p = s.get(Project, q.project_id)
            if p:
                _sync_project_from_quote(p, q, items)
                s.add(p)
                s.commit()

    return RedirectResponse(f"/quotes/{qid}", status_code=303)


def _sync_project_from_quote(p, q, items):
    """견적서 수정 시 연결된 프로젝트도 자동 갱신.
    - 거래처 (vendor_id) ← ⭐ 신규 추가
    - 매출/공급가/VAT/부가세 모드
    - 행사일 (event_date)
    - 프로젝트명 (project_name)
    ★ 프로젝트 코드는 변경하지 않음 (프로젝트 고유 식별자 유지)
    ★ status/settlement_status/paid_date/입금정보 등 프로젝트 자체 관리 필드는 건드리지 않음
    """
    # ⭐ 거래처 동기화 — 견적서의 vendor_id를 프로젝트에도 반영
    # (견적 수정 로직에서 vendor_name 기반 자동 매칭이 이미 수행됨)
    if q.vendor_id is not None:
        p.vendor_id = q.vendor_id

    # 금액 동기화
    if q.vat_mode == "total":
        p.revenue = q.total
        p.supply_amount = q.after_discount
        p.vat_amount = q.vat
    elif q.vat_mode == "cash":
        p.revenue = q.after_discount
        p.supply_amount = q.after_discount
        p.vat_amount = 0
    else:  # supply
        p.revenue = q.total
        p.supply_amount = q.after_discount
        p.vat_amount = q.vat
    p.vat_mode = q.vat_mode or "supply"
    p.cash_no_invoice = (q.vat_mode == "cash")

    # 행사일 동기화 (견적서에 있으면 반영)
    if q.event_date:
        p.event_date = q.event_date

    # 프로젝트명 (견적서에 project_name 있으면 반영)
    if q.project_name:
        p.name = q.project_name

    # ⭐ 특이사항(메모) 동기화 — 견적서의 notes를 프로젝트 special_notes에 반영
    # 견적서에 특이사항이 있을 때만 덮어씀 (빈 값으로 지우지 않음)
    if q.notes:
        p.special_notes = q.notes


@router.get("/quotes/{qid}/print", response_class=HTMLResponse)
def quote_print(request: Request, qid: int):
    """인쇄용 깨끗한 견적서"""
    _u = _user(request)
    from permissions import has_permission as _hpq
    if not _hpq(_u, "view_revenue"):
        raise HTTPException(403, "견적서 출력은 매출 금액 조회 권한이 필요합니다.")
    with Session(engine, expire_on_commit=False) as s:
        q = s.get(Quote, qid)
        if not q:
            raise HTTPException(404)
        items_raw = s.exec(select(QuoteItem).where(QuoteItem.quote_id == qid).order_by(QuoteItem.seq)).all()
        q_dict = _quote_to_dict(q)
        items_list = _items_to_list(items_raw)
    return templates.TemplateResponse(request, "quote_print.html", {
        "q": q_dict, "items": items_list,
    })


# ════════════════════════════════════════════════════════════════════
# 견적서 PDF 다운로드 (서버 생성)
#
# [왜 PDF 방식인가]
# 삼성인터넷·크롬의 '강제 다크모드'는 CSS 우선순위가 아니라 화면을 그리는
# 단계에서 색을 반전시키는 기능이다. 따라서 background:#FFF !important,
# color-scheme, 배경이미지 기법 등 어떤 CSS로도 완전히 막을 수 없다.
# → 서버가 PDF를 만들어 내려주면 브라우저는 '파일'을 받을 뿐이므로
#   다크모드가 개입할 여지가 아예 없다. 흰 배경 + 테마 컬러가 100% 보장된다.
# 부수 효과: 저장 파일명을 서버가 지정할 수 있다 (견적번호_프로젝트명).
# ════════════════════════════════════════════════════════════════════

def _safe_filename(text: str) -> str:
    """파일명에 쓸 수 없는 문자 제거 (Windows/macOS/Android 공통 금지문자)"""
    import re
    text = (text or "").strip()
    text = re.sub(r'[\\/:*?"<>|\r\n\t]', "", text)   # 금지문자 제거
    text = re.sub(r"\s+", " ", text).strip()            # 연속 공백 정리
    return text[:80]                                     # 과도한 길이 방지


def _quote_pdf_filename(q_dict: dict) -> str:
    """저장 파일명 = 견적번호 + 프로젝트명 (없으면 거래처명으로 대체)"""
    no = _safe_filename(q_dict.get("quote_number") or "")
    name = _safe_filename(q_dict.get("project_name") or "")
    if not name:
        name = _safe_filename(q_dict.get("vendor_name") or "")
    parts = [p for p in (no, name) if p]
    base = "_".join(parts) if parts else "견적서"
    return f"{base}.pdf"


@router.get("/quotes/{qid}/pdf")
def quote_pdf(request: Request, qid: int, inline: int = 0):
    """견적서를 서버에서 PDF로 생성해 다운로드.
    inline=1 이면 브라우저 내장 뷰어로 열기(모바일 미리보기용)."""
    _u = _user(request)
    from permissions import has_permission as _hpq
    if not _hpq(_u, "view_revenue"):
        raise HTTPException(403, "견적서 출력은 매출 금액 조회 권한이 필요합니다.")
    with Session(engine, expire_on_commit=False) as s:
        q = s.get(Quote, qid)
        if not q:
            raise HTTPException(404)
        items_raw = s.exec(
            select(QuoteItem).where(QuoteItem.quote_id == qid).order_by(QuoteItem.seq)
        ).all()
        q_dict = _quote_to_dict(q)
        items_list = _items_to_list(items_raw)

    # 인쇄용 템플릿을 그대로 렌더 → 화면 인쇄본과 100% 동일한 결과
    html_str = templates.get_template("quote_print.html").render({
        "request": request,
        "q": q_dict,
        "items": items_list,
    })

    filename = _quote_pdf_filename(q_dict)

    try:
        from weasyprint import HTML as _WeasyHTML
    except Exception:
        # PDF 엔진 미설치 환경 → 인쇄 화면으로 대체 (기능 중단 방지)
        return RedirectResponse(f"/quotes/{qid}/print", status_code=303)

    base_url = str(request.base_url)
    pdf_bytes = _WeasyHTML(string=html_str, base_url=base_url).write_pdf()

    from urllib.parse import quote as _urlquote
    disp = "inline" if inline else "attachment"
    # 한글 파일명 → RFC 5987 (filename* ) 로 전달해야 안드로이드/iOS에서 안 깨짐
    cd = f"{disp}; filename*=UTF-8''{_urlquote(filename)}"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": cd,
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


@router.post("/quotes/{qid}/status")
def quote_update_status(request: Request, qid: int, status: str = Form(...)):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        q = s.get(Quote, qid)
        if not q:
            raise HTTPException(404)
        q.status = status
        s.add(q)
        s.commit()
        # 수주 확정 시 → 프로젝트 미리보기로 자동 이동 (이미 연결된 경우 제외)
        if status == "수주" and not q.project_id:
            return RedirectResponse(f"/quotes/{qid}/to-project", status_code=303)
    return RedirectResponse(f"/quotes/{qid}", status_code=303)


# ============================================================
# 견적서 → 프로젝트 변환 (확인 후 생성)
# ============================================================

@router.get("/quotes/{qid}/to-project", response_class=HTMLResponse)
def quote_to_project_preview(request: Request, qid: int):
    """수주된 견적서를 프로젝트로 등록하기 전 미리보기 (행사일·카테고리·메모 확인)"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        q = s.get(Quote, qid)
        if not q:
            raise HTTPException(404)
        # 이미 프로젝트 연결됨 → 해당 프로젝트로 이동
        if q.project_id:
            existing = s.get(Project, q.project_id)
            if existing:
                return RedirectResponse(f"/projects/{q.project_id}", status_code=303)
        items_raw = s.exec(select(QuoteItem).where(QuoteItem.quote_id == qid).order_by(QuoteItem.seq)).all()
        items_list = _items_to_list(items_raw)
        q_dict = _quote_to_dict(q)

        # 자동 분류된 카테고리
        auto_cats = _infer_categories(items_raw)
        # 특이사항(품목표) 미리 생성
        items_text = _items_to_text_table(items_raw)

        # 거래처 정보
        vendor = s.get(Vendor, q.vendor_id) if q.vendor_id else None

        # 프로젝트 코드 자동 생성
        default_code = _generate_project_code()

        # 프로젝트명: 견적서의 project_name 우선, 없으면 vendor_name + 견적서번호
        default_name = q.project_name or f"{q.vendor_name} {q.quote_number}"

        # ── 견적서 정보 → 프로젝트 미리보기로 자동 전달 ──
        # 1) 행사일: 견적서의 event_date 그대로 (없으면 빈 값)
        default_event_date = q.event_date.strftime("%Y-%m-%d") if q.event_date else ""

        # 2) 부가세 모드: 견적서 그대로 사용 (supply/total/cash)
        default_vat_mode = q.vat_mode or "supply"

        # 3) 금액 계산: 부가세 모드별로 입력 필드에 들어갈 값과 분해값을 미리 계산
        if default_vat_mode == "total":
            # 총액 입력 모드 → 입력란에는 총액(=q.total)을 넣음
            default_supply = q.after_discount
            default_vat = q.vat
            default_total = q.total
            default_amount_input = q.total
        elif default_vat_mode == "cash":
            # 현금(무증빙) 모드 → 부가세 0, 입력란은 공급가 = 총액
            default_supply = q.after_discount
            default_vat = 0
            default_total = q.after_discount
            default_amount_input = q.after_discount
        else:  # supply (기본)
            # 공급가 입력 모드 → 입력란은 공급가, VAT 별도
            default_supply = q.after_discount
            default_vat = q.vat
            default_total = q.total
            default_amount_input = q.after_discount

        # 4) 결제 방법 추론: 견적서 payment_terms에서 키워드로 추정 (수정 가능)
        pt = (q.payment_terms or "").lower()
        if "카드" in pt:
            default_payment_method = "카드"
        elif "현금" in pt or default_vat_mode == "cash":
            default_payment_method = "현금"
        elif "계좌" in pt or "이체" in pt:
            default_payment_method = "계좌이체"
        elif "세금계산서" in pt:
            default_payment_method = "세금계산서"
        else:
            default_payment_method = ""

    from routes_project import CATEGORY_OPTIONS, CATEGORY_ICONS
    return templates.TemplateResponse(request, "quote_to_project.html", {
        "user": user, "q": q_dict, "items": items_list,
        "vendor": {"id": vendor.id, "name": vendor.name} if vendor else None,
        "auto_categories": auto_cats,
        "items_text": items_text,
        "default_code": default_code,
        "default_name": default_name,
        "default_supply": default_supply,
        "default_total": default_total,
        "default_vat": default_vat,
        "default_amount_input": default_amount_input,
        "default_event_date": default_event_date,
        "default_vat_mode": default_vat_mode,
        "default_payment_method": default_payment_method,
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
        "today": date.today(),
    })


@router.post("/quotes/{qid}/to-project")
def quote_to_project_create(
    request: Request, qid: int,
    code: str = Form(...),
    name: str = Form(...),
    event_date: str = Form(""),
    location: str = Form(""),
    amount_input: str = Form("0"),
    vat_mode: str = Form("supply"),
    status: str = Form("준비중"),
    payment_method: str = Form(""),
    special_notes: str = Form(""),
    memo: str = Form(""),
    categories_csv: str = Form(""),
):
    """미리보기에서 확인된 정보로 실제 프로젝트 생성 + 견적서에 연결"""
    _user(request)
    # 매출액 파싱
    def _to_int(v):
        try:
            return int(str(v).replace(",", "").replace(" ", "").strip() or 0)
        except (ValueError, TypeError):
            return 0
    amount_int = _to_int(amount_input)
    # VAT 분해
    if vat_mode == "total":
        total = amount_int
        supply = round(total / 1.1)
        vat = total - supply
    elif vat_mode == "cash":
        supply = amount_int
        vat = 0
        total = amount_int
    else:  # supply
        supply = amount_int
        vat = round(supply * 0.1)
        total = supply + vat

    cats = ",".join([c.strip() for c in (categories_csv or "").split(",") if c.strip()])
    is_cash_no_invoice = (vat_mode == "cash")

    # ⭐ 날짜 안전 파싱 (다양한 포맷 허용, 잘못돼도 500 에러 안 남)
    from routes_project import _parse_date_safe, _unique_project_code
    event_date_parsed = _parse_date_safe(event_date)

    with Session(engine, expire_on_commit=False) as s:
        q = s.get(Quote, qid)
        if not q:
            raise HTTPException(404)
        if q.project_id:
            # 이미 연결된 경우 중복 생성 방지
            return RedirectResponse(f"/projects/{q.project_id}", status_code=303)

        # ⭐ 프로젝트 코드 중복 자동 회피 (P-2026-001-A, -B ...)
        final_code = _unique_project_code(s, code)

        try:
            p = Project(
                code=final_code, name=(name or "").strip(),
                vendor_id=q.vendor_id,
                event_date=event_date_parsed,
                location=(location or "").strip(),
                revenue=total,
                supply_amount=supply,
                vat_amount=vat,
                vat_mode=vat_mode if vat_mode in ("supply", "total", "cash") else "supply",
                status=status if status in ("준비중", "진행중", "완료", "취소") else "준비중",
                settlement_status="미수금",
                payment_method=payment_method or "",
                cash_no_invoice=is_cash_no_invoice,
                special_notes=special_notes or "",
                memo=memo or "",
                categories=cats,
            )
            s.add(p)
            s.commit()
            s.refresh(p)
        except Exception as ex:
            s.rollback()
            raise HTTPException(
                400,
                f"프로젝트 생성 실패: {type(ex).__name__} — {str(ex)[:200]}"
            )

        # 견적서 ↔ 프로젝트 연결
        q.project_id = p.id
        # 상태가 아직 '수주'가 아니라면 자동 갱신
        if q.status != "수주":
            q.status = "수주"
        s.add(q)
        s.commit()

    return RedirectResponse(f"/projects/{p.id}?from_quote={qid}", status_code=303)


@router.post("/quotes/{qid}/delete")
def quote_delete(request: Request, qid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        items = s.exec(select(QuoteItem).where(QuoteItem.quote_id == qid)).all()
        for it in items:
            s.delete(it)
        q = s.get(Quote, qid)
        if q:
            s.delete(q)
        s.commit()
    return RedirectResponse("/quotes", status_code=303)
