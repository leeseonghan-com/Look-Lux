"""회사 설정 관리 (로고/도장 이미지 업로드 포함)"""
import os
import uuid
from pathlib import Path
from datetime import date
from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session
from PIL import Image

from database import (
    engine, CompanySettings, User, QUOTE_THEMES, get_company_settings,
    get_equipment_prefixes, save_equipment_prefixes, DEFAULT_EQUIPMENT_PREFIXES,
)
from template_utils import templates

router = APIRouter()
COMPANY_DIR = Path(os.environ.get("DATA_DIR", "data")) / "company"


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        return s.get(User, uid)


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        cs = s.get(CompanySettings, 1)
        if not cs:
            cs = CompanySettings(id=1, company_name="(주)회사명")
            s.add(cs)
            s.commit()
            s.refresh(cs)
        sd = {
            "company_name": cs.company_name, "representative": cs.representative,
            "biz_number": cs.biz_number, "biz_type": cs.biz_type, "biz_item": cs.biz_item,
            "phone": cs.phone, "fax": cs.fax, "email": cs.email, "address": cs.address,
            "bank_account": cs.bank_account,
            "logo_image": cs.logo_image, "stamp_image": cs.stamp_image,
            "default_payment_terms": cs.default_payment_terms,
            "default_delivery_terms": cs.default_delivery_terms,
            "default_valid_days": cs.default_valid_days,
            "quote_footer_notes": cs.quote_footer_notes,
            "quote_theme": cs.quote_theme or "classic",
            "quote_color_primary": cs.quote_color_primary,
            "quote_color_accent": cs.quote_color_accent,
            "quote_title_normal": cs.quote_title_normal or "견  적  서",
            "quote_title_rental": cs.quote_title_rental or "렌 탈 견 적 서",
            "quote_greeting": cs.quote_greeting or "아래와 같이 견적합니다.",
            "quote_footer_message": cs.quote_footer_message,
            "show_spec": cs.show_spec, "show_unit": cs.show_unit, "show_memo": cs.show_memo,
            "show_bank_in_quote": cs.show_bank_in_quote,
            "show_stamp_in_sign": cs.show_stamp_in_sign,
            "show_logo": cs.show_logo,
            "quote_font_scale": cs.quote_font_scale or 100,
            "quote_layout_json": cs.quote_layout_json or "",
        }
    return templates.TemplateResponse(request, "settings.html", {"user": user, "s": sd})


async def _save_image(upload: UploadFile, max_size: int = 800) -> str:
    """이미지 저장 + 리사이즈 → 파일명 반환"""
    if not upload or not upload.filename:
        return ""
    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".png"
    fname = f"{uuid.uuid4().hex}{ext}"
    fpath = COMPANY_DIR / fname
    fpath.parent.mkdir(parents=True, exist_ok=True)
    content = await upload.read()
    fpath.write_bytes(content)
    try:
        img = Image.open(fpath)
        img.thumbnail((max_size, max_size), Image.LANCZOS)
        # PNG는 투명도 유지, JPG는 RGB
        if ext in (".jpg", ".jpeg"):
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(fpath, quality=90, optimize=True)
        else:
            img.save(fpath, optimize=True)
    except Exception:
        pass
    return fname


@router.post("/settings")
async def settings_save(
    request: Request,
    company_name: str = Form(""),
    representative: str = Form(""),
    biz_number: str = Form(""),
    biz_type: str = Form(""),
    biz_item: str = Form(""),
    phone: str = Form(""),
    fax: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    bank_account: str = Form(""),
    default_payment_terms: str = Form(""),
    default_delivery_terms: str = Form(""),
    default_valid_days: int = Form(30),
    quote_footer_notes: str = Form(""),
    logo: UploadFile = File(None),
    stamp: UploadFile = File(None),
    remove_logo: str = Form(""),
    remove_stamp: str = Form(""),
    # 견적서 디자인/양식
    quote_theme: str = Form("classic"),
    quote_color_primary: str = Form(""),
    quote_color_accent: str = Form(""),
    quote_title_normal: str = Form("견  적  서"),
    quote_title_rental: str = Form("렌 탈 견 적 서"),
    quote_greeting: str = Form("아래와 같이 견적합니다."),
    quote_footer_message: str = Form(""),
    show_spec: str = Form(""),
    show_unit: str = Form(""),
    show_memo: str = Form(""),
    show_bank_in_quote: str = Form(""),
    show_stamp_in_sign: str = Form(""),
    show_logo: str = Form(""),
    quote_font_scale: int = Form(100),
):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        cs = s.get(CompanySettings, 1)
        if not cs:
            cs = CompanySettings(id=1)
            s.add(cs)

        cs.company_name = company_name
        cs.representative = representative
        cs.biz_number = biz_number
        cs.biz_type = biz_type
        cs.biz_item = biz_item
        cs.phone = phone
        cs.fax = fax
        cs.email = email
        cs.address = address
        cs.bank_account = bank_account
        cs.default_payment_terms = default_payment_terms
        cs.default_delivery_terms = default_delivery_terms
        cs.default_valid_days = default_valid_days
        cs.quote_footer_notes = quote_footer_notes
        # 견적서 양식
        cs.quote_theme = quote_theme
        cs.quote_color_primary = quote_color_primary.strip()
        cs.quote_color_accent = quote_color_accent.strip()
        cs.quote_title_normal = quote_title_normal
        cs.quote_title_rental = quote_title_rental
        cs.quote_greeting = quote_greeting
        cs.quote_footer_message = quote_footer_message
        # 체크박스는 'on' 문자열이면 True
        cs.show_spec = bool(show_spec)
        cs.show_unit = bool(show_unit)
        cs.show_memo = bool(show_memo)
        cs.show_bank_in_quote = bool(show_bank_in_quote)
        cs.show_stamp_in_sign = bool(show_stamp_in_sign)
        cs.show_logo = bool(show_logo)
        cs.quote_font_scale = max(70, min(140, quote_font_scale))

        # 로고 처리
        if remove_logo == "yes" and cs.logo_image:
            try:
                (COMPANY_DIR / cs.logo_image).unlink(missing_ok=True)
            except Exception:
                pass
            cs.logo_image = ""
        if logo and logo.filename:
            if cs.logo_image:
                try:
                    (COMPANY_DIR / cs.logo_image).unlink(missing_ok=True)
                except Exception:
                    pass
            cs.logo_image = await _save_image(logo, max_size=800)

        # 도장 처리
        if remove_stamp == "yes" and cs.stamp_image:
            try:
                (COMPANY_DIR / cs.stamp_image).unlink(missing_ok=True)
            except Exception:
                pass
            cs.stamp_image = ""
        if stamp and stamp.filename:
            if cs.stamp_image:
                try:
                    (COMPANY_DIR / cs.stamp_image).unlink(missing_ok=True)
                except Exception:
                    pass
            cs.stamp_image = await _save_image(stamp, max_size=400)

        s.add(cs)
        s.commit()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.get("/settings/quote-designer", response_class=HTMLResponse)
def quote_designer(request: Request):
    """드래그 가능한 견적서 디자이너"""
    user = _user(request)
    cs = get_company_settings()
    return templates.TemplateResponse(request, "quote_designer.html", {
        "user": user, "c": cs,
    })


@router.post("/settings/quote-layout")
async def save_quote_layout(request: Request):
    """드래그 편집한 레이아웃 JSON 저장"""
    _user(request)
    body = await request.json()
    layout_json = body.get("layout", "")
    with Session(engine, expire_on_commit=False) as s:
        cs = s.get(CompanySettings, 1)
        if not cs:
            cs = CompanySettings(id=1)
            s.add(cs)
        cs.quote_layout_json = layout_json if isinstance(layout_json, str) else ""
        s.add(cs)
        s.commit()
    return {"ok": True}


@router.post("/settings/quote-layout-reset")
def reset_quote_layout(request: Request):
    """레이아웃 초기화"""
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        cs = s.get(CompanySettings, 1)
        if cs:
            cs.quote_layout_json = ""
            s.add(cs)
            s.commit()
    return RedirectResponse("/settings/quote-designer", status_code=303)


@router.get("/settings/preview", response_class=HTMLResponse)
def settings_preview(request: Request):
    """견적서 디자인 미리보기 — 폼 데이터를 URL 쿼리로 받아 가짜 견적서 렌더링"""
    qp = request.query_params

    # 저장된 회사 설정을 기본값으로 가져온 뒤, 폼에서 들어온 값으로 덮어쓰기
    base_company = get_company_settings()

    def gv(key, default=None):
        v = qp.get(key)
        return v if v is not None and v != "" else (default if default is not None else base_company.get(key, ""))

    theme_key = qp.get("quote_theme") or base_company.get("quote_theme") or "classic"
    theme = QUOTE_THEMES.get(theme_key, QUOTE_THEMES["classic"]).copy()
    cp = qp.get("quote_color_primary", "").strip()
    ca = qp.get("quote_color_accent", "").strip()
    if cp:
        theme["primary"] = cp
        theme["header_bg"] = cp
        theme["total_bg"] = cp
    if ca:
        theme["accent"] = ca
        theme["total_fg"] = ca

    def cb(key, default):
        # 체크박스: 쿼리에 키가 있으면 그 값, 없으면 기본값
        if key in qp:
            return qp.get(key) == "on" or qp.get(key) == "true" or qp.get(key) == "1"
        return default

    # 미리보기용 회사 정보 (사용자 입력 우선)
    company = dict(base_company)
    company.update({
        "company_name": gv("company_name") or base_company.get("company_name") or "(주)회사명",
        "representative": gv("representative") or base_company.get("representative") or "홍길동",
        "biz_number": gv("biz_number") or base_company.get("biz_number") or "000-00-00000",
        "phone": gv("phone") or base_company.get("phone") or "02-0000-0000",
        "address": gv("address") or base_company.get("address") or "회사 주소",
        "bank_account": gv("bank_account") or base_company.get("bank_account") or "",
        "logo_image": base_company.get("logo_image", ""),  # 파일은 그대로
        "stamp_image": base_company.get("stamp_image", ""),
        "quote_theme": theme_key,
        "quote_theme_data": theme,
        "quote_title_normal": gv("quote_title_normal") or "견  적  서",
        "quote_title_rental": gv("quote_title_rental") or "렌 탈 견 적 서",
        "quote_greeting": gv("quote_greeting") or "아래와 같이 견적합니다.",
        "quote_footer_message": gv("quote_footer_message") or "",
        "show_spec": cb("show_spec", True),
        "show_unit": cb("show_unit", True),
        "show_memo": cb("show_memo", False),
        "show_bank_in_quote": cb("show_bank_in_quote", True),
        "show_stamp_in_sign": cb("show_stamp_in_sign", True),
        "show_logo": cb("show_logo", True),
        "quote_font_scale": int(qp.get("quote_font_scale") or base_company.get("quote_font_scale") or 100),
    })

    # 미리보기용 샘플 견적서
    sample_q = {
        "quote_number": "Q-2026-001",
        "quote_date": date.today(),
        "quote_type": "납품",
        "rental_days": 1,
        "vendor_name": "샘플 거래처",
        "vendor_contact": "김담당",
        "vendor_phone": "010-0000-0000",
        "project_name": "행사명 예시",
        "subtotal": 5000000, "discount": 200000,
        "after_discount": 4800000, "vat": 480000, "total": 5280000,
        "valid_until": "2026-02-15",
        "payment_terms": gv("default_payment_terms") or "세금계산서 발행 후 30일 이내 입금",
        "delivery_terms": gv("default_delivery_terms") or "발주일로부터 협의 후 결정",
        "notes": gv("quote_footer_notes") or "",
    }
    sample_items = [
        {"seq": 1, "name": "LED 무빙라이트 230W", "spec": "Beam/Spot/Wash", "quantity": 4, "unit": "EA", "unit_price": 800000, "amount": 3200000, "memo": ""},
        {"seq": 2, "name": "라인어레이 스피커", "spec": "12인치 2way", "quantity": 1, "unit": "SET", "unit_price": 1500000, "amount": 1500000, "memo": ""},
        {"seq": 3, "name": "무선마이크", "spec": "UHF 듀얼", "quantity": 1, "unit": "SET", "unit_price": 300000, "amount": 300000, "memo": "행사 진행용"},
    ]
    return templates.TemplateResponse(request, "quote_print.html", {
        "q": sample_q,
        "items": sample_items,
        "preview_company": company,  # 미리보기 전용 (request.state.company 무시)
    })


# ============================================================
# 장비 관리코드 prefix 설정
# ============================================================
@router.get("/settings/equipment-prefixes", response_class=HTMLResponse)
def equipment_prefixes_view(request: Request):
    """장비 카테고리별 관리코드 prefix 설정 페이지"""
    user = _user(request)
    prefixes = get_equipment_prefixes()
    return templates.TemplateResponse(request, "equipment_prefixes.html", {
        "user": user,
        "prefixes": prefixes,
        "defaults": DEFAULT_EQUIPMENT_PREFIXES,
        "categories": list(DEFAULT_EQUIPMENT_PREFIXES.keys()),
    })


@router.post("/settings/equipment-prefixes")
async def equipment_prefixes_save(request: Request):
    """카테고리별 prefix 저장 (form 필드명: prefix_<카테고리>)"""
    _user(request)
    form = await request.form()
    new_prefixes = {}
    for cat in DEFAULT_EQUIPMENT_PREFIXES.keys():
        val = form.get(f"prefix_{cat}", "").strip().upper()
        if val:
            new_prefixes[cat] = val
        else:
            # 비우면 기본값 사용
            new_prefixes[cat] = DEFAULT_EQUIPMENT_PREFIXES[cat]
    save_equipment_prefixes(new_prefixes)
    return RedirectResponse("/settings/equipment-prefixes?saved=1", status_code=303)
