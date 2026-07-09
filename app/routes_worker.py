"""근무자 마스터 관리"""
import os
import uuid
from pathlib import Path
from fastapi import APIRouter, Request, Form, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from sqlmodel import Session, select

from database import engine, Worker, User
from template_utils import templates

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

router = APIRouter()

# 근무자 사진 저장 디렉토리
WORKER_PHOTO_DIR = Path(__file__).parent / "data" / "worker_photos"
WORKER_PHOTO_DIR.mkdir(parents=True, exist_ok=True)


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        user = s.get(User, uid)
    if user:
        from permissions import has_permission
        if not has_permission(user, "workers"):
            raise HTTPException(403, "근무자 접근 권한이 없습니다.")
    return user


def _save_photo(upload: UploadFile) -> str:
    """업로드된 사진을 저장하고 파일명을 반환. 실패 시 빈 문자열."""
    if not upload or not upload.filename:
        return ""
    ext = os.path.splitext(upload.filename)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
        ext = ".jpg"
    fname = f"{uuid.uuid4().hex}{ext}"
    fpath = WORKER_PHOTO_DIR / fname
    try:
        content = upload.file.read()
        fpath.write_bytes(content)
        # 리사이즈 (긴 변 800px)
        if PIL_OK:
            try:
                img = Image.open(fpath)
                img.thumbnail((800, 800), Image.LANCZOS)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(fpath, quality=85, optimize=True)
            except Exception:
                pass
        return fname
    except Exception:
        return ""


def _format_resident_no(raw: str) -> str:
    """주민등록번호 정규화 — 숫자만 추출 후 6-7 형태로 포맷"""
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 13:
        return f"{digits[:6]}-{digits[6:]}"
    if len(digits) == 7:
        return f"{digits[:6]}-{digits[6:]}"
    # 그 외 형식은 입력 그대로 (사용자가 부분만 입력한 경우)
    return raw.strip()


def _safe_int(v, default=0):
    try:
        return int(str(v).replace(",", "").strip() or default)
    except (ValueError, TypeError):
        return default


@router.get("/workers", response_class=HTMLResponse)
def worker_list(request: Request, q: str = "", type: str = "", status: str = ""):
    """근무자 목록 — 검색 + 알바/정직원 탭 + 재직상태 필터"""
    user = _user(request)
    search_q = (q or "").strip().lower()
    type_filter = (type or "").strip()
    status_filter = (status or "").strip()
    with Session(engine, expire_on_commit=False) as s:
        workers = s.exec(select(Worker).order_by(Worker.is_active.desc(), Worker.name)).all()
        # 타입 필터
        if type_filter in ("정직원", "알바"):
            workers = [w for w in workers if (w.employee_type or "알바") == type_filter]
        # 재직 상태 필터 (정직원만 의미 있음)
        if status_filter:
            workers = [w for w in workers if (getattr(w, "employment_status", "재직") or "재직") == status_filter]
        # 키워드 검색
        if search_q:
            workers = [w for w in workers if any(search_q in (str(x) or "").lower() for x in [
                w.name, w.phone, w.bank, w.account, w.memo,
                getattr(w, "resident_no", ""), getattr(w, "employee_type", ""),
            ])]
        rows = [{
            "id": w.id, "name": w.name, "phone": w.phone,
            "bank": w.bank, "account": w.account,
            "default_daily_wage": w.default_daily_wage,
            "employee_type": w.employee_type or "알바",
            "monthly_salary": w.monthly_salary or 0,
            "resident_no": w.resident_no or "",
            "photo_filename": w.photo_filename or "",
            "memo": w.memo, "is_active": w.is_active,
            "hire_date": getattr(w, "hire_date", None),
            "resign_date": getattr(w, "resign_date", None),
            "employment_status": getattr(w, "employment_status", "재직") or "재직",
        } for w in workers]
    return templates.TemplateResponse(request, "workers.html", {
        "user": user, "workers": rows, "search_q": search_q,
        "type_filter": type_filter, "status_filter": status_filter,
    })


@router.post("/workers/new")
async def worker_create(request: Request):
    _user(request)
    form = await request.form()

    name = (form.get("name", "") or "").strip()
    if not name:
        raise HTTPException(400, "이름은 필수입니다.")

    etype = (form.get("employee_type", "알바") or "알바").strip()
    if etype not in ("정직원", "알바"):
        etype = "알바"

    wage = _safe_int(form.get("default_daily_wage", "150000"), 150000)
    salary = _safe_int(form.get("monthly_salary", "0"), 0)

    if etype == "정직원":
        wage = 0
    else:
        salary = 0

    resident_no = _format_resident_no(form.get("resident_no", ""))

    # 사진 업로드 처리
    photo_file = form.get("photo")
    photo_filename = ""
    if photo_file and hasattr(photo_file, "filename") and photo_file.filename:
        photo_filename = _save_photo(photo_file)

    # 정직원 인사 필드
    from datetime import datetime as _dt
    def _pd(s):
        if not s: return None
        try: return _dt.strptime(str(s).strip(), "%Y-%m-%d").date()
        except: return None
    hire_date = _pd(form.get("hire_date"))
    resign_date = _pd(form.get("resign_date"))
    emp_status = (form.get("employment_status", "재직") or "재직").strip()
    if emp_status not in ("재직", "휴직", "퇴사"):
        emp_status = "재직"
    position = (form.get("position", "") or "").strip()

    with Session(engine, expire_on_commit=False) as s:
        w = Worker(
            name=name,
            phone=(form.get("phone", "") or "").strip(),
            bank=(form.get("bank", "") or "").strip(),
            account=(form.get("account", "") or "").strip(),
            default_daily_wage=wage,
            employee_type=etype,
            monthly_salary=salary,
            resident_no=resident_no,
            photo_filename=photo_filename,
            hire_date=hire_date, resign_date=resign_date,
            employment_status=emp_status, position=position,
            memo=(form.get("memo", "") or "").strip(),
        )
        s.add(w)
        s.commit()
    return RedirectResponse(f"/workers?type={etype}&created={etype}", status_code=303)


@router.get("/workers/{wid}/edit", response_class=HTMLResponse)
def worker_edit(request: Request, wid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        w = s.get(Worker, wid)
        if not w:
            raise HTTPException(404)
        wd = {
            "id": w.id, "name": w.name, "phone": w.phone, "bank": w.bank,
            "account": w.account, "default_daily_wage": w.default_daily_wage,
            "employee_type": w.employee_type or "알바",
            "monthly_salary": w.monthly_salary or 0,
            "resident_no": w.resident_no or "",
            "photo_filename": w.photo_filename or "",
            "memo": w.memo, "is_active": w.is_active,
            "hire_date": getattr(w, "hire_date", None),
            "resign_date": getattr(w, "resign_date", None),
            "employment_status": getattr(w, "employment_status", "재직") or "재직",
            "position": getattr(w, "position", "") or "",
        }
    return templates.TemplateResponse(request, "worker_edit.html", {"user": user, "w": wd})


@router.post("/workers/{wid}/edit")
async def worker_update(request: Request, wid: int):
    _user(request)
    form = await request.form()

    etype = (form.get("employee_type", "알바") or "알바").strip()
    if etype not in ("정직원", "알바"):
        etype = "알바"

    wage = _safe_int(form.get("default_daily_wage", "150000"), 150000)
    salary = _safe_int(form.get("monthly_salary", "0"), 0)
    if etype == "정직원":
        wage = 0
    else:
        salary = 0

    from datetime import datetime as _dt
    def _pd(s):
        if not s: return None
        try: return _dt.strptime(str(s).strip(), "%Y-%m-%d").date()
        except: return None
    hire_date = _pd(form.get("hire_date"))
    resign_date = _pd(form.get("resign_date"))
    emp_status = (form.get("employment_status", "재직") or "재직").strip()
    if emp_status not in ("재직", "휴직", "퇴사"):
        emp_status = "재직"
    # 퇴사 상태인데 퇴사일 없으면 오늘로 자동 설정
    if emp_status == "퇴사" and not resign_date:
        from datetime import date as _d
        resign_date = _d.today()
    position = (form.get("position", "") or "").strip()

    with Session(engine, expire_on_commit=False) as s:
        w = s.get(Worker, wid)
        if not w:
            raise HTTPException(404)
        w.name = (form.get("name", "") or w.name).strip()
        w.phone = (form.get("phone", "") or "").strip()
        w.bank = (form.get("bank", "") or "").strip()
        w.account = (form.get("account", "") or "").strip()
        w.default_daily_wage = wage
        w.employee_type = etype
        w.monthly_salary = salary
        w.resident_no = _format_resident_no(form.get("resident_no", ""))
        w.hire_date = hire_date
        w.resign_date = resign_date
        w.employment_status = emp_status
        w.position = position

        # 사진 변경 처리
        photo_file = form.get("photo")
        if photo_file and hasattr(photo_file, "filename") and photo_file.filename:
            new_photo = _save_photo(photo_file)
            if new_photo:
                # 기존 사진 삭제 (있으면)
                if w.photo_filename:
                    try:
                        (WORKER_PHOTO_DIR / w.photo_filename).unlink(missing_ok=True)
                    except Exception:
                        pass
                w.photo_filename = new_photo

        # 사진 삭제 체크박스
        if form.get("delete_photo") == "yes" and w.photo_filename:
            try:
                (WORKER_PHOTO_DIR / w.photo_filename).unlink(missing_ok=True)
            except Exception:
                pass
            w.photo_filename = ""

        w.memo = (form.get("memo", "") or "").strip()
        s.add(w)
        s.commit()
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{wid}/toggle")
def worker_toggle(request: Request, wid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        w = s.get(Worker, wid)
        if w:
            w.is_active = not w.is_active
            s.add(w)
            s.commit()
    return RedirectResponse("/workers", status_code=303)


@router.post("/workers/{wid}/delete")
def worker_delete(request: Request, wid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        w = s.get(Worker, wid)
        if w:
            # 사진 파일도 함께 삭제
            if w.photo_filename:
                try:
                    (WORKER_PHOTO_DIR / w.photo_filename).unlink(missing_ok=True)
                except Exception:
                    pass
            s.delete(w)
            s.commit()
    return RedirectResponse("/workers", status_code=303)


@router.get("/worker-photo/{filename}")
def serve_worker_photo(request: Request, filename: str):
    """근무자 사진 파일 서빙 (로그인 필요)"""
    _user(request)
    # 경로 traversal 방지
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "잘못된 파일명")
    fpath = WORKER_PHOTO_DIR / filename
    if not fpath.exists():
        raise HTTPException(404, "사진을 찾을 수 없습니다.")
    return FileResponse(fpath)
