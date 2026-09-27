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


@app.get("/deploy-check", response_class=HTMLResponse)
def deploy_check():
    """배포 진단 페이지 — 현재 배포된 코드에 어떤 기능이 포함되어 있는지 확인.
    /deploy-check 로 접속하면 로그인 없이 표시됨.
    """
    import os
    checks = []

    # 1. 배지 버전 확인
    try:
        with open("templates/base.html", "r", encoding="utf-8") as f:
            base_content = f.read()
    except Exception:
        try:
            with open(os.path.join(os.path.dirname(__file__), "templates/base.html"), "r", encoding="utf-8") as f:
                base_content = f.read()
        except Exception:
            base_content = ""

    import re
    m = re.search(r"_current_version = '([^']+)'", base_content)
    ver = m.group(1) if m else "(알 수 없음)"

    # 2. 각 기능 검증
    try:
        with open(os.path.join(os.path.dirname(__file__), "routes_quote.py"), "r", encoding="utf-8") as f:
            rq = f.read()
    except Exception:
        rq = ""

    try:
        with open(os.path.join(os.path.dirname(__file__), "templates/mobile/quotes.html"), "r", encoding="utf-8") as f:
            mq = f.read()
    except Exception:
        mq = ""

    # 추가 파일 로드
    def _read(rel):
        try:
            with open(os.path.join(os.path.dirname(__file__), rel), "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""

    rex = _read("routes_expense.py")
    rpj = _read("routes_project.py")
    t_new = _read("templates/expense_new.html")
    t_edit = _read("templates/expense_edit.html")
    t_list = _read("templates/expenses.html")
    t_unpaid = _read("templates/expenses_unpaid.html")
    t_home = _read("templates/home.html")
    js_proj = _read("static/project-search.js")
    js_sw = _read("static/sw.js")
    db_py = _read("database.py")

    # ── 이전 기능 (견적/프로젝트) ──
    checks.append(("견적 수주 오류 안전화", "_unique_project_code" in rq and "프로젝트 생성 실패" in rq))
    checks.append(("견적↔프로젝트 자동 동기화", "_sync_project_from_quote" in rq))
    checks.append(("견적서 거래처 자동 매칭", "Vendor.name == q.vendor_name" in rq))
    checks.append(("프로젝트 자동 상태전환 (준비중/진행중/완료)", "IMMINENT_DAYS" in rpj and "sync_auto_status_to_db" in rpj))

    # ── 하청/외주 지급 전환 ──
    checks.append(("메뉴: 하청/외주 지급으로 전환", "하청/외주 지급" in base_content))
    checks.append(("DB: 하청 상세사양 필드 (spec_detail)", "spec_detail" in db_py))
    checks.append(("DB: 부가세 필드 (vat_mode/supply/vat)", "vat_mode" in db_py and "supply_amount" in db_py))
    checks.append(("지급 상태 토글 (미지급↔지급완료)", "toggle-pay" in rex))
    checks.append(("거래처별 미지급 집계 (에러 수정)", "g[\"rows\"]" in rex and "for it in g.rows" in t_unpaid))
    checks.append(("대시보드: 총 지출 + 미지급 표기", "총 지출" in t_home and "sub_unpaid_amount" in t_home))

    # ── 발주 등록 UI 통일 ──
    checks.append(("프로젝트 검색 위젯 파일 존재", len(js_proj) > 500 and "data-project-search" in js_proj))
    checks.append(("프로젝트 검색 API", "api/projects/search" in rpj))
    checks.append(("발주등록: 프로젝트 검색 위젯 적용", "data-project-search" in t_new))
    checks.append(("발주등록: 거래처 검색 위젯 적용", "data-vendor-search" in t_new))
    checks.append(("발주수정: 검색 위젯 적용", "data-project-search" in t_edit and "data-vendor-search" in t_edit))
    checks.append(("부가세 입력 + 금액 콤마", "vat-mode" in t_new and "toLocaleString" in t_new))
    checks.append(("구 비용처리 정리메뉴 제거됨", "purge-all" not in t_list and "구 비용처리" not in t_list))

    # ── 캐시 문제 수정 ──
    checks.append(("HTML 캐시 차단 (배포 즉시 반영)", "배포 후 옛 화면이 보이는 문제 방지" in _read("main.py")))
    checks.append(("정적파일 캐시버스팅 (?v=)", "_asset_ver" in base_content))
    checks.append(("서비스워커: HTML 캐시 안 함", "isHTML" in js_sw))

    # ── v1600: QR라벨 / 내보내기 / 인쇄 ──
    t_qsel = _read("templates/equipment_labels_select.html")
    t_qnew = _read("templates/quote_new.html")
    t_mbase = _read("templates/mobile/base.html")
    t_pdet = _read("templates/product_detail.html")
    req = _read("routes_equipment.py")
    rexp = _read("routes_export.py")
    checks.append(("QR라벨: 선택 인쇄 화면", len(t_qsel) > 500 and "unit-check" in t_qsel))
    checks.append(("QR라벨: 제품ID 오동작 수정 (eq 파라미터)", "EquipmentUnit.equipment_id == int(eq)" in req))
    checks.append(("QR라벨: 제품상세 링크 수정", "labels/select?eq=" in t_pdet))
    checks.append(("견적: 순서 ▲▼ 버튼 표시 수정", "row-move-btn" in t_qnew))
    checks.append(("인쇄: PC 다크모드 흰배경 강제", "prefers-color-scheme" in base_content))
    checks.append(("인쇄: 모바일 다크모드 흰배경 강제", "prefers-color-scheme" in t_mbase))
    checks.append(("내보내기: 엑셀 모듈 (7종)", len(rexp) > 1000 and "export/projects" in rexp))
    checks.append(("내보내기: 라우터 등록", "export_router" in _read("main.py")))

    rows = ""
    all_pass = True
    for name, ok in checks:
        icon = "✅" if ok else "❌"
        color = "#15803D" if ok else "#DC2626"
        if not ok:
            all_pass = False
        rows += f'<tr><td style="padding:10px 14px;">{name}</td><td style="padding:10px 14px; color:{color}; font-weight:700;">{icon} {"반영됨" if ok else "누락됨"}</td></tr>'

    overall_color = "#15803D" if all_pass else "#DC2626"
    overall_icon = "✅" if all_pass else "⚠️"
    overall_text = "모든 최신 기능이 정상 배포되었습니다" if all_pass else "일부 파일이 배포에 누락되었습니다 — GitHub 저장소에 아래 파일 재업로드 필요"

    html = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>배포 진단</title>
<style>
body{{font-family:-apple-system,'Pretendard',sans-serif;background:#F4F4F5;margin:0;padding:30px 20px;}}
.wrap{{max-width:720px;margin:0 auto;}}
h1{{font-size:22px;font-weight:800;margin:0 0 6px;color:#0A0E1A;}}
.sub{{font-size:13px;color:#52525B;margin-bottom:20px;}}
.overall{{padding:20px;border-radius:12px;background:white;border:2px solid {overall_color};margin-bottom:20px;text-align:center;}}
.overall .icon{{font-size:32px;}}
.overall .msg{{font-size:15px;font-weight:700;color:{overall_color};margin-top:8px;}}
table{{width:100%;background:white;border-radius:10px;border-collapse:collapse;box-shadow:0 2px 8px rgba(0,0,0,0.05);}}
th{{background:#0A0E1A;color:white;padding:12px 14px;text-align:left;font-size:13px;}}
td{{border-bottom:1px solid #F4F4F5;font-size:13px;}}
tr:last-child td{{border-bottom:none;}}
.ver{{display:inline-block;padding:4px 12px;background:#0A0E1A;color:#C9A961;border-radius:999px;font-family:monospace;font-size:12px;font-weight:700;}}
.help{{margin-top:20px;padding:16px;background:#FEF3C7;border:1px solid #FCD34D;border-radius:10px;font-size:12px;color:#78350F;line-height:1.7;}}
.help b{{color:#92400E;}}
.help code{{background:white;padding:2px 6px;border-radius:4px;font-size:11px;}}
a.btn{{display:inline-block;margin-top:16px;background:#0A0E1A;color:white;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:700;font-size:13px;}}
</style></head><body><div class="wrap">
<h1>🔍 배포 진단 페이지</h1>
<div class="sub">현재 배포된 코드에 최신 기능이 포함되어 있는지 확인합니다.</div>
<div style="margin-bottom:12px;">현재 배포 버전: <span class="ver">⚡ {ver}</span></div>
<div class="overall">
  <div class="icon">{overall_icon}</div>
  <div class="msg">{overall_text}</div>
</div>
<table>
  <tr><th style="width:70%;">기능</th><th>상태</th></tr>
  {rows}
</table>
{"" if all_pass else '''<div class="help"><b>❌ 누락된 기능이 있으신가요?</b><br>
① GitHub 저장소에 다음 3개 파일이 최신 상태로 올라가 있는지 확인:<br>
&nbsp;&nbsp;• <code>app/routes_quote.py</code> (32,487 bytes 이상)<br>
&nbsp;&nbsp;• <code>app/templates/base.html</code> (19,914 bytes 이상)<br>
&nbsp;&nbsp;• <code>app/templates/mobile/quotes.html</code> (2,763 bytes 이상)<br><br>
② Railway 대시보드 → Deployments 탭 → 최근 배포 상태가 <b>"Success"</b>인지 확인<br>
③ 브라우저 <b>강력 새로고침</b> (Ctrl+Shift+R / ⌘+Shift+R)<br>
④ 배지가 계속 이전 버전으로 표시된다면 GitHub push가 안 된 것 — 다시 push 필요</div>'''}
<a href="/" class="btn">← 홈으로</a>
</div></body></html>"""
    return HTMLResponse(html)


# ============================================================
# 인증 헬퍼
# ============================================================
# ── 자동 로그아웃 / 자동 로그인 설정 ──
IDLE_TIMEOUT_SEC = int(os.environ.get("IDLE_TIMEOUT_MIN", "60")) * 60   # 60분 무동작 시 로그아웃
REMEMBER_DAYS = 30                                                       # '자동 로그인' 유지 기간
REMEMBER_COOKIE = "remember_token"


def _remember_serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(_SECRET_FOR_REMEMBER(), salt="remember-login")


def _SECRET_FOR_REMEMBER():
    return os.environ.get("SECRET_KEY") or "change-this-secret-in-production-1234567890"


def _make_remember_token(user) -> str:
    # 비밀번호가 바뀌면 기존 토큰 무효화되도록 해시 일부 포함
    return _remember_serializer().dumps({"uid": user.id, "ph": (user.password_hash or "")[-12:]})


def _user_from_remember(request: Request) -> Optional[User]:
    tok = request.cookies.get(REMEMBER_COOKIE)
    if not tok:
        return None
    try:
        data = _remember_serializer().loads(tok, max_age=REMEMBER_DAYS * 86400)
    except Exception:
        return None
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(User, data.get("uid"))
        if not u or not getattr(u, "is_active", True):
            return None
        if (u.password_hash or "")[-12:] != data.get("ph"):
            return None
        return u


def current_user(request: Request) -> Optional[User]:
    import time
    uid = request.session.get("user_id")
    now = int(time.time())
    if uid:
        last = request.session.get("last_seen", now)
        # 자동 로그인이 아닌 세션은 무동작 시간 초과 시 로그아웃
        if not request.session.get("remember") and now - int(last) > IDLE_TIMEOUT_SEC:
            request.session.clear()
            uid = None
        else:
            request.session["last_seen"] = now
    if not uid:
        # '자동 로그인' 쿠키가 있으면 세션 복원
        u = _user_from_remember(request)
        if u:
            request.session["user_id"] = u.id
            request.session["remember"] = True
            request.session["last_seen"] = now
            return u
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
    # /deploy-check 도 로그인 없이 배포 진단 가능
    if request.url.path in ("/health", "/healthz", "/deploy-check"):
        return await call_next(request)
    try:
        user = current_user(request)
        request.state.user = user
        # 권한 정보 함께 전달 (템플릿에서 perms.xxx 로 접근)
        from permissions import get_permissions, is_admin, money_flags
        request.state.perms = get_permissions(user)
        request.state.is_admin = is_admin(user)
        for _k, _v in money_flags(user).items():
            setattr(request.state, _k, _v)
    except Exception:
        request.state.user = None
        request.state.perms = {}
        request.state.is_admin = False
        for _k in ("can_view_amounts", "can_view_revenue", "can_view_settle", "can_manage_settle",
                   "can_view_sub", "can_manage_sub", "can_view_wage", "can_manage_wage", "can_view_profit"):
            setattr(request.state, _k, False)
    # 회사 설정을 전역에서 사용 가능하게
    try:
        from database import get_company_settings
        request.state.company = get_company_settings()
    except Exception:
        request.state.company = {}
    # ★ 저장 후 작업하던 화면으로 돌아가기
    #   (본문을 미리 읽으면 라우트에서 폼이 비어버리므로, 주소의 ?_rt= 값만 사용)
    _return_to = None
    if request.method == "POST":
        _rt = (request.query_params.get("_rt") or "").strip()
        if _rt.startswith("/") and not _rt.startswith("//") and not _rt.startswith("/login"):
            _return_to = _rt
    response = await call_next(request)
    if _return_to and response.status_code in (302, 303):
        _loc = response.headers.get("location", "")
        # 로그인 이동·삭제 후 이동·에러는 건드리지 않음
        if _loc and "/login" not in _loc and "delete" not in request.url.path:
            sep = "&" if "?" in _return_to else "?"
            _flag = "saved=1"
            # 이미 저장 알림 파라미터가 있으면 중복 방지
            new_loc = _return_to if "saved=1" in _return_to else f"{_return_to.split('#')[0]}{sep}{_flag}" + (("#" + _return_to.split("#", 1)[1]) if "#" in _return_to else "")
            response.headers["location"] = new_loc
    # ⭐ HTML 화면은 절대 캐시하지 않음 — 배포 후 옛 화면이 보이는 문제 방지
    try:
        ctype = response.headers.get("content-type", "")
        if "text/html" in ctype:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
    except Exception:
        pass
    return response


# SessionMiddleware는 가장 나중에 add — 그래야 가장 바깥에서 실행되어 session 주입이 먼저 됨
_SECRET = os.environ.get("SECRET_KEY") or "change-this-secret-in-production-1234567890"
# ⭐ max_age=None → '브라우저 세션 쿠키'. 브라우저/앱을 완전히 닫으면 쿠키가 사라져 자동 로그아웃.
app.add_middleware(SessionMiddleware, secret_key=_SECRET, max_age=None, same_site="lax")


# ============================================================
# 로그인 / 로그아웃
# ============================================================
def _safe_next(nxt: str) -> str:
    nxt = (nxt or "").strip()
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "", expired: int = 0):
    if current_user(request):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "error": None, "next": _safe_next(next) if next else "",
        "info": "일정 시간 사용하지 않아 자동 로그아웃되었습니다." if expired else None,
    })


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          remember: str = Form(""), next: str = Form("")):
    import time
    username = (username or "").strip()
    with Session(engine, expire_on_commit=False) as s:
        user = s.exec(select(User).where(User.username == username)).first()
        if not user or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request, "login.html",
                {"error": "아이디 또는 비밀번호가 잘못되었습니다.", "next": _safe_next(next) if next else "",
                 "info": None, "last_username": username},
            )
        request.session.clear()
        request.session["user_id"] = user.id
        request.session["last_seen"] = int(time.time())
        request.session["remember"] = bool(remember)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    if remember:
        resp.set_cookie(REMEMBER_COOKIE, _make_remember_token(user),
                        max_age=REMEMBER_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https")
    else:
        resp.delete_cookie(REMEMBER_COOKIE)
    return resp


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(REMEMBER_COOKIE)   # 명시적 로그아웃 = 자동 로그인도 해제
    return resp


@app.get("/api/session-ping")
def session_ping(request: Request):
    """화면이 다시 보일 때 세션 유효성 확인 (앱 복귀 시 자동 로그아웃 판정용)"""
    from fastapi.responses import JSONResponse
    return JSONResponse({"ok": bool(current_user(request))})


# ============================================================
# 홈 / 대시보드
# ============================================================
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    # ⭐ 대시보드 진입 시 프로젝트 자동 상태 동기화 (준비중↔진행중↔완료)
    # → 이걸로 "미수금 업데이트가 안 됨" 이슈 해결
    from routes_project import auto_status, sync_all_project_statuses
    with Session(engine, expire_on_commit=False) as s:
        try:
            sync_all_project_statuses(s)
        except Exception as ex:
            print(f"[WARN] dashboard auto-status sync failed: {ex}")

        projects = s.exec(select(Project)).all()
        total_revenue = sum(p.revenue for p in projects)
        total_supply = sum((p.supply_amount or p.revenue) for p in projects)
        total_vat = sum((p.vat_amount or 0) for p in projects)
        # ⭐ 하청/외주 지급 집계 (구 '비용처리' → 성격 전환)
        _all_exp = s.exec(select(Expense)).all()
        total_expense = sum(e.amount for e in _all_exp)
        # 지급 상태별 분리 — 인건비와 동일 관점
        sub_unpaid_amount = sum(
            e.amount for e in _all_exp
            if (getattr(e, "pay_status", "미지급") or "미지급") == "미지급"
        )
        sub_unpaid_count = sum(
            1 for e in _all_exp
            if (getattr(e, "pay_status", "미지급") or "미지급") == "미지급"
        )
        sub_paid_amount = total_expense - sub_unpaid_amount

        _all_att = s.exec(select(Attendance)).all()
        total_wage = sum(a.total_wage for a in _all_att)
        # 인건비 미지급
        wage_unpaid_amount = sum(
            a.total_wage for a in _all_att
            if (getattr(a, "pay_status", "미지급") or "미지급") == "미지급"
        )
        wage_unpaid_count = sum(
            1 for a in _all_att
            if (getattr(a, "pay_status", "미지급") or "미지급") == "미지급"
        )

        # 거래처 맵 (한 번에 조회하여 N+1 방지)
        vendors_map = {v.id: v.name for v in s.exec(select(Vendor)).all()}

        # ★ 미수금 정의: "행사일이 이미 지났고 + 아직 입금되지 않은" 프로젝트만
        # → auto_status()로 계산된 최신 정산상태 기준 (DB 미수금 잔상 방지)
        today_d = date.today()
        unpaid_projects = []
        for p in projects:
            # 1) 행사일이 없거나 미래면 → 매출이 아직 발생 안 함 → 미수금 아님
            if not p.event_date or p.event_date >= today_d:
                continue
            # 2) 취소된 프로젝트 제외
            if p.status == "취소":
                continue
            # 3) ⭐ auto_status로 최종 정산상태 계산 (DB 잔상 무시)
            _, auto_settle = auto_status(p)
            # 4) paid_date 또는 auto_settle이 '입금완료'면 제외
            if p.paid_date or auto_settle == "입금완료" or p.settlement_status == "입금완료":
                continue
            # 5) 위 조건을 모두 통과 → 진짜 미수금
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

    response = templates.TemplateResponse(request, "home.html", {"user": user,
        "total_revenue": total_revenue,
        "total_supply": total_supply,
        "total_vat": total_vat,
        "total_expense": total_expense,
        "sub_unpaid_amount": sub_unpaid_amount,
        "sub_unpaid_count": sub_unpaid_count,
        "sub_paid_amount": sub_paid_amount,
        "wage_unpaid_amount": wage_unpaid_amount,
        "wage_unpaid_count": wage_unpaid_count,
        "total_wage": total_wage,
        "net_profit": net_profit,
        "unpaid_amount": unpaid_amount,
        "unpaid_count": unpaid_count,
        "project_count": len(projects),
        "recent_projects": recent_projects,
        "unpaid_projects": unpaid_projects,  # 전체 (5건 제한 제거)
        "today": date.today(),
    })
    # ⭐ 브라우저 캐시 방지 — 뒤로가기 시 이전 미수금 화면 잔상 방지
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


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
from routes_export import router as export_router
from routes_employee import router as employee_router
from routes_staff import router as staff_router

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
app.include_router(export_router)
app.include_router(staff_router)


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
