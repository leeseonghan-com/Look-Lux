"""홈 화면 위젯 · 바로가기용 원터치 출퇴근

- 직원마다 개인 '출퇴근 키'(punch_token)를 발급 → 개인 링크 /p/{token}
- 로그인 없이(자동 로그아웃과 무관하게) 큰 버튼 하나로 출근/퇴근
- 안드로이드: 크롬/삼성인터넷 '홈 화면에 추가' 또는 앱 아이콘 길게 누르기 → 출퇴근
- 아이폰: '단축어' 앱으로 /p/{token}/toggle 호출 → 홈 화면·잠금화면 위젯
- 자정 넘어 새벽 퇴근도 진행 중 근무에 정확히 기록 (routes_attendance 의 로직 재사용)
"""
import secrets
from datetime import date, datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlmodel import Session, select

from database import engine, User, Worker, WorkerCheckin
from template_utils import templates

router = APIRouter()


def _login_user(request: Request):
    u = getattr(request.state, "user", None)
    if not u:
        raise HTTPException(303, headers={"Location": "/login?next=/m/widget"})
    return u


def _ensure_token(s, user) -> str:
    u = s.get(User, user.id)
    if not (getattr(u, "punch_token", "") or ""):
        u.punch_token = secrets.token_urlsafe(18)
        s.add(u)
        s.commit()
    return u.punch_token


def _by_token(s, token: str):
    if not token or len(token) < 16:
        raise HTTPException(404, "잘못된 출퇴근 링크입니다.")
    u = s.exec(select(User).where(User.punch_token == token)).first()
    if not u or not getattr(u, "is_active", True):
        raise HTTPException(404, "만료되었거나 잘못된 출퇴근 링크입니다. 앱에서 새 링크를 받아주세요.")
    from permissions import my_worker, is_employed
    w = my_worker(s, u)
    if not w or not is_employed(w):
        raise HTTPException(403, "출퇴근 대상(재직 정직원)과 연결된 계정이 아닙니다. 관리자에게 문의하세요.")
    return u, w


def _state(s, w):
    from routes_attendance import open_shift, current_shift
    sh = open_shift(s, w.id)
    last = current_shift(s, w.id)
    hrs = None
    if sh and sh.check_in:
        hrs = round((datetime.now() - sh.check_in).total_seconds() / 3600, 1)
    return {
        "working": bool(sh),
        "check_in": sh.check_in if sh else (last.check_in if last else None),
        "check_out": (None if sh else (last.check_out if last else None)),
        "shift_date": sh.work_date if sh else (last.work_date if last else None),
        "hours": hrs,
    }


# ────────────────────────────────────────────────────────────
# 위젯 설정 안내 (로그인 필요)
# ────────────────────────────────────────────────────────────
@router.get("/m/widget", response_class=HTMLResponse)
def widget_setup(request: Request):
    user = _login_user(request)
    from permissions import my_worker, is_employed
    with Session(engine, expire_on_commit=False) as s:
        w = my_worker(s, user)
        ok = bool(w and is_employed(w))
        token = _ensure_token(s, user) if ok else ""
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https":
        base = base.replace("http://", "https://")
    return templates.TemplateResponse(request, "mobile/widget.html", {
        "user": user, "ok": ok, "worker_name": (w.name if w else ""),
        "link": f"{base}/p/{token}" if token else "",
        "toggle_link": f"{base}/p/{token}/toggle" if token else "",
        "active_tab": "more",
    })


@router.get("/m/punch")
def my_punch(request: Request):
    """앱 아이콘 길게 누르기 → '출퇴근' 바로가기: 내 개인 출퇴근 화면으로 이동"""
    user = _login_user(request)
    from permissions import my_worker, is_employed
    with Session(engine, expire_on_commit=False) as s:
        w = my_worker(s, user)
        if not (w and is_employed(w)):
            return RedirectResponse("/m/attendance", status_code=303)
        token = _ensure_token(s, user)
    return RedirectResponse(f"/p/{token}", status_code=303)


@router.post("/m/widget/reset")
def widget_reset(request: Request):
    """링크 분실 시 재발급 — 이전 링크는 즉시 무효"""
    user = _login_user(request)
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(User, user.id)
        u.punch_token = secrets.token_urlsafe(18)
        s.add(u)
        s.commit()
    return RedirectResponse("/m/widget?reset=1", status_code=303)


# ────────────────────────────────────────────────────────────
# 개인 출퇴근 화면 (로그인 불필요 — 링크 자체가 열쇠)
# ────────────────────────────────────────────────────────────
@router.get("/p/{token}", response_class=HTMLResponse)
def punch_page(request: Request, token: str):
    with Session(engine, expire_on_commit=False) as s:
        u, w = _by_token(s, token)
        st = _state(s, w)
    return templates.TemplateResponse(request, "mobile/punch.html", {
        "token": token, "name": w.name, "st": st, "now": datetime.now(),
        "done": request.query_params.get("done", ""),
    })


@router.post("/p/{token}/{action}")
def punch_do(request: Request, token: str, action: str):
    from routes_attendance import do_checkin, do_checkout, open_shift
    with Session(engine, expire_on_commit=False) as s:
        u, w = _by_token(s, token)
        if action == "toggle":
            action = "out" if open_shift(s, w.id) else "in"
        if action == "in":
            r = do_checkin(s, w.id)
            done = "in" if r == "ok" else "dup"
        elif action == "out":
            r = do_checkout(s, w.id)
            done = "out" if r == "ok" else "none"
        else:
            raise HTTPException(400)
        st = _state(s, w)
    if "application/json" in (request.headers.get("accept") or "") or request.query_params.get("json"):
        return JSONResponse(_json(w.name, done, st))
    return RedirectResponse(f"/p/{token}?done={done}", status_code=303)


@router.get("/p/{token}/toggle")
def punch_toggle_get(request: Request, token: str):
    """아이폰 '단축어' 용 — 호출 한 번으로 출근/퇴근 전환 후 결과 문장 반환"""
    from routes_attendance import do_checkin, do_checkout, open_shift
    with Session(engine, expire_on_commit=False) as s:
        u, w = _by_token(s, token)
        if open_shift(s, w.id):
            done = "out" if do_checkout(s, w.id) == "ok" else "none"
        else:
            done = "in" if do_checkin(s, w.id) == "ok" else "dup"
        st = _state(s, w)
    d = _json(w.name, done, st)
    if request.query_params.get("json"):
        return JSONResponse(d)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(d["message"])


@router.get("/p/{token}/status")
def punch_status(request: Request, token: str):
    with Session(engine, expire_on_commit=False) as s:
        u, w = _by_token(s, token)
        st = _state(s, w)
    return JSONResponse(_json(w.name, "", st))


def _json(name, done, st):
    t = datetime.now().strftime("%H:%M")
    msg = {
        "in": f"{name}님 출근 완료 ({t})",
        "out": f"{name}님 퇴근 완료 ({t})" + (f" · {st['check_in']:%m/%d %H:%M} 출근" if st.get("check_in") else ""),
        "dup": f"{name}님은 이미 근무 중입니다",
        "none": f"{name}님은 출근 기록이 없습니다",
        "": (f"{name}님 근무 중 · {st['check_in']:%H:%M} 출근" if st["working"] else f"{name}님 대기 중"),
    }[done]
    return {"ok": done in ("in", "out", ""), "result": done, "message": msg,
            "working": st["working"],
            "check_in": st["check_in"].isoformat() if st.get("check_in") else None,
            "check_out": st["check_out"].isoformat() if st.get("check_out") else None}


@router.get("/p/{token}/manifest.json")
def punch_manifest(request: Request, token: str):
    """개인 출퇴근 버튼을 '홈 화면에 추가'하면 독립 아이콘(앱처럼)으로 열리도록"""
    with Session(engine, expire_on_commit=False) as s:
        u, w = _by_token(s, token)
    return JSONResponse({
        "name": f"출퇴근 · {w.name}", "short_name": "출퇴근",
        "start_url": f"/p/{token}", "scope": f"/p/{token}", "id": f"/p/{token}",
        "display": "standalone", "orientation": "portrait",
        "background_color": "#0B1220", "theme_color": "#0B1220", "lang": "ko-KR",
        "icons": [
            {"src": "/static/icon-punch-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/static/icon-punch-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, media_type="application/manifest+json")
