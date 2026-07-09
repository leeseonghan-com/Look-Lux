"""장비 관리 — 마스터/개체/유지보수/대여이력 + QR 라벨 인쇄"""
import io
import base64
from datetime import date, datetime
from pathlib import Path
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, select

from database import (
    engine, Equipment, EquipmentUnit, MaintenanceLog, RentalLog,
    Project, User, Item, get_equipment_prefixes, DEFAULT_EQUIPMENT_PREFIXES,
)
from template_utils import templates

router = APIRouter()


def _to_int(v, default=0) -> int:
    """콤마 포함 가능한 금액 안전 파싱"""
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


def _sync_item_with_equipment(session, eq: Equipment,
                              consumer_price: int = 0,
                              rental_daily: int = 0,
                              rental_deposit: int = 0,
                              unit: str = "EA"):
    """장비 종류 ↔ 단가표 자동 동기화.
    - eq.id 로 연결된 Item이 있으면 가격만 업데이트
    - 없으면 새 Item 자동 생성
    - 가격이 0인 항목은 기존 값 유지 (덮어쓰지 않음)
    """
    existing = session.exec(select(Item).where(Item.equipment_id == eq.id)).first()
    if existing:
        # 기존 단가표 항목 — 가격은 입력값이 있으면 덮어쓰기
        if consumer_price > 0:
            existing.consumer_price = consumer_price
        if rental_daily > 0:
            existing.rental_daily = rental_daily
        if rental_deposit > 0:
            existing.rental_deposit = rental_deposit
        # 이름·스펙은 항상 장비 정보로 동기화
        existing.name = eq.name
        existing.spec = eq.spec or eq.model or ""
        existing.unit = unit or existing.unit
        # category 자동 분류
        has_sale = (existing.consumer_price or 0) > 0
        has_rent = (existing.rental_daily or 0) > 0
        existing.category = "겸용" if (has_sale and has_rent) else ("렌탈" if has_rent else ("납품" if has_sale else "겸용"))
        session.add(existing)
        return existing
    else:
        # 새로 생성 — 단가표 코드 자동 할당
        existing_codes = {it.code for it in session.exec(select(Item)).all()}
        next_num = 1
        while True:
            candidate = f"I-{next_num:03d}"
            if candidate not in existing_codes:
                break
            next_num += 1
        has_sale = consumer_price > 0
        has_rent = rental_daily > 0
        category = "겸용" if (has_sale and has_rent) else ("렌탈" if has_rent else ("납품" if has_sale else "겸용"))
        new_item = Item(
            code=candidate,
            name=eq.name,
            spec=eq.spec or eq.model or "",
            unit=unit or "EA",
            consumer_price=consumer_price,
            rental_daily=rental_daily,
            rental_deposit=rental_deposit,
            category=category,
            equipment_id=eq.id,
            memo=f"장비 종류 자동 연동 ({eq.category})",
        )
        session.add(new_item)
        return new_item


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        return s.get(User, uid)


def _next_asset_code(category: str = "기타") -> str:
    """카테고리별 관리코드 자동 생성.
    예: 음향 → SC-2026-0001, 조명 → LB-2026-0001
    설정에서 prefix를 변경하면 그 값이 반영됨.
    """
    prefixes = get_equipment_prefixes()
    prefix = prefixes.get(category, prefixes.get("기타", "EQ"))
    year = date.today().year
    pattern = f"{prefix}-{year}-"
    with Session(engine, expire_on_commit=False) as s:
        units = s.exec(select(EquipmentUnit).where(EquipmentUnit.asset_code.like(f"{pattern}%"))).all()
        nums = []
        for u in units:
            try:
                # 패턴: PREFIX-YYYY-NNNN
                tail = u.asset_code.replace(pattern, "", 1)
                if tail.isdigit():
                    nums.append(int(tail))
            except (ValueError, IndexError):
                pass
        next_num = (max(nums) + 1) if nums else 1
        return f"{prefix}-{year}-{next_num:04d}"


def _next_code_by_pattern(last_code: str) -> str:
    """기존 코드의 패턴을 분석해서 다음 번호 자동 추론.
    예: 'MY-CUSTOM-001' → 'MY-CUSTOM-002'
        'LED-A-1' → 'LED-A-2'
        'P12' → 'P13'
    숫자가 없으면 기본 자동 코드 사용.
    이미 사용 중이면 다음 번호로 계속 증가.
    """
    import re
    if not last_code or not last_code.strip():
        return _next_asset_code()

    last_code = last_code.strip()
    # 코드 끝의 숫자(0패딩 포함) 찾기
    m = re.match(r'^(.*?)(\d+)([^\d]*)$', last_code)
    if not m:
        # 숫자가 없는 코드 (예: "ABC") → 기본 자동 코드로 폴백
        return _next_asset_code()

    prefix, num_str, suffix = m.group(1), m.group(2), m.group(3)
    width = len(num_str)  # 자릿수 보존 (예: 001 → 002)

    with Session(engine, expire_on_commit=False) as s:
        # 같은 prefix 시작 + 같은 suffix 끝나는 코드들 중 가장 큰 번호 찾기
        # SQL LIKE 패턴: prefix% 로 찾고, Python에서 끝 검증
        all_units = s.exec(select(EquipmentUnit)).all()
        related_nums = []
        for u in all_units:
            code = u.asset_code or ""
            if not code.startswith(prefix) or not code.endswith(suffix):
                continue
            # prefix 와 suffix 사이가 숫자여야 함
            middle = code[len(prefix):len(code) - len(suffix)] if suffix else code[len(prefix):]
            if middle.isdigit():
                related_nums.append(int(middle))

        if not related_nums:
            # 패턴 매칭되는 것 없으면 last_code 자체의 번호 + 1
            related_nums = [int(num_str)]

        next_num = max(related_nums) + 1
        # 자릿수 패딩 유지
        return f"{prefix}{str(next_num).zfill(width)}{suffix}"


# ============================================================
# 장비 마스터 CRUD
# ============================================================

@router.post("/equipment/sync-all-items")
def equipment_sync_all_items(request: Request):
    """기존 등록된 모든 보유장비를 단가표에 일괄 동기화.
    - 단가표에 없으면 새로 생성 (가격은 0으로 시작 — 사용자가 채워야 함)
    - 이미 연결된 항목은 이름/규격만 동기화 (가격은 건드리지 않음)
    - referer에 따라 단가표 또는 보유장비 페이지로 복귀
    """
    _user(request)
    created = 0
    updated = 0
    with Session(engine, expire_on_commit=False) as s:
        all_eqs = s.exec(select(Equipment).where(Equipment.is_active == True)).all()
        for eq in all_eqs:
            existing = s.exec(select(Item).where(Item.equipment_id == eq.id)).first()
            if existing:
                # 이름/규격만 동기화 (가격은 사용자 입력 존중)
                changed = False
                if existing.name != eq.name:
                    existing.name = eq.name
                    changed = True
                new_spec = eq.spec or eq.model or ""
                if existing.spec != new_spec:
                    existing.spec = new_spec
                    changed = True
                if changed:
                    s.add(existing)
                    updated += 1
            else:
                # 새 Item 생성 (가격 0 — 사용자가 단가표에서 입력)
                existing_codes = {it.code for it in s.exec(select(Item)).all()}
                next_num = 1
                while True:
                    candidate = f"I-{next_num:03d}"
                    if candidate not in existing_codes:
                        break
                    next_num += 1
                new_item = Item(
                    code=candidate,
                    name=eq.name,
                    spec=eq.spec or eq.model or "",
                    unit="EA",
                    consumer_price=0,
                    rental_daily=0,
                    rental_deposit=0,
                    category="겸용",
                    equipment_id=eq.id,
                    memo=f"기존 보유장비 자동 연동 ({eq.category}) — 가격을 입력해주세요",
                )
                s.add(new_item)
                created += 1
        s.commit()
    # referer 기반 리다이렉트 (단가표에서 눌렀으면 단가표로, 아니면 보유장비로)
    referer = request.headers.get("referer", "")
    if "/items" in referer:
        target = f"/items?synced=1&created={created}&updated={updated}"
    else:
        target = f"/equipment?synced=1&created={created}&updated={updated}"
    return RedirectResponse(target, status_code=303)


@router.get("/equipment", response_class=HTMLResponse)
def equipment_list(request: Request, category: str = "all"):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        q = select(Equipment).where(Equipment.is_active == True)
        if category != "all":
            q = q.where(Equipment.category == category)
        eqs = s.exec(q).all()
        # 카테고리별 카운터 (필터 칩에 표시)
        all_eqs = s.exec(select(Equipment).where(Equipment.is_active == True)).all()
        cat_counts = {"all": len(all_eqs)}
        for c in ["조명", "음향", "영상", "구조물", "전원", "기타"]:
            cat_counts[c] = sum(1 for e in all_eqs if e.category == c)

        # 연동된 단가표 정보 미리 조회
        all_items = s.exec(select(Item)).all()
        item_by_eq = {it.equipment_id: it for it in all_items if it.equipment_id}

        rows = []
        for eq in eqs:
            units = s.exec(select(EquipmentUnit).where(EquipmentUnit.equipment_id == eq.id)).all()
            total = len(units)
            available = sum(1 for u in units if u.status == "보유중")
            on_rent = sum(1 for u in units if u.status == "대여중")
            in_repair = sum(1 for u in units if u.status == "수리중")
            linked = item_by_eq.get(eq.id)
            rows.append({
                "id": eq.id, "name": eq.name, "model": eq.model,
                "manufacturer": eq.manufacturer, "category": eq.category,
                "spec": eq.spec, "memo": eq.memo,
                "total": total, "available": available,
                "on_rent": on_rent, "in_repair": in_repair,
                # 단가표 연동 상태
                "linked_item_id": linked.id if linked else None,
                "linked_item_code": linked.code if linked else "",
                "consumer_price": linked.consumer_price if linked else 0,
                "rental_daily": linked.rental_daily if linked else 0,
            })
    return templates.TemplateResponse(request, "equipment_list.html", {
        "user": user, "rows": rows, "category": category, "cat_counts": cat_counts,
    })


@router.get("/equipment/new", response_class=HTMLResponse)
def equipment_new(request: Request):
    user = _user(request)
    return templates.TemplateResponse(request, "equipment_new.html", {"user": user})


@router.post("/equipment/new")
async def equipment_create(request: Request):
    """장비 종류 등록 — 옵션으로 첫 번째 개체(재고)도 동시 등록 가능"""
    _user(request)
    form = await request.form()

    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "장비명은 필수입니다.")

    category = form.get("category", "조명") or "조명"
    if category not in ['조명', '음향', '영상', '구조물', '전원', '특수효과', '기타']:
        category = "기타"

    with Session(engine, expire_on_commit=False) as s:
        eq = Equipment(
            name=name,
            model=(form.get("model") or "").strip(),
            manufacturer=(form.get("manufacturer") or "").strip(),
            category=category,
            spec=(form.get("spec") or "").strip(),
            memo=(form.get("memo") or "").strip(),
        )
        s.add(eq)
        s.commit()
        s.refresh(eq)
        eqid = eq.id

        # 단가표 자동 동기화
        _sync_item_with_equipment(
            s, eq,
            consumer_price=_to_int(form.get("consumer_price", "0")),
            rental_daily=_to_int(form.get("rental_daily", "0")),
            rental_deposit=_to_int(form.get("rental_deposit", "0")),
            unit=form.get("unit", "EA") or "EA",
        )
        s.commit()

        # ───── 옵션: 첫 개체(재고) 동시 등록 ─────
        if form.get("create_unit") == "yes":
            asset_code = (form.get("unit_asset_code") or "").strip()
            if not asset_code:
                asset_code = _next_asset_code(category)
            # 중복 회피 — 비어있지 않은데 중복이면 자동 다음 코드
            existing = s.exec(select(EquipmentUnit).where(EquipmentUnit.asset_code == asset_code)).first()
            if existing:
                asset_code = _next_asset_code(category)
            purchase_date_str = (form.get("unit_purchase_date") or "").strip()
            try:
                pdate = datetime.strptime(purchase_date_str, "%Y-%m-%d").date() if purchase_date_str else None
            except (ValueError, TypeError):
                pdate = None
            u = EquipmentUnit(
                equipment_id=eqid,
                asset_code=asset_code,
                serial_number=(form.get("unit_serial_number") or "").strip(),
                department=(form.get("unit_department") or "").strip(),
                manager=(form.get("unit_manager") or "").strip(),
                location=(form.get("unit_location") or "").strip(),
                status=(form.get("unit_status") or "보유중").strip(),
                purchase_vendor=(form.get("unit_purchase_vendor") or "").strip(),
                purchase_date=pdate,
                purchase_price=_to_int(form.get("unit_purchase_price", "0")),
                memo=(form.get("unit_memo") or "").strip(),
            )
            s.add(u)
            s.commit()

    return RedirectResponse(f"/equipment/{eqid}", status_code=303)


@router.get("/equipment/labels", response_class=HTMLResponse)
def labels_view_early(request: Request, ids: str = ""):
    """QR 라벨 인쇄 페이지 — /equipment/{eqid} 보다 먼저 매칭되도록 위치"""
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        if ids:
            id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
            units = [s.get(EquipmentUnit, i) for i in id_list]
            units = [u for u in units if u]
        else:
            units = s.exec(select(EquipmentUnit).order_by(EquipmentUnit.asset_code)).all()

        rows = []
        for u in units:
            eq = s.get(Equipment, u.equipment_id)
            rows.append({
                "id": u.id,
                "asset_code": u.asset_code,
                "name": eq.name if eq else "?",
                "model": eq.model if eq else "",
                "manufacturer": eq.manufacturer if eq else "",
                "category": eq.category if eq else "",
                "serial_number": u.serial_number,
                "purchase_vendor": u.purchase_vendor,
                "purchase_date": u.purchase_date,
                "purchase_price": u.purchase_price,
                "location": u.location,
                "department": u.department,
                "manager": u.manager,
                "status": u.status,
                "qr_data": u.asset_code,
            })
    return templates.TemplateResponse(request, "equipment_labels.html", {
        "rows": rows,
    })


@router.get("/equipment/{eqid}", response_class=HTMLResponse)
def equipment_detail(request: Request, eqid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        eq = s.get(Equipment, eqid)
        if not eq:
            raise HTTPException(404)
        units_raw = s.exec(select(EquipmentUnit).where(EquipmentUnit.equipment_id == eqid).order_by(EquipmentUnit.asset_code)).all()
        units = [{
            "id": u.id, "asset_code": u.asset_code, "serial_number": u.serial_number,
            "purchase_vendor": u.purchase_vendor, "purchase_date": u.purchase_date,
            "purchase_price": u.purchase_price, "rental_price_daily": u.rental_price_daily,
            "status": u.status, "location": u.location,
            "department": u.department, "manager": u.manager,
            "last_check_date": u.last_check_date, "memo": u.memo,
        } for u in units_raw]
        eq_dict = {
            "id": eq.id, "name": eq.name, "model": eq.model,
            "manufacturer": eq.manufacturer, "category": eq.category,
            "spec": eq.spec, "memo": eq.memo,
        }
        # 합계
        total_purchase = sum(u["purchase_price"] for u in units)
        available = sum(1 for u in units if u["status"] == "보유중")
        on_rent = sum(1 for u in units if u["status"] == "대여중")
        in_repair = sum(1 for u in units if u["status"] == "수리중")

        # 다음 개체 추가 폼의 기본값 — 가장 최근 개체 정보를 복사
        # (관리코드는 카테고리별 prefix + 다음번호 자동 생성)
        defaults = {
            "asset_code": _next_asset_code(eq.category),
            "serial_number": "",
            "department": "", "manager": "",
            "purchase_vendor": "", "purchase_date": "",
            "purchase_price": 0, "rental_price_daily": 0, "location": "",
            "memo": "",
        }
        if units_raw:
            # 가장 최근 등록된 개체
            # units_raw는 asset_code 순으로 정렬돼 있으니, 등록 순서로 다시 정렬
            latest = max(units_raw, key=lambda u: (u.id or 0))
            defaults.update({
                "asset_code": _next_code_by_pattern(latest.asset_code),
                "department": latest.department or "",
                "manager": latest.manager or "",
                "purchase_vendor": latest.purchase_vendor or "",
                "purchase_date": str(latest.purchase_date) if latest.purchase_date else "",
                "purchase_price": latest.purchase_price or 0,
                "rental_price_daily": latest.rental_price_daily or 0,
                "location": latest.location or "",
            })

    return templates.TemplateResponse(request, "equipment_detail.html", {
        "user": user, "eq": eq_dict, "units": units,
        "total_purchase": total_purchase,
        "available": available, "on_rent": on_rent, "in_repair": in_repair,
        "defaults": defaults,
    })


@router.get("/equipment/{eqid}/edit", response_class=HTMLResponse)
def equipment_edit(request: Request, eqid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        eq = s.get(Equipment, eqid)
        if not eq:
            raise HTTPException(404)
        # 연결된 단가표 정보 조회 (있으면 현재 가격을 폼에 미리 채움)
        linked_item = s.exec(select(Item).where(Item.equipment_id == eqid)).first()
        eq_dict = {
            "id": eq.id, "name": eq.name, "model": eq.model,
            "manufacturer": eq.manufacturer, "category": eq.category,
            "spec": eq.spec, "memo": eq.memo,
            # 단가표 연동 가격 (편집 폼 기본값으로)
            "consumer_price": linked_item.consumer_price if linked_item else 0,
            "rental_daily": linked_item.rental_daily if linked_item else 0,
            "rental_deposit": linked_item.rental_deposit if linked_item else 0,
            "unit": linked_item.unit if linked_item else "EA",
            "linked_item_code": linked_item.code if linked_item else "",
            "linked_item_id": linked_item.id if linked_item else None,
        }
    return templates.TemplateResponse(request, "equipment_edit.html", {"user": user, "eq": eq_dict})


@router.post("/equipment/{eqid}/edit")
def equipment_update(
    request: Request, eqid: int,
    name: str = Form(...), model: str = Form(""),
    manufacturer: str = Form(""), category: str = Form("조명"),
    spec: str = Form(""), memo: str = Form(""),
    # 단가표 자동 연동용 가격 (선택사항)
    consumer_price: str = Form("0"),
    rental_daily: str = Form("0"),
    rental_deposit: str = Form("0"),
    unit: str = Form("EA"),
):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        eq = s.get(Equipment, eqid)
        if not eq:
            raise HTTPException(404)
        eq.name = name; eq.model = model; eq.manufacturer = manufacturer
        eq.category = category; eq.spec = spec; eq.memo = memo
        s.add(eq)
        s.commit()
        # 단가표 동기화 (기존 연결 항목이 있으면 가격만 업데이트)
        _sync_item_with_equipment(
            s, eq,
            consumer_price=_to_int(consumer_price),
            rental_daily=_to_int(rental_daily),
            rental_deposit=_to_int(rental_deposit),
            unit=unit,
        )
        s.commit()
    return RedirectResponse(f"/equipment/{eqid}", status_code=303)


@router.post("/equipment/{eqid}/delete")
def equipment_delete(request: Request, eqid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        eq = s.get(Equipment, eqid)
        if eq:
            eq.is_active = False
            s.add(eq)
            s.commit()
    return RedirectResponse("/equipment", status_code=303)


# ============================================================
# 장비 개체 (Unit) — 같은 종류 여러 대
# ============================================================

@router.post("/equipment/{eqid}/units/new")
def unit_create(
    request: Request, eqid: int,
    asset_code: str = Form(""),  # 비워두면 자동 생성
    serial_number: str = Form(""), purchase_vendor: str = Form(""),
    purchase_date: str = Form(""), purchase_price: int = Form(0),
    rental_price_daily: int = Form(0), location: str = Form(""),
    department: str = Form(""), manager: str = Form(""),
    memo: str = Form(""),
):
    user = _user(request)
    asset_code = (asset_code or "").strip()
    with Session(engine, expire_on_commit=False) as s:
        # Equipment 조회하여 카테고리 확인
        eq = s.get(Equipment, eqid)
        eq_category = eq.category if eq else "기타"
        if not asset_code:
            asset_code = _next_asset_code(eq_category)
        # 중복 검사
        existing = s.exec(select(EquipmentUnit).where(EquipmentUnit.asset_code == asset_code)).first()
        if existing:
            next_code = _next_asset_code(eq_category)
            return templates.TemplateResponse(request, "error.html", {
                "user": user, "title": "관리코드 중복",
                "message": f"코드 '{asset_code}' 는 이미 사용 중입니다.\n비워두면 자동 생성됩니다. 추천 코드: {next_code}",
                "back_url": f"/equipment/{eqid}",
            }, status_code=400)
        u = EquipmentUnit(
            equipment_id=eqid, asset_code=asset_code,
            serial_number=serial_number, purchase_vendor=purchase_vendor,
            purchase_date=datetime.strptime(purchase_date, "%Y-%m-%d").date() if purchase_date else None,
            purchase_price=purchase_price, rental_price_daily=rental_price_daily,
            location=location, department=department, manager=manager, memo=memo,
        )
        s.add(u)
        s.commit()
    return RedirectResponse(f"/equipment/{eqid}", status_code=303)


@router.get("/equipment/unit/{uid}", response_class=HTMLResponse)
def unit_detail(request: Request, uid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(EquipmentUnit, uid)
        if not u:
            raise HTTPException(404)
        eq = s.get(Equipment, u.equipment_id)
        maintenance_raw = s.exec(
            select(MaintenanceLog).where(MaintenanceLog.unit_id == uid).order_by(MaintenanceLog.log_date.desc())
        ).all()
        rentals_raw = s.exec(
            select(RentalLog).where(RentalLog.unit_id == uid).order_by(RentalLog.out_date.desc())
        ).all()
        maintenance = [{
            "id": m.id, "date": m.log_date, "type": m.log_type, "title": m.title,
            "description": m.description, "vendor": m.vendor, "cost": m.cost,
            "status_after": m.status_after,
        } for m in maintenance_raw]
        rentals = []
        for r in rentals_raw:
            p = s.get(Project, r.project_id) if r.project_id else None
            rentals.append({
                "id": r.id, "out_date": r.out_date, "in_date": r.in_date,
                "destination": r.destination, "note": r.note,
                "project_name": p.name if p else "",
                "project_id": r.project_id,
            })
        u_dict = {
            "id": u.id, "equipment_id": u.equipment_id, "asset_code": u.asset_code,
            "serial_number": u.serial_number, "purchase_vendor": u.purchase_vendor,
            "purchase_date": u.purchase_date, "purchase_price": u.purchase_price,
            "rental_price_daily": u.rental_price_daily, "status": u.status,
            "location": u.location, "department": u.department, "manager": u.manager,
            "last_check_date": u.last_check_date, "memo": u.memo,
        }
        eq_dict = {"id": eq.id, "name": eq.name, "model": eq.model, "manufacturer": eq.manufacturer} if eq else None
        # 프로젝트 목록 (대여 등록용)
        projects = s.exec(select(Project)).all()
        projects_data = [{"id": p.id, "name": p.name} for p in projects]
        total_maintenance_cost = sum(m["cost"] for m in maintenance)
    return templates.TemplateResponse(request, "equipment_unit_detail.html", {
        "user": user, "u": u_dict, "eq": eq_dict,
        "maintenance": maintenance, "rentals": rentals,
        "projects": projects_data,
        "total_maintenance_cost": total_maintenance_cost,
    })


@router.post("/equipment/unit/{uid}/edit")
def unit_update(
    request: Request, uid: int,
    asset_code: str = Form(...), serial_number: str = Form(""),
    purchase_vendor: str = Form(""), purchase_date: str = Form(""),
    purchase_price: int = Form(0), rental_price_daily: int = Form(0),
    status: str = Form("보유중"), location: str = Form(""),
    department: str = Form(""), manager: str = Form(""),
    last_check_date: str = Form(""), memo: str = Form(""),
):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(EquipmentUnit, uid)
        if not u:
            raise HTTPException(404)
        if asset_code != u.asset_code:
            existing = s.exec(select(EquipmentUnit).where(EquipmentUnit.asset_code == asset_code)).first()
            if existing:
                return templates.TemplateResponse(request, "error.html", {
                    "user": user, "title": "관리코드 중복",
                    "message": f"코드 '{asset_code}' 는 이미 사용 중입니다.",
                    "back_url": f"/equipment/unit/{uid}",
                }, status_code=400)
            u.asset_code = asset_code
        u.serial_number = serial_number
        u.purchase_vendor = purchase_vendor
        u.purchase_date = datetime.strptime(purchase_date, "%Y-%m-%d").date() if purchase_date else None
        u.purchase_price = purchase_price
        u.rental_price_daily = rental_price_daily
        u.status = status
        u.location = location
        u.department = department
        u.manager = manager
        u.last_check_date = datetime.strptime(last_check_date, "%Y-%m-%d").date() if last_check_date else None
        u.memo = memo
        s.add(u)
        s.commit()
    return RedirectResponse(f"/equipment/unit/{uid}", status_code=303)


@router.post("/equipment/units/bulk-update")
def units_bulk_update(
    request: Request,
    unit_ids: str = Form(...),  # "1,2,3"
    field: str = Form(...),     # 어떤 필드를 변경할지
    value: str = Form(""),      # 새 값
):
    """선택된 개체들의 필드를 일괄 수정 (텍스트·숫자·날짜·상태 지원)"""
    _user(request)
    try:
        ids = [int(x) for x in unit_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "잘못된 ID 형식")

    if not ids:
        raise HTTPException(400, "선택된 개체가 없습니다.")

    # 허용 필드 + 각 필드의 값 변환 함수
    TEXT_FIELDS = {"status", "location", "department", "manager",
                   "purchase_vendor", "memo", "serial_number"}
    INT_FIELDS = {"purchase_price", "rental_price_daily"}
    DATE_FIELDS = {"purchase_date", "last_check_date"}

    ALLOWED_FIELDS = TEXT_FIELDS | INT_FIELDS | DATE_FIELDS

    if field not in ALLOWED_FIELDS:
        raise HTTPException(400, f"허용되지 않은 필드: {field}")

    # 상태값 검증
    if field == "status":
        ALLOWED_STATUS = {"보유중", "대여중", "수리중", "폐기"}
        if value not in ALLOWED_STATUS:
            raise HTTPException(400, f"허용되지 않은 상태값: {value}")

    # 값 변환
    if field in INT_FIELDS:
        try:
            new_value = _to_int(value, 0)
        except Exception:
            raise HTTPException(400, f"숫자 형식 오류: {value}")
    elif field in DATE_FIELDS:
        v = (value or "").strip()
        if not v:
            new_value = None
        else:
            try:
                new_value = datetime.strptime(v, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                raise HTTPException(400, f"날짜 형식 오류 (YYYY-MM-DD): {value}")
    else:  # TEXT_FIELDS
        new_value = (value or "").strip()

    eqid_for_redirect = None
    updated = 0
    with Session(engine, expire_on_commit=False) as s:
        for uid in ids:
            u = s.get(EquipmentUnit, uid)
            if u:
                setattr(u, field, new_value)
                s.add(u)
                updated += 1
                if eqid_for_redirect is None:
                    eqid_for_redirect = u.equipment_id
        s.commit()

    if eqid_for_redirect:
        return RedirectResponse(
            f"/equipment/{eqid_for_redirect}?bulk_updated={updated}&bulk_field={field}",
            status_code=303,
        )
    return RedirectResponse("/equipment", status_code=303)


@router.post("/equipment/unit/{uid}/delete")
def unit_delete(request: Request, uid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(EquipmentUnit, uid)
        eqid = u.equipment_id if u else None
        if u:
            # 관련 이력도 삭제
            for m in s.exec(select(MaintenanceLog).where(MaintenanceLog.unit_id == uid)).all():
                s.delete(m)
            for r in s.exec(select(RentalLog).where(RentalLog.unit_id == uid)).all():
                s.delete(r)
            s.delete(u)
            s.commit()
    return RedirectResponse(f"/equipment/{eqid}" if eqid else "/equipment", status_code=303)


# ============================================================
# 유지보수 이력
# ============================================================

@router.post("/equipment/unit/{uid}/maintenance/new")
def maintenance_create(
    request: Request, uid: int,
    log_date: str = Form(...), log_type: str = Form("점검"),
    title: str = Form(...), description: str = Form(""),
    vendor: str = Form(""), cost: int = Form(0),
    status_after: str = Form(""),
):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        m = MaintenanceLog(
            unit_id=uid,
            log_date=datetime.strptime(log_date, "%Y-%m-%d").date(),
            log_type=log_type, title=title, description=description,
            vendor=vendor, cost=cost, status_after=status_after,
            registered_by=user.id,
        )
        s.add(m)
        # 자동: AS/수리 등록 시 장비 상태 업데이트
        u = s.get(EquipmentUnit, uid)
        if u and status_after:
            u.status = status_after
            s.add(u)
        if u and log_type == "점검":
            u.last_check_date = m.log_date
            s.add(u)
        s.commit()
    return RedirectResponse(f"/equipment/unit/{uid}", status_code=303)


@router.post("/equipment/maintenance/{mid}/delete")
def maintenance_delete(request: Request, mid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        m = s.get(MaintenanceLog, mid)
        uid = m.unit_id if m else None
        if m:
            s.delete(m)
            s.commit()
    return RedirectResponse(f"/equipment/unit/{uid}" if uid else "/equipment", status_code=303)


# ============================================================
# 대여 이력
# ============================================================

@router.post("/equipment/unit/{uid}/rental/new")
def rental_create(
    request: Request, uid: int,
    project_id: int = Form(None), out_date: str = Form(...),
    in_date: str = Form(""), destination: str = Form(""),
    note: str = Form(""),
):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        r = RentalLog(
            unit_id=uid, project_id=project_id or None,
            out_date=datetime.strptime(out_date, "%Y-%m-%d").date(),
            in_date=datetime.strptime(in_date, "%Y-%m-%d").date() if in_date else None,
            destination=destination, note=note,
            registered_by=user.id,
        )
        s.add(r)
        u = s.get(EquipmentUnit, uid)
        if u and not r.in_date:
            u.status = "대여중"
            s.add(u)
        elif u and r.in_date:
            u.status = "보유중"
            s.add(u)
        s.commit()
    return RedirectResponse(f"/equipment/unit/{uid}", status_code=303)


@router.post("/equipment/rental/{rid}/return")
def rental_return(request: Request, rid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        r = s.get(RentalLog, rid)
        uid = r.unit_id if r else None
        if r and not r.in_date:
            r.in_date = date.today()
            s.add(r)
            u = s.get(EquipmentUnit, r.unit_id)
            if u:
                u.status = "보유중"
                s.add(u)
            s.commit()
    return RedirectResponse(f"/equipment/unit/{uid}" if uid else "/equipment", status_code=303)


@router.post("/equipment/rental/{rid}/delete")
def rental_delete(request: Request, rid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        r = s.get(RentalLog, rid)
        uid = r.unit_id if r else None
        if r:
            s.delete(r)
            s.commit()
    return RedirectResponse(f"/equipment/unit/{uid}" if uid else "/equipment", status_code=303)



