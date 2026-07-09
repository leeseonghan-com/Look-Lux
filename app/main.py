"""FastAPI 메인 앱 - 정산 · 견적 · 근태 관리 시스템"""
import os
import shutil
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session, select
from PIL import Image

from database import (
    engine, init_db, verify_password, hash_password,
    User, Vendor, Project, Expense, Worker, Attendance, Item, Quote, QuoteItem,
)
# Vendor는 이미 위에서 import됨 — 별도 추가 불필요
from template_utils import templates

# ============================================================
app = FastAPI(title="정산관리 시스템")

BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
_DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(f"{_DATA_DIR}/receipts", exist_ok=True)
os.makedirs(f"{_DATA_DIR}/company", exist_ok=True)
app.mount("/receipts", StaticFiles(directory=f"{_DATA_DIR}/receipts"), name="receipts")
app.mount("/company", StaticFiles(directory=f"{_DATA_DIR}/company"), name="company")



@app.on_event("startup")
def startup():
    try:
        init_db()
    except Exception as e:
        # 마이그레이션 실패해도 서버는 살아있게 (Healthcheck 통과용)
        import traceback
        print(f"[WARN] init_db failed: {e}")
        traceback.print_exc()


# 모듈 import 시점에도 초기화 (TestClient 등 startup 이벤트 미호출 환경 대비)
try:
    init_db()
except Exception as e:
    import traceback
    print(f"[WARN] init_db (module-import) failed: {e}")
    traceback.print_exc()


# ============================================================
# Healthcheck 엔드포인트 (Railway / Fly.io 헬스체크용)
# DB나 세션을 건드리지 않고 즉시 200 응답 → 헬스체크 안정성 확보
# ============================================================
@app.get("/health")
def healthcheck():
    return {"status": "ok"}


# ============================================================
# 친절한 권한 에러 페이지 (403)
# ============================================================
from fastapi.exceptions import HTTPException as _HTTPException
from fastapi.requests import Request as _Request

@app.exception_handler(_HTTPException)
async def http_exception_handler(request: _Request, exc: _HTTPException):
    """모든 HTTP 에러를 친절한 안내 페이지로"""
    from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
    # 303은 리다이렉트 (로그인 페이지 등) — 그대로 처리
    if exc.status_code == 303:
        location = exc.headers.get("Location", "/") if exc.headers else "/"
        return RedirectResponse(location, status_code=303)
    is_mobile = request.url.path.startswith("/m/")
    # 친절한 에러 페이지 (400 / 403 / 404 / 500 등)
    icon, title, color = {
        400: ("⚠", "잘못된 요청", "#B45309"),
        403: ("🔒", "접근 권한이 없습니다", "#B45309"),
        404: ("🔎", "찾을 수 없는 항목입니다", "#6B7280"),
    }.get(exc.status_code, ("⚠", "오류가 발생했습니다", "#991B1B"))
    detail = (exc.detail if isinstance(exc.detail, str) else None) or "잠시 후 다시 시도해주세요."
    back_url = "/m/" if is_mobile else "/"
    referer = request.headers.get("referer", "")
    if referer and not referer.endswith(request.url.path):
        back_url = referer
    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>body{{font-family:-apple-system,system-ui,'Pretendard',sans-serif;background:#F4F4F5;margin:0;padding:40px 20px;text-align:center;color:#18181B;}}
    .icon{{font-size:72px;margin-bottom:20px;}}
    h1{{font-size:22px;font-weight:800;margin:0 0 12px;color:{color};}}
    p{{font-size:14px;color:#52525B;line-height:1.6;margin:0 0 24px;max-width:420px;margin-left:auto;margin-right:auto;}}
    .btn{{display:inline-block;background:#0A0E1A;color:white;padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:700;margin:4px;}}
    .btn-sec{{background:#E4E4E7;color:#18181B;}}
    code{{display:inline-block;background:#F4F4F5;padding:2px 6px;border-radius:4px;font-size:12px;color:#52525B;}}</style>
    </head><body><div class="icon">{icon}</div><h1>{title}</h1>
    <p>{detail}</p>
    <a href="{back_url}" class="btn">← 이전 페이지</a>
    <a href="{'/m/' if is_mobile else '/'}" class="btn btn-sec">홈으로</a>
    <div style="margin-top:24px;"><code>{request.url.path} · {exc.status_code}</code></div>
    </body></html>"""
    return HTMLResponse(html, status_code=exc.status_code)


# 500 에러도 동일하게 처리
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: _Request, exc: Exception):
    """예상하지 못한 서버 에러를 친절한 페이지로 — 콘솔에는 트레이스 출력"""
    import traceback
    print(f"[ERROR] {request.method} {request.url.path}: {exc}")
    traceback.print_exc()
    from fastapi.responses import HTMLResponse
    is_mobile = request.url.path.startswith("/m/")
    back_url = request.headers.get("referer", "/m/" if is_mobile else "/")
    detail = str(exc)[:300] if str(exc) else "내부 처리 중 문제가 발생했습니다."
    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>서버 오류</title>
    <style>body{{font-family:-apple-system,system-ui,'Pretendard',sans-serif;background:#F4F4F5;margin:0;padding:40px 20px;text-align:center;color:#18181B;}}
    .icon{{font-size:72px;margin-bottom:20px;}}
    h1{{font-size:22px;font-weight:800;margin:0 0 12px;color:#991B1B;}}
    p{{font-size:14px;color:#52525B;line-height:1.6;margin:0 0 24px;max-width:420px;margin-left:auto;margin-right:auto;}}
    .btn{{display:inline-block;background:#0A0E1A;color:white;padding:14px 28px;border-radius:10px;text-decoration:none;font-weight:700;margin:4px;}}
    .btn-sec{{background:#E4E4E7;color:#18181B;}}
    details{{margin-top:20px;max-width:480px;margin-left:auto;margin-right:auto;text-align:left;background:#FEF2F2;padding:12px;border-radius:8px;border:1px solid #FECACA;}}
    code{{display:block;background:#F4F4F5;padding:8px;border-radius:4px;font-size:11px;color:#52525B;white-space:pre-wrap;word-break:break-all;}}</style>
    </head><body><div class="icon">⚠</div><h1>일시적인 오류가 발생했습니다</h1>
    <p>방금 시도하신 작업이 처리되지 않았습니다. 다시 시도하거나 관리자에게 문의해주세요.</p>
    <a href="{back_url}" class="btn">← 이전 페이지</a>
    <a href="{'/m/' if is_mobile else '/'}" class="btn btn-sec">홈으로</a>
    <details><summary style="cursor:pointer;font-size:12px;color:#991B1B;">기술 정보</summary>
    <code>{request.method} {request.url.path}\n{detail}</code></details>
    </body></html>"""
    return HTMLResponse(html, status_code=500)


@app.get("/healthz")  # 표준 k8s 스타일 alias
def healthcheck_z():
    return {"status": "ok"}


# ============================================================
# 인증 헬퍼
# ============================================================
def current_user(request: Request) -> Optional[User]:
    uid = request.session.get("user_id")
    if not uid:
        return None
    with Session(engine, expire_on_commit=False) as s:
        return s.get(User, uid)


def require_login(request: Request) -> User:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return u


@app.middleware("http")
async def add_user_to_request(request: Request, call_next):
    # Healthcheck 경로는 미들웨어 부담 없이 즉시 통과 (Railway/Fly.io 안정성)
    if request.url.path in ("/health", "/healthz"):
        return await call_next(request)
    try:
        user = current_user(request)
        request.state.user = user
        # 권한 정보 함께 전달 (템플릿에서 perms.xxx 로 접근)
        from permissions import get_permissions, is_admin, can_view_amounts
        request.state.perms = get_permissions(user)
        request.state.is_admin = is_admin(user)
        request.state.can_view_amounts = can_view_amounts(user)
    except Exception:
        request.state.user = None
        request.state.perms = {}
        request.state.is_admin = False
        request.state.can_view_amounts = False
    # 회사 설정을 전역에서 사용 가능하게
    try:
        from database import get_company_settings
        request.state.company = get_company_settings()
    except Exception:
        request.state.company = {}
    response = await call_next(request)
    return response


# SessionMiddleware는 가장 나중에 add — 그래야 가장 바깥에서 실행되어 session 주입이 먼저 됨
_SECRET = os.environ.get("SECRET_KEY") or "change-this-secret-in-production-1234567890"
app.add_middleware(SessionMiddleware, secret_key=_SECRET)


# ============================================================
# 로그인 / 로그아웃
# ============================================================
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with Session(engine, expire_on_commit=False) as s:
        user = s.exec(select(User).where(User.username == username)).first()
        if not user or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request, "login.html", {"error": "아이디 또는 비밀번호가 잘못되었습니다."}
            )
        request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ============================================================
# 홈 / 대시보드
# ============================================================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # auto_status는 routes_project 모듈에 정의됨 (행사일 기준 자동 전환)
    from routes_project import auto_status
    with Session(engine, expire_on_commit=False) as s:
        projects = s.exec(select(Project)).all()
        total_revenue = sum(p.revenue for p in projects)
        total_supply = sum((p.supply_amount or p.revenue) for p in projects)
        total_vat = sum((p.vat_amount or 0) for p in projects)
        total_expense = sum(e.amount for e in s.exec(select(Expense)).all())
        total_wage = sum(a.total_wage for a in s.exec(select(Attendance)).all())

        # 거래처 맵 (한 번에 조회하여 N+1 방지)
        vendors_map = {v.id: v.name for v in s.exec(select(Vendor)).all()}

        # ★ 미수금 정의: "행사일이 이미 지났고 + 아직 입금되지 않은" 프로젝트만
        # → 행사 시작 전 프로젝트(준비중/진행중)는 아직 매출이 발생하지 않았으므로 미수금에서 제외
        today_d = date.today()
        unpaid_projects = []
        for p in projects:
            # 1) 행사일이 없거나 미래면 → 매출이 아직 발생 안 함 → 미수금 아님
            if not p.event_date or p.event_date >= today_d:
                continue
            # 2) 취소된 프로젝트 제외
            if p.status == "취소":
                continue
            # 3) 이미 입금완료된 건 제외
            if p.paid_date or p.settlement_status == "입금완료":
                continue
            # 4) 위 조건을 모두 통과 → 진짜 미수금
            unpaid_projects.append({
                "id": p.id, "code": p.code, "name": p.name,
                "vendor_name": vendors_map.get(p.vendor_id, "거래처 미지정") if p.vendor_id else "-",
                "event_date": p.event_date,
                "revenue": p.revenue,
                "supply_amount": p.supply_amount or p.revenue,
                "days_overdue": (today_d - p.event_date).days,  # 연체일수
            })
        # 행사일이 오래된 미수금(연체일수 큰 것)부터 정렬
        unpaid_projects.sort(key=lambda x: x["event_date"] or date.max, reverse=False)
        unpaid_amount = sum(p["revenue"] for p in unpaid_projects)
        unpaid_count = len(unpaid_projects)

        # 순이익은 공급가액 기준 (부가세는 결국 국세청에 납부할 돈이라 회사 이익 아님)
        net_profit = total_supply - total_expense - total_wage

        # 최근 프로젝트 5개
        recent_projects = sorted(projects, key=lambda p: p.created_at, reverse=True)[:5]

    return templates.TemplateResponse(request, "home.html", {"user": user,
        "total_revenue": total_revenue,
        "total_supply": total_supply,
        "total_vat": total_vat,
        "total_expense": total_expense,
        "total_wage": total_wage,
        "net_profit": net_profit,
        "unpaid_amount": unpaid_amount,
        "unpaid_count": unpaid_count,
        "project_count": len(projects),
        "recent_projects": recent_projects,
        "unpaid_projects": unpaid_projects,  # 전체 (5건 제한 제거)
        "today": date.today(),
    })


# 다른 라우트들은 별도 파일에서 import
from routes_attendance import router as attendance_router
from routes_expense import router as expense_router
from routes_project import router as project_router
from routes_product import router as product_router    # 통합 물품 등록 (메인)
from routes_item import router as item_router          # 기존 단가표 (호환용 API)
from routes_quote import router as quote_router
from routes_worker import router as worker_router
from routes_vendor import router as vendor_router
from routes_settings import router as settings_router
from routes_equipment import router as equipment_router  # 기존 보유장비 (QR 라벨 등)
from routes_mobile import router as mobile_router
from routes_backup import router as backup_router
from routes_employee import router as employee_router

# 통합 물품 등록 라우터를 가장 먼저 등록 (우선순위 — 호환 리다이렉트 우선 적용)
app.include_router(product_router)
app.include_router(employee_router)
app.include_router(attendance_router)
app.include_router(expense_router)
app.include_router(project_router)
app.include_router(item_router)
app.include_router(quote_router)
app.include_router(worker_router)
app.include_router(vendor_router)
app.include_router(settings_router)
app.include_router(equipment_router)
app.include_router(mobile_router)
app.include_router(backup_router)


# PWA: service worker는 루트 scope에서 작동하도록 별도 핸들러
@app.get("/sw.js")
def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


# TWA (Trusted Web Activity) 도메인 검증
# PWABuilder로 .apk 생성 후 SHA-256 fingerprint를 여기에 등록
# 우선순위: (1) static 폴더 (2) 데이터 폴더 (3) 빈 응답
@app.get("/.well-known/assetlinks.json")
def asset_links():
    """Android TWA 검증 — 여러 위치에서 assetlinks.json 자동 탐색"""
    # 1. static 폴더 (GitHub 커밋 가능)
    static_file = BASE_DIR / "static" / ".well-known" / "assetlinks.json"
    if static_file.exists():
        return FileResponse(static_file, media_type="application/json")
    # 2. 데이터 폴더 (Railway 콘솔에서 업로드 가능)
    user_file = Path(_DATA_DIR) / "assetlinks.json"
    if user_file.exists():
        return FileResponse(user_file, media_type="application/json")
    # 3. 기본 빈 응답
    return JSONResponse([])


if __name__ == "__main__":
    import uvicorn
    # Railway / Fly.io / Render는 PORT 환경변수로 포트를 지정함
    _PORT = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=_PORT, reload=False)
