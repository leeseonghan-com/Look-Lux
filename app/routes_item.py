"""단가표 (납품 + 렌탈 동시 보유 가능)"""
from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select

from database import engine, Item, Equipment, User
from template_utils import templates

router = APIRouter()


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        return s.get(User, uid)


def _next_code() -> str:
    """다음 사용 가능한 품목코드 생성: I-001 (Item 통합)
    기존 P-/R- 코드도 충돌 검사에 포함."""
    with Session(engine, expire_on_commit=False) as s:
        items = s.exec(select(Item).where(Item.code.like("I-%"))).all()
        nums = []
        for it in items:
            try:
                n = int(it.code.split("-", 1)[1])
                nums.append(n)
            except (ValueError, IndexError):
                pass
        next_num = (max(nums) + 1) if nums else 1
        # 충돌 회피
        while True:
            candidate = f"I-{next_num:03d}"
            if not s.exec(select(Item).where(Item.code == candidate)).first():
                return candidate
            next_num += 1


def _classify(item) -> str:
    """가격 입력 상태로 용도 자동 판별 (UI 배지 표시용)
    - 둘 다 있음 → 겸용
    - 납품만 있음 → 납품
    - 렌탈만 있음 → 렌탈
    - 둘 다 없음 → 미설정
    """
    has_sale = (item.consumer_price or 0) > 0
    has_rent = (item.rental_daily or 0) > 0
    if has_sale and has_rent:
        return "겸용"
    if has_sale:
        return "납품"
    if has_rent:
        return "렌탈"
    return "미설정"


def _to_int(value, default: int = 0) -> int:
    """콤마 포함 가능한 금액 안전 파싱"""
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


@router.get("/items", response_class=HTMLResponse)
def item_list(request: Request, usage: str = "all"):
    """단가표 목록. usage = all/sale/rental/combo/unset (가격 입력 상태로 필터)"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        items_raw = s.exec(select(Item).where(Item.is_active == True)).all()
        # 연결된 장비 정보 조회 (있는 경우)
        eq_map = {e.id: e for e in s.exec(select(Equipment)).all()}

        # 필터링 + 자동 분류
        rows = []
        for i in items_raw:
            kind = _classify(i)
            # 필터
            if usage != "all":
                want = {"sale": "납품", "rental": "렌탈", "combo": "겸용", "unset": "미설정"}.get(usage)
                if kind != want:
                    continue
            eq = eq_map.get(i.equipment_id) if i.equipment_id else None
            rows.append({
                "id": i.id, "code": i.code, "name": i.name, "spec": i.spec,
                "unit": i.unit,
                "import_price": i.import_price, "dealer_price": i.dealer_price,
                "consumer_price": i.consumer_price,
                "rental_daily": i.rental_daily, "rental_deposit": i.rental_deposit,
                "memo": i.memo,
                "usage": kind,  # 겸용/납품/렌탈/미설정
                "equipment_id": i.equipment_id,
                "equipment_name": eq.name if eq else "",
                "equipment_category": eq.category if eq else "",
            })

        # 전체 통계
        all_items = items_raw
        counts = {
            "total": len(all_items),
            "combo": sum(1 for i in all_items if _classify(i) == "겸용"),
            "sale": sum(1 for i in all_items if _classify(i) == "납품"),
            "rental": sum(1 for i in all_items if _classify(i) == "렌탈"),
            "unset": sum(1 for i in all_items if _classify(i) == "미설정"),
            "linked": sum(1 for i in all_items if i.equipment_id),
        }
    return templates.TemplateResponse(request, "items.html", {
        "user": user, "items": rows, "usage": usage, "counts": counts,
    })


@router.get("/items/new", response_class=HTMLResponse)
def item_new(request: Request):
    user = _user(request)
    suggested_code = _next_code()
    return templates.TemplateResponse(request, "item_new.html", {
        "user": user, "suggested_code": suggested_code,
    })


@router.post("/items/new")
def item_create(
    request: Request,
    code: str = Form(""),
    name: str = Form(...), spec: str = Form(""),
    unit: str = Form("EA"),
    import_price: str = Form("0"),
    dealer_price: str = Form("0"),
    consumer_price: str = Form("0"),
    rental_daily: str = Form("0"),
    rental_deposit: str = Form("0"),
    memo: str = Form(""),
):
    user = _user(request)
    code = (code or "").strip()
    name = (name or "").strip()

    if not name:
        return templates.TemplateResponse(request, "error.html", {
            "user": user, "title": "입력 오류",
            "message": "품목명을 입력해주세요.",
            "back_url": "/items/new",
        }, status_code=400)

    with Session(engine, expire_on_commit=False) as s:
        if not code:
            code = _next_code()
        existing = s.exec(select(Item).where(Item.code == code)).first()
        if existing:
            new_code = _next_code()
            return templates.TemplateResponse(request, "error.html", {
                "user": user, "title": "품목 코드 중복",
                "message": f"코드 '{code}' 는 이미 사용 중입니다.\n자동 생성 권장: '{new_code}'",
                "back_url": "/items/new",
                "suggestion_code": new_code,
            }, status_code=400)
        i = Item(
            code=code, name=name, spec=spec, unit=unit, category="겸용",
            import_price=_to_int(import_price),
            dealer_price=_to_int(dealer_price),
            consumer_price=_to_int(consumer_price),
            rental_daily=_to_int(rental_daily),
            rental_deposit=_to_int(rental_deposit),
            memo=memo,
        )
        s.add(i)
        s.commit()
    return RedirectResponse("/items", status_code=303)


@router.get("/items/{iid}/edit", response_class=HTMLResponse)
def item_edit(request: Request, iid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        i = s.get(Item, iid)
        if not i:
            raise HTTPException(404)
        eq = s.get(Equipment, i.equipment_id) if i.equipment_id else None
        idata = {
            "id": i.id, "code": i.code, "name": i.name, "spec": i.spec,
            "unit": i.unit,
            "import_price": i.import_price, "dealer_price": i.dealer_price,
            "consumer_price": i.consumer_price,
            "rental_daily": i.rental_daily, "rental_deposit": i.rental_deposit,
            "memo": i.memo,
            "usage": _classify(i),
            "equipment_id": i.equipment_id,
            "equipment_name": eq.name if eq else "",
        }
    return templates.TemplateResponse(request, "item_edit.html", {"user": user, "i": idata})


@router.post("/items/{iid}/edit")
def item_update(
    request: Request, iid: int,
    code: str = Form(...),
    name: str = Form(...), spec: str = Form(""),
    unit: str = Form("EA"),
    import_price: str = Form("0"),
    dealer_price: str = Form("0"),
    consumer_price: str = Form("0"),
    rental_daily: str = Form("0"),
    rental_deposit: str = Form("0"),
    memo: str = Form(""),
):
    user = _user(request)
    code = (code or "").strip()
    with Session(engine, expire_on_commit=False) as s:
        i = s.get(Item, iid)
        if not i:
            raise HTTPException(404)
        if code != i.code:
            existing = s.exec(select(Item).where(Item.code == code)).first()
            if existing:
                return templates.TemplateResponse(request, "error.html", {
                    "user": user, "title": "품목 코드 중복",
                    "message": f"코드 '{code}' 는 이미 사용 중입니다.",
                    "back_url": f"/items/{iid}/edit",
                }, status_code=400)
            i.code = code
        i.name = name; i.spec = spec; i.unit = unit
        i.import_price = _to_int(import_price)
        i.dealer_price = _to_int(dealer_price)
        i.consumer_price = _to_int(consumer_price)
        i.rental_daily = _to_int(rental_daily)
        i.rental_deposit = _to_int(rental_deposit)
        i.memo = memo
        # category는 자동 계산
        i.category = _classify(i)
        s.add(i)
        s.commit()
    return RedirectResponse("/items", status_code=303)


@router.post("/items/{iid}/delete")
def item_delete(request: Request, iid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        i = s.get(Item, iid)
        if i:
            i.is_active = False
            s.add(i)
            s.commit()
    return RedirectResponse("/items", status_code=303)


@router.post("/items/bulk-delete")
async def items_bulk_delete(request: Request):
    """선택된 품목 일괄 삭제 (비활성화)"""
    _user(request)
    form = await request.form()
    ids_str = form.get("ids", "")
    try:
        ids = [int(x) for x in ids_str.split(",") if x.strip()]
    except ValueError:
        ids = []
    deleted = 0
    with Session(engine, expire_on_commit=False) as s:
        for iid in ids:
            i = s.get(Item, iid)
            if i and i.is_active:
                i.is_active = False
                s.add(i)
                deleted += 1
        s.commit()
    return RedirectResponse(f"/items?deleted={deleted}", status_code=303)


@router.post("/items/{iid}/hard-delete")
def item_hard_delete(request: Request, iid: int):
    """품목 완전 삭제 (DB에서 제거)"""
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        i = s.get(Item, iid)
        if i:
            s.delete(i)
            s.commit()
    return RedirectResponse("/items?deleted=1", status_code=303)


# API: 견적서에서 품목명 자동완성용
@router.get("/api/items/search")
def items_search(request: Request, q: str = "", category: str = ""):
    """모든 품목 검색 — category 파라미터로 가격 우선순위만 결정.
    - category=렌탈 → rental_daily 우선
    - category=물품/납품 → consumer_price 우선
    """
    with Session(engine, expire_on_commit=False) as s:
        # ★ 견적서 자동완성은 견적 단가표(usage_type='quote')만 조회
        #   재고 관리용 장비(usage_type='inventory')는 표시하지 않음
        items = s.exec(
            select(Item)
            .where(Item.is_active == True)
            .where(Item.usage_type == "quote")
        ).all()

        # 검색어 매칭만 적용 (카테고리 필터는 제거 — 모든 품목 표시)
        if q:
            ql = q.lower().strip()
            items = [i for i in items
                     if ql in (i.name or "").lower()
                     or ql in (i.code or "").lower()
                     or ql in (i.spec or "").lower()]

        result = []
        is_rental = (category == "렌탈")
        for i in items[:30]:
            # 견적서 타입에 따라 우선 가격 선택, 없으면 다른 쪽 가격 반환
            if is_rental:
                price = i.rental_daily or i.consumer_price or 0
                price_kind = "렌탈/일" if i.rental_daily else ("납품가" if i.consumer_price else "")
            else:
                price = i.consumer_price or i.rental_daily or 0
                price_kind = "납품가" if i.consumer_price else ("렌탈/일" if i.rental_daily else "")
            result.append({
                "id": i.id, "code": i.code, "name": i.name, "spec": i.spec or "",
                "unit": i.unit or "EA", "price": price,
                "price_kind": price_kind,
                # 양쪽 가격 모두 표시용 (UI에서 활용 가능)
                "consumer_price": i.consumer_price or 0,
                "rental_daily": i.rental_daily or 0,
                "usage": _classify(i),
            })
        # ★ 로그인·금액 권한 확인 — 권한 없으면 가격을 보내지 않음
        from permissions import has_permission
        _u = getattr(request.state, "user", None)
        if not _u:
            return JSONResponse([], status_code=401)
        if not has_permission(_u, "view_revenue"):
            for r in result:
                r["price"] = 0; r["consumer_price"] = 0; r["rental_daily"] = 0; r["price_kind"] = ""
        return JSONResponse(result)
