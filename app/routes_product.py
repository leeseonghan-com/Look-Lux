"""물품 등록 통합 라우트 — 단가표(Item)와 보유장비(Equipment)를 하나의 인터페이스로 관리.

핵심 설계:
- "물품" 1건 = Item 1건 + (선택) Equipment 1건 + (선택) EquipmentUnit N건
- Item이 항상 마스터 (이름·가격 정보 보유)
- Equipment는 "재고 관리하는 물품"인 경우만 자동 생성
- 한 화면에서 가격/규격/개체(일련번호/QR/수리이력)까지 처리
"""
from datetime import date, datetime
from typing import Optional
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from database import (
    engine, Item, Equipment, EquipmentUnit, MaintenanceLog, User,
    get_equipment_prefixes, DEFAULT_EQUIPMENT_PREFIXES,
)
from template_utils import templates

router = APIRouter()


def _user(request: Request):
    """기본 사용자 — products 권한 체크 포함"""
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        user = s.get(User, uid)
    if user:
        from permissions import has_permission
        if not has_permission(user, "products"):
            raise HTTPException(403, "물품 등록 접근 권한이 없습니다.")
    return user


def _to_int(v, default=0):
    if v is None:
        return default
    if isinstance(v, int):
        return v
    s = str(v).strip().replace(",", "").replace(" ", "")
    if not s:
        return default
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return default


def _classify(item) -> str:
    """가격으로 용도 자동 판별"""
    has_sale = (item.consumer_price or 0) > 0
    has_rent = (item.rental_daily or 0) > 0
    if has_sale and has_rent:
        return "겸용"
    if has_sale:
        return "납품"
    if has_rent:
        return "렌탈"
    return "미설정"


def _next_item_code(usage_type: str = "quote") -> str:
    """다음 품목 코드 — 견적/재고 완전 분리된 코드 체계
    - 견적: Q-001, Q-002, ... (Quote 단가표)
    - 재고: E-001, E-002, ... (Equipment)
    """
    prefix = "Q" if usage_type == "quote" else "E"
    with Session(engine, expire_on_commit=False) as s:
        existing_codes = {it.code for it in s.exec(select(Item)).all()}
        next_num = 1
        while True:
            candidate = f"{prefix}-{next_num:03d}"
            if candidate not in existing_codes:
                return candidate
            next_num += 1


def _next_asset_code(category: str = "기타") -> str:
    """개체 관리코드 (SC-2026-0001 ...)"""
    prefixes = get_equipment_prefixes()
    prefix = prefixes.get(category, prefixes.get("기타", "EQ"))
    year = date.today().year
    pattern = f"{prefix}-{year}-"
    with Session(engine, expire_on_commit=False) as s:
        units = s.exec(select(EquipmentUnit).where(
            EquipmentUnit.asset_code.like(f"{pattern}%")
        )).all()
        nums = []
        for u in units:
            try:
                tail = u.asset_code.replace(pattern, "", 1)
                if tail.isdigit():
                    nums.append(int(tail))
            except (ValueError, IndexError):
                pass
        next_num = (max(nums) + 1) if nums else 1
        return f"{prefix}-{year}-{next_num:04d}"


CATEGORY_OPTIONS = ["음향", "조명", "영상", "구조물", "전원", "특수효과", "기타"]
CATEGORY_ICONS = {
    "음향": "🔊", "조명": "🔆", "영상": "📺",
    "구조물": "🏗️", "전원": "⚡", "특수효과": "✨", "기타": "📦",
}


# ============================================================
# 목록 페이지 (통합 메인)
# ============================================================
@router.get("/products", response_class=HTMLResponse)
def product_list(request: Request, category: str = "all", usage: str = "all", q: str = ""):
    """💰 견적 단가표 — 가격 정보만 관리 (usage_type='quote')"""
    return _render_product_list(request, category=category, usage=usage, q=q,
                                usage_type="quote", template_name="products.html")


@router.get("/inventory", response_class=HTMLResponse)
def inventory_list(request: Request, category: str = "all", q: str = ""):
    """📦 재고/장비 관리 — 개체별(QR) 재고 추적 (usage_type='inventory')"""
    return _render_product_list(request, category=category, usage="all", q=q,
                                usage_type="inventory", template_name="inventory_list.html")


def _render_product_list(request, category, usage, q, usage_type, template_name):
    """공통 목록 렌더러 — usage_type으로 필터링"""
    user = _user(request)
    search_q = (q or "").strip().lower()
    with Session(engine, expire_on_commit=False) as s:
        # ★ usage_type으로 1차 필터링
        items = s.exec(
            select(Item).where(Item.is_active == True).where(Item.usage_type == usage_type)
        ).all()
        if search_q:
            items = [i for i in items if any(search_q in (str(x) or "").lower() for x in [
                i.code, i.name, i.spec, i.unit, i.memo, i.category,
            ])]
        all_eqs = s.exec(select(Equipment).where(Equipment.is_active == True)).all()
        eq_by_id = {e.id: e for e in all_eqs}

        # 모든 개체 조회 (재고 카운트용)
        all_units = s.exec(select(EquipmentUnit)).all()
        units_by_eq = {}
        for u in all_units:
            units_by_eq.setdefault(u.equipment_id, []).append(u)

        # ⭐ usage_type에 따라 완전히 다른 rows 구조를 만든다 (중복 필드 제거)
        rows = []
        linked_eq_ids = set()
        for i in items:
            eq = eq_by_id.get(i.equipment_id) if i.equipment_id else None
            if eq:
                linked_eq_ids.add(eq.id)

            if usage_type == "quote":
                # 💰 견적 관점: 가격 관련만 (카테고리는 정식 옵션 중 하나여야 함)
                cat_val = i.category if i.category in CATEGORY_OPTIONS else "기타"
                row = {
                    "id": i.id,
                    "code": i.code, "name": i.name, "spec": i.spec, "unit": i.unit,
                    "category": cat_val,
                    "consumer_price": i.consumer_price,
                    "rental_daily": i.rental_daily,
                    "rental_deposit": i.rental_deposit,
                    "usage": _classify(i),
                    "memo": i.memo,
                }
            else:
                # 📦 재고 관점: 재고/장비 관련만 (가격 정보 절대 노출 X)
                units = units_by_eq.get(i.equipment_id, []) if i.equipment_id else []
                row = {
                    "id": i.id,
                    "code": i.code, "name": i.name, "spec": i.spec, "unit": i.unit,
                    "category": (eq.category if eq else i.category if i.category not in ("겸용",) else "기타"),
                    "equipment_id": i.equipment_id,
                    "manufacturer": eq.manufacturer if eq else "",
                    "model": eq.model if eq else "",
                    "total": len(units),
                    "available": sum(1 for u in units if u.status == "보유중"),
                    "on_rent": sum(1 for u in units if u.status == "대여중"),
                    "in_repair": sum(1 for u in units if u.status == "수리중"),
                    "has_inventory": eq is not None,
                    "memo": i.memo,
                }
            rows.append(row)

        # Equipment에만 있고 Item 연결 안 된 케이스도 노출 (재고 모드에서만)
        if usage_type == "inventory":
            for eq in all_eqs:
                if eq.id in linked_eq_ids:
                    continue
                units = units_by_eq.get(eq.id, [])
                rows.append({
                    "id": None,  # 단가표 미연동
                    "code": "(미연동)", "name": eq.name, "spec": eq.spec, "unit": "EA",
                    "category": eq.category,
                    "consumer_price": 0, "rental_daily": 0, "rental_deposit": 0,
                    "usage": "미설정",
                    "equipment_id": eq.id,
                    "manufacturer": eq.manufacturer, "model": eq.model,
                    "total": len(units),
                    "available": sum(1 for u in units if u.status == "보유중"),
                    "on_rent": sum(1 for u in units if u.status == "대여중"),
                    "in_repair": sum(1 for u in units if u.status == "수리중"),
                    "has_inventory": True,
                    "memo": eq.memo,
                    "orphan_equipment": True,
                })

        # 카운터
        cat_counts = {"all": len(rows)}
        for c in CATEGORY_OPTIONS:
            cat_counts[c] = sum(1 for r in rows if r.get("category") == c)

        # 견적 화면 통계 (가격 미설정 등) — 재고 화면에서는 usage 키가 없으므로 무해
        usage_counts = {
            "all": len(rows),
            "combo": sum(1 for r in rows if r.get("usage") == "겸용"),
            "sale": sum(1 for r in rows if r.get("usage") == "납품"),
            "rental": sum(1 for r in rows if r.get("usage") == "렌탈"),
            "unset": sum(1 for r in rows if r.get("usage") == "미설정"),
        }
        # 재고 화면 통계 (재고 추적 여부)
        with_inventory = sum(1 for r in rows if r.get("has_inventory"))

        # 카테고리 필터
        if category != "all":
            rows = [r for r in rows if r.get("category") == category]
        # 용도 필터는 견적 화면에서만 (재고는 용도 개념 없음)
        if usage_type == "quote" and usage != "all":
            want = {"sale": "납품", "rental": "렌탈", "combo": "겸용", "unset": "미설정"}.get(usage)
            if want:
                rows = [r for r in rows if r.get("usage") == want]

    return templates.TemplateResponse(request, template_name, {
        "user": user, "rows": rows,
        "category": category, "usage": usage,
        "cat_counts": cat_counts, "usage_counts": usage_counts,
        "category_options": CATEGORY_OPTIONS, "category_icons": CATEGORY_ICONS,
        "with_inventory": with_inventory,
        "usage_type": usage_type,  # 템플릿에서 링크/타이틀 분기용
        "search_q": search_q,
    })


# ============================================================
# 새 물품 등록
# ============================================================
@router.get("/products/new", response_class=HTMLResponse)
def product_new(request: Request):
    """💰 견적 단가표 — 새 품목 등록 (가격만, 재고 미관리)"""
    user = _user(request)
    return templates.TemplateResponse(request, "product_new.html", {
        "user": user,
        "suggested_code": _next_item_code("quote"),
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
        "usage_type": "quote",
    })


@router.get("/inventory/new", response_class=HTMLResponse)
def inventory_new(request: Request):
    """📦 재고/장비 관리 — 새 장비 등록 (재고 개체 추적 위주)"""
    user = _user(request)
    return templates.TemplateResponse(request, "inventory_new.html", {
        "user": user,
        "suggested_code": _next_item_code("inventory"),
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
        "usage_type": "inventory",
    })


@router.post("/products/new")
async def product_create(request: Request):
    """견적용 품목 등록"""
    return await _create_product(request, usage_type="quote")


@router.post("/inventory/new")
async def inventory_create(request: Request):
    """재고용 장비 등록"""
    return await _create_product(request, usage_type="inventory")


async def _create_product(request: Request, usage_type: str):
    """공통 등록 처리 — usage_type으로 저장 후 각 목록으로 리다이렉트"""
    user = _user(request)
    form = await request.form()

    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "물품명을 입력해주세요.")

    code = ((form.get("code") or "").strip()) or _next_item_code(usage_type)
    category = form.get("category") or "기타"
    # ⭐ 재고용은 가격을 저장하지 않음 (견적 단가표와 완전 분리)
    if usage_type == "inventory":
        consumer_price_i = 0
        rental_daily_i = 0
        rental_deposit_i = 0
    else:
        consumer_price_i = _to_int(form.get("consumer_price", "0"))
        rental_daily_i = _to_int(form.get("rental_daily", "0"))
        rental_deposit_i = _to_int(form.get("rental_deposit", "0"))
    initial_units_i = _to_int(form.get("initial_units", "0"))
    # 재고용은 항상 개체 추적 ON, 견적용은 체크박스 무시하고 항상 OFF
    if usage_type == "inventory":
        is_tracked = True
    else:
        is_tracked = False
    # 사양 데이터 입력 시 Equipment 메타 저장이 필요하지만 견적용은 사양 자체를 안 다룸
    has_spec_input = usage_type == "inventory" and (any(
        (form.get(f"spec_{k}") or "").strip() for k in
        ("power_w", "weight_kg", "ch_count", "spl_db",
         "rms_power_w", "peak_power_w", "freq_range", "notes")
    ) or any((form.get(f"spec_ch{i}_value") or "").strip() for i in range(1, 5)))

    # 초기 개체에 적용할 공통 정보
    unit_department = (form.get("unit_department") or "").strip()
    unit_manager    = (form.get("unit_manager") or "").strip()
    unit_location   = (form.get("unit_location") or "").strip()
    unit_status     = (form.get("unit_status") or "보유중").strip()
    if unit_status not in ("보유중", "대여중", "수리중", "폐기"):
        unit_status = "보유중"
    unit_purchase_vendor = (form.get("unit_purchase_vendor") or "").strip()
    unit_purchase_date_str = (form.get("unit_purchase_date") or "").strip()
    try:
        unit_purchase_date = datetime.strptime(unit_purchase_date_str, "%Y-%m-%d").date() if unit_purchase_date_str else None
    except (ValueError, TypeError):
        unit_purchase_date = None
    unit_purchase_price = _to_int(form.get("unit_purchase_price", "0"))
    unit_memo = (form.get("unit_memo") or "").strip()

    spec = (form.get("spec") or "").strip()
    unit_label = form.get("unit", "EA") or "EA"
    manufacturer = (form.get("manufacturer") or "").strip()
    model = (form.get("model") or "").strip()
    memo = (form.get("memo") or "").strip()

    # ───── 카테고리별 상세 사양 수집 (조명/음향) ─────
    import json as _json
    spec_data = {}
    for key in ("power_w", "weight_kg", "ch_count", "spl_db",
                "rms_power_w", "peak_power_w", "freq_range", "notes"):
        v = (form.get(f"spec_{key}") or "").strip()
        if v:
            spec_data[key] = v
    # 조명 채널 모드 (4개) — 변수명 ch_name으로 분리 (외부의 name과 충돌 방지!)
    for i in range(1, 5):
        ch_name = (form.get(f"spec_ch{i}_name") or "").strip()
        ch_val = (form.get(f"spec_ch{i}_value") or "").strip()
        if ch_name or ch_val:
            spec_data[f"ch{i}_name"] = ch_name or f"MODE {i}"
            spec_data[f"ch{i}_value"] = ch_val
    spec_data_json = _json.dumps(spec_data, ensure_ascii=False) if spec_data else "{}"

    with Session(engine, expire_on_commit=False) as s:
        existing = s.exec(select(Item).where(Item.code == code)).first()
        if existing:
            code = _next_item_code(usage_type)

        equipment_id = None
        if is_tracked:
            eq = Equipment(
                name=name, model=model, manufacturer=manufacturer,
                category=category, spec=spec, memo=memo,
                spec_data=spec_data_json,
            )
            s.add(eq)
            s.commit()
            s.refresh(eq)
            equipment_id = eq.id

            if initial_units_i > 0:
                prefixes = get_equipment_prefixes()
                prefix = prefixes.get(category, prefixes.get("기타", "EQ"))
                year = date.today().year
                pattern = f"{prefix}-{year}-"
                existing_units = s.exec(select(EquipmentUnit).where(
                    EquipmentUnit.asset_code.like(f"{pattern}%")
                )).all()
                nums = []
                for u in existing_units:
                    tail = u.asset_code.replace(pattern, "", 1)
                    if tail.isdigit():
                        nums.append(int(tail))
                next_num = (max(nums) + 1) if nums else 1
                for _ in range(initial_units_i):
                    asset_code = f"{prefix}-{year}-{next_num:04d}"
                    unit_obj = EquipmentUnit(
                        equipment_id=eq.id,
                        asset_code=asset_code,
                        rental_price_daily=rental_daily_i,
                        purchase_price=unit_purchase_price,
                        status=unit_status,
                        department=unit_department,
                        manager=unit_manager,
                        location=unit_location,
                        purchase_vendor=unit_purchase_vendor,
                        purchase_date=unit_purchase_date,
                        memo=unit_memo,
                    )
                    s.add(unit_obj)
                    next_num += 1
            s.commit()

        # Item 생성 — 카테고리는 사용자가 선택한 값 유지 (조명/음향/영상/구조물/전원/특수효과/기타)
        item_category = category if category in CATEGORY_OPTIONS else "기타"
        item = Item(
            code=code, name=name, spec=spec, unit=unit_label,
            consumer_price=consumer_price_i,
            rental_daily=rental_daily_i,
            rental_deposit=rental_deposit_i,
            category=item_category,
            usage_type=usage_type,
            equipment_id=equipment_id,
            memo=memo,
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        new_id = item.id

    # ★ 등록 완료 후 각각의 목록 홈으로 이동 (상세/수정 페이지가 아닌 목록)
    if usage_type == "inventory":
        return RedirectResponse("/inventory?created=1", status_code=303)
    return RedirectResponse("/products?created=1", status_code=303)


# ============================================================
# 물품 상세 (한 화면에서 가격 + 개체 + 수리이력)
# ============================================================
@router.get("/products/{pid}", response_class=HTMLResponse)
def product_detail(request: Request, pid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(Item, pid)
        if not item or not item.is_active:
            raise HTTPException(404)
        eq = s.get(Equipment, item.equipment_id) if item.equipment_id else None
        units = []
        maint_logs = []
        if eq:
            units_raw = s.exec(
                select(EquipmentUnit).where(EquipmentUnit.equipment_id == eq.id).order_by(EquipmentUnit.asset_code)
            ).all()
            units = [{
                "id": u.id, "asset_code": u.asset_code,
                "serial_number": u.serial_number,
                "status": u.status, "location": u.location,
                "manager": u.manager,
                "purchase_date": u.purchase_date,
                "purchase_price": u.purchase_price,
                "rental_price_daily": u.rental_price_daily,
                "last_check_date": u.last_check_date,
                "memo": u.memo,
            } for u in units_raw]
            # 수리이력 (최근 10건)
            unit_ids = [u.id for u in units_raw]
            if unit_ids:
                maint_raw = s.exec(
                    select(MaintenanceLog).where(MaintenanceLog.unit_id.in_(unit_ids))
                    .order_by(MaintenanceLog.log_date.desc())
                ).all()[:10]
                maint_logs = [{
                    "id": m.id, "unit_id": m.unit_id,
                    "log_date": m.log_date, "log_type": m.log_type,
                    "description": m.description, "cost": m.cost,
                    "vendor": m.vendor,
                    "asset_code": next((u["asset_code"] for u in units if u["id"] == m.unit_id), ""),
                } for m in maint_raw]

        # 카테고리별 상세 사양 파싱
        import json as _json
        spec_data = {}
        if eq and eq.spec_data:
            try:
                spec_data = _json.loads(eq.spec_data)
                if not isinstance(spec_data, dict):
                    spec_data = {}
            except (ValueError, TypeError):
                spec_data = {}

        pd = {
            "id": item.id, "code": item.code, "name": item.name,
            "spec": item.spec, "unit": item.unit,
            "consumer_price": item.consumer_price,
            "rental_daily": item.rental_daily,
            "rental_deposit": item.rental_deposit,
            "usage": _classify(item),
            "usage_type": getattr(item, "usage_type", "quote"),
            "memo": item.memo,
            "equipment_id": item.equipment_id,
            # ★ 카테고리 우선순위: Item.category (사용자 선택) → Equipment.category → "기타"
            "category": (
                item.category if item.category in CATEGORY_OPTIONS
                else (eq.category if eq and eq.category in CATEGORY_OPTIONS else "기타")
            ),
            "manufacturer": eq.manufacturer if eq else "",
            "model": eq.model if eq else "",
            "spec_data": spec_data,
        }

        # 재고 통계
        total = len(units)
        available = sum(1 for u in units if u["status"] == "보유중")
        on_rent = sum(1 for u in units if u["status"] == "대여중")
        in_repair = sum(1 for u in units if u["status"] == "수리중")

        # 다음 개체 코드 추천
        next_asset = _next_asset_code(pd["category"])

    # ⭐ usage_type에 따라 완전히 다른 상세 페이지 렌더 (기능 중복 제거)
    template = "product_detail_quote.html" if pd["usage_type"] == "quote" else "product_detail.html"
    return templates.TemplateResponse(request, template, {
        "user": user, "p": pd, "units": units, "maint_logs": maint_logs,
        "total": total, "available": available, "on_rent": on_rent, "in_repair": in_repair,
        "next_asset_code": next_asset,
        "category_options": CATEGORY_OPTIONS,
        "category_icons": CATEGORY_ICONS,
    })


# ============================================================
# 물품 수정 (가격, 규격, 카테고리 등)
# ============================================================
@router.post("/products/{pid}/edit")
async def product_update(request: Request, pid: int):
    """물품 수정 — Item + Equipment(있으면) 동시 갱신, spec_data 포함"""
    _user(request)
    form = await request.form()

    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "물품명을 입력해주세요.")

    consumer_price_i = _to_int(form.get("consumer_price", "0"))
    rental_daily_i = _to_int(form.get("rental_daily", "0"))
    rental_deposit_i = _to_int(form.get("rental_deposit", "0"))
    spec = (form.get("spec") or "").strip()
    unit_label = form.get("unit", "EA") or "EA"
    manufacturer = (form.get("manufacturer") or "").strip()
    model = (form.get("model") or "").strip()
    category = form.get("category", "기타") or "기타"
    memo = (form.get("memo") or "").strip()

    # 사양 데이터
    import json as _json
    spec_data = {}
    for key in ("power_w", "weight_kg", "ch_count", "spl_db",
                "rms_power_w", "peak_power_w", "freq_range", "notes"):
        v = (form.get(f"spec_{key}") or "").strip()
        if v:
            spec_data[key] = v
    for i in range(1, 5):
        nm = (form.get(f"spec_ch{i}_name") or "").strip()
        val = (form.get(f"spec_ch{i}_value") or "").strip()
        if nm or val:
            spec_data[f"ch{i}_name"] = nm or f"MODE {i}"
            spec_data[f"ch{i}_value"] = val
    spec_data_json = _json.dumps(spec_data, ensure_ascii=False) if spec_data else "{}"

    with Session(engine, expire_on_commit=False) as s:
        item = s.get(Item, pid)
        if not item:
            raise HTTPException(404)
        item.name = name; item.spec = spec; item.unit = unit_label
        # ⭐ 재고용 품목은 가격 필드 항상 0으로 강제 (견적 단가표와 완전 분리)
        item_usage_type = getattr(item, "usage_type", "quote")
        if item_usage_type == "inventory":
            item.consumer_price = 0
            item.rental_daily = 0
            item.rental_deposit = 0
        else:
            item.consumer_price = consumer_price_i
            item.rental_daily = rental_daily_i
            item.rental_deposit = rental_deposit_i
        item.memo = memo
        # ★ 카테고리는 사용자가 폼에서 선택한 값 그대로 유지 (조명/음향/영상 등)
        #   가격 유무로 자동 덮어쓰던 이전 로직 제거 — "용도(납품/렌탈/겸용)"는 _classify()로 파생 계산됨
        if category and category in CATEGORY_OPTIONS:
            item.category = category
        s.add(item)

        # Equipment 정보도 함께 갱신 (없으면 사양만 입력된 경우 새로 생성)
        if item.equipment_id:
            eq = s.get(Equipment, item.equipment_id)
            if eq:
                eq.name = name; eq.spec = spec
                eq.manufacturer = manufacturer; eq.model = model
                eq.category = category; eq.memo = memo
                eq.spec_data = spec_data_json
                s.add(eq)
        elif spec_data:
            # Equipment 없는데 사양이 입력됐다면 자동 생성
            eq = Equipment(
                name=name, model=model, manufacturer=manufacturer,
                category=category, spec=spec, memo=memo,
                spec_data=spec_data_json,
            )
            s.add(eq)
            s.commit()
            s.refresh(eq)
            item.equipment_id = eq.id
            s.add(item)
        s.commit()
    return RedirectResponse(f"/products/{pid}", status_code=303)


@router.post("/products/{pid}/delete")
def product_delete(request: Request, pid: int):
    """물품 비활성화 (개체는 유지) — usage_type에 따라 원래 목록으로 리다이렉트"""
    _user(request)
    ret = "/products"
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(Item, pid)
        if item:
            if getattr(item, "usage_type", "quote") == "inventory":
                ret = "/inventory"
            item.is_active = False
            s.add(item)
        # Equipment도 함께 비활성화
        if item and item.equipment_id:
            eq = s.get(Equipment, item.equipment_id)
            if eq:
                eq.is_active = False
                s.add(eq)
        s.commit()
    return RedirectResponse(f"{ret}?deleted=1", status_code=303)


@router.post("/products/bulk-delete")
async def products_bulk_delete(request: Request):
    return await _bulk_delete_impl(request, redirect_to="/products")


@router.post("/inventory/bulk-delete")
async def inventory_bulk_delete(request: Request):
    return await _bulk_delete_impl(request, redirect_to="/inventory")


async def _bulk_delete_impl(request: Request, redirect_to: str):
    _user(request)
    form = await request.form()
    ids = [int(x) for x in form.get("ids", "").split(",") if x.strip()]
    deleted = 0
    with Session(engine, expire_on_commit=False) as s:
        for pid in ids:
            item = s.get(Item, pid)
            if item and item.is_active:
                item.is_active = False
                s.add(item)
                if item.equipment_id:
                    eq = s.get(Equipment, item.equipment_id)
                    if eq:
                        eq.is_active = False
                        s.add(eq)
                deleted += 1
        s.commit()
    return RedirectResponse(f"{redirect_to}?deleted={deleted}", status_code=303)


@router.post("/inventory/bulk-update")
async def inventory_bulk_update(request: Request):
    """재고/장비 목록에서의 일괄 수정 (products_bulk_update와 동일 동작)"""
    return await products_bulk_update(request, redirect_to="/inventory")


@router.post("/products/bulk-update")
async def products_bulk_update(request: Request, redirect_to: str = "/products"):
    """물품(품목) 목록에서 선택한 항목들을 일괄 수정.

    field 종류:
    - consumer_price, rental_daily, rental_deposit : Item 가격 (숫자)
    - category : Item + Equipment 카테고리 (텍스트)
    - manufacturer / model / unit / memo : Item 텍스트 필드
    - propagate_to_units=yes 시 소속 EquipmentUnit에도 함께 적용
      (가격→rental_price_daily / 카테고리→해당 없음 / 기타 매핑은 아래 PROPAGATE_MAP)
    """
    _user(request)
    form = await request.form()
    ids = [int(x) for x in form.get("ids", "").split(",") if x.strip()]
    if not ids:
        raise HTTPException(400, "선택된 물품이 없습니다.")
    field = (form.get("field") or "").strip()
    value = (form.get("value") or "").strip()
    propagate = (form.get("propagate_to_units") == "yes")

    # Item 측 허용 필드
    ITEM_INT_FIELDS = {"consumer_price", "rental_daily", "rental_deposit"}
    ITEM_TEXT_FIELDS = {"manufacturer", "model", "unit", "memo", "category"}
    if field not in (ITEM_INT_FIELDS | ITEM_TEXT_FIELDS):
        raise HTTPException(400, f"허용되지 않은 필드: {field}")

    # 값 정규화
    if field in ITEM_INT_FIELDS:
        new_value_int = _to_int(value, 0)
        new_value = new_value_int
    else:
        new_value = value

    # Item → EquipmentUnit 전파 매핑 (개체에도 같이 반영할 필드들)
    # rental_daily는 EquipmentUnit.rental_price_daily에 대응
    PROPAGATE_MAP = {
        "rental_daily": "rental_price_daily",
    }

    updated = 0
    units_updated = 0
    with Session(engine, expire_on_commit=False) as s:
        for pid in ids:
            item = s.get(Item, pid)
            if not item:
                continue
            setattr(item, field, new_value)
            s.add(item)
            updated += 1

            # Equipment 함께 (category 변경 시 메타에도 반영)
            if field == "category" and item.equipment_id:
                eq = s.get(Equipment, item.equipment_id)
                if eq:
                    eq.category = new_value
                    s.add(eq)

            # 재고(개체)에도 전파
            if propagate and item.equipment_id:
                target_unit_field = PROPAGATE_MAP.get(field)
                if target_unit_field:
                    units = s.exec(select(EquipmentUnit).where(
                        EquipmentUnit.equipment_id == item.equipment_id
                    )).all()
                    for u in units:
                        setattr(u, target_unit_field, new_value)
                        s.add(u)
                        units_updated += 1
        s.commit()
    return RedirectResponse(
        f"{redirect_to}?bulk_updated={updated}&units_updated={units_updated}&bulk_field={field}",
        status_code=303,
    )


# ============================================================
# 재고 관리 활성화 (Item만 있고 Equipment 없는 경우)
# ============================================================
@router.post("/products/{pid}/enable-inventory")
def product_enable_inventory(request: Request, pid: int,
                              category: str = Form("기타"),
                              initial_units: str = Form("0")):
    """가격만 있는 물품에 재고 관리를 추가."""
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(Item, pid)
        if not item:
            raise HTTPException(404)
        if not item.equipment_id:
            eq = Equipment(
                name=item.name, spec=item.spec or "",
                category=category, memo=item.memo or "",
            )
            s.add(eq)
            s.commit()
            s.refresh(eq)
            item.equipment_id = eq.id
            s.add(item)
            s.commit()
            # 초기 개체 생성 — 카운터 직접 증가
            n = _to_int(initial_units)
            if n > 0:
                prefixes = get_equipment_prefixes()
                prefix = prefixes.get(category, prefixes.get("기타", "EQ"))
                year = date.today().year
                pattern = f"{prefix}-{year}-"
                existing_units = s.exec(select(EquipmentUnit).where(
                    EquipmentUnit.asset_code.like(f"{pattern}%")
                )).all()
                nums = []
                for u in existing_units:
                    tail = u.asset_code.replace(pattern, "", 1)
                    if tail.isdigit():
                        nums.append(int(tail))
                next_num = (max(nums) + 1) if nums else 1
                for _ in range(n):
                    s.add(EquipmentUnit(
                        equipment_id=eq.id,
                        asset_code=f"{prefix}-{year}-{next_num:04d}",
                        rental_price_daily=item.rental_daily or 0,
                        status="보유중",
                    ))
                    next_num += 1
            s.commit()
    return RedirectResponse(f"/products/{pid}", status_code=303)


# ============================================================
# 개체 (Unit) CRUD - 물품 상세 페이지 내부 동작
# ============================================================
@router.post("/products/{pid}/units/new")
def product_unit_create(
    request: Request, pid: int,
    asset_code: str = Form(""),
    serial_number: str = Form(""),
    location: str = Form(""),
    manager: str = Form(""),
    purchase_date: str = Form(""),
    purchase_price: str = Form("0"),
    memo: str = Form(""),
):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(Item, pid)
        if not item:
            raise HTTPException(404)
        eq = s.get(Equipment, item.equipment_id) if item.equipment_id else None
        if not eq:
            raise HTTPException(400, "재고 관리가 비활성화된 물품입니다.")
        category = eq.category or "기타"
        asset_code = (asset_code or "").strip() or _next_asset_code(category)
        # 중복 회피
        while s.exec(select(EquipmentUnit).where(EquipmentUnit.asset_code == asset_code)).first():
            asset_code = _next_asset_code(category)
        unit_obj = EquipmentUnit(
            equipment_id=eq.id, asset_code=asset_code,
            serial_number=serial_number, location=location, manager=manager,
            purchase_date=datetime.strptime(purchase_date, "%Y-%m-%d").date() if purchase_date else None,
            purchase_price=_to_int(purchase_price),
            rental_price_daily=item.rental_daily or 0,
            status="보유중", memo=memo,
        )
        s.add(unit_obj)
        s.commit()
    return RedirectResponse(f"/products/{pid}#units", status_code=303)


@router.post("/products/{pid}/units/{uid}/edit")
def product_unit_update(
    request: Request, pid: int, uid: int,
    asset_code: str = Form(...),
    serial_number: str = Form(""),
    location: str = Form(""),
    manager: str = Form(""),
    status: str = Form("보유중"),
    purchase_date: str = Form(""),
    purchase_price: str = Form("0"),
    last_check_date: str = Form(""),
    memo: str = Form(""),
):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(EquipmentUnit, uid)
        if not u:
            raise HTTPException(404)
        u.asset_code = asset_code.strip()
        u.serial_number = serial_number
        u.location = location; u.manager = manager
        u.status = status
        u.purchase_date = datetime.strptime(purchase_date, "%Y-%m-%d").date() if purchase_date else None
        u.purchase_price = _to_int(purchase_price)
        u.last_check_date = datetime.strptime(last_check_date, "%Y-%m-%d").date() if last_check_date else None
        u.memo = memo
        s.add(u)
        s.commit()
    return RedirectResponse(f"/products/{pid}#units", status_code=303)


@router.post("/products/{pid}/units/{uid}/delete")
def product_unit_delete(request: Request, pid: int, uid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(EquipmentUnit, uid)
        if u:
            s.delete(u)
            s.commit()
    return RedirectResponse(f"/products/{pid}#units", status_code=303)


# ============================================================
# 호환 리다이렉트 (기존 /items, /equipment 접근 → /products)
# ============================================================
@router.get("/items", include_in_schema=False)
def items_redirect():
    return RedirectResponse("/products", status_code=301)


@router.get("/items/new", include_in_schema=False)
def items_new_redirect():
    return RedirectResponse("/products/new", status_code=301)


@router.get("/equipment", include_in_schema=False)
def equipment_redirect():
    return RedirectResponse("/products", status_code=301)


@router.get("/equipment/new", include_in_schema=False)
def equipment_new_redirect():
    return RedirectResponse("/products/new", status_code=301)
