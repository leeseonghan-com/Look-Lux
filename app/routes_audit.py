"""활동 기록(감사 로그) 화면 — 대표자(관리자) 전용: 조회 · 되돌리기"""
import json
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select as sa_select, update as sa_update, func, or_
from sqlmodel import Session

from database import engine, User
from template_utils import templates
import audit
from audit import audit_table as T

router = APIRouter()

ACTION_LABELS = {"create": "등록", "update": "수정", "delete": "삭제", "login": "로그인", "logout": "로그아웃", "revert": "되돌리기"}

FIELD_LABELS = {
    "name": "이름", "title": "제목", "code": "코드", "status": "진행 상태", "settlement_status": "결제 상태",
    "event_date": "시작일", "event_end_date": "종료일", "start_date": "시작일", "end_date": "종료일",
    "location": "장소", "revenue": "매출(총액)", "supply_amount": "공급가", "vat_amount": "부가세", "vat_mode": "부가세 방식",
    "amount": "금액", "vendor_id": "거래처", "project_id": "프로젝트", "worker_id": "근무자", "folder_id": "폴더",
    "pay_status": "지급 상태", "pay_date": "지급일", "due_date": "지급 예정일", "paid_date": "입금일",
    "work_date": "근무일", "days": "근무일수", "daily_wage": "일당", "total_wage": "급여 합계", "bonus_amount": "추가금액",
    "bonus_memo": "추가금 메모", "check_in": "출근", "check_out": "퇴근", "memo": "메모", "special_notes": "특이사항",
    "phone": "연락처", "account": "계좌", "bank": "은행", "resident_no": "주민번호", "monthly_salary": "월급",
    "default_daily_wage": "기본 일당", "employment_status": "재직 상태", "resign_date": "퇴사일", "hire_date": "입사일",
    "quote_number": "견적번호", "total": "견적 합계", "subtotal": "소계", "discount": "할인", "vat": "부가세",
    "unit_price": "단가", "quantity": "수량", "permissions_json": "권한", "role": "역할", "is_active": "사용 여부",
    "password_hash": "비밀번호", "is_offer": "오퍼", "person_name": "이름", "is_deleted": "휴지통", "kind": "종류",
    "categories": "카테고리", "payment_method": "결제수단", "invoice_date": "계산서 발행일", "settle_due_date": "정산 예정일",
    "category": "역할/항목", "description": "내용", "username": "아이디", "employee_type": "구분", "position": "직급",
}


def _admin(request: Request):
    u = getattr(request.state, "user", None)
    if not u:
        raise HTTPException(303, headers={"Location": "/login?next=/audit"})
    from permissions import is_admin
    if not is_admin(u):
        raise HTTPException(403, "활동 기록은 대표자(관리자)만 볼 수 있습니다.")
    return u


def _fmt_changes(r):
    """화면 표시용 변경 내역 [(칸 이름, 이전, 이후)]"""
    try:
        b = json.loads(r["before_json"] or "{}")
        a = json.loads(r["after_json"] or "{}")
    except Exception:
        return []
    out = []
    if r["action"] == "update":
        for k in (r["changed"] or "").split(","):
            if not k:
                continue
            if k == "password_hash":
                out.append(("비밀번호", "", "변경됨"))
                continue
            out.append((FIELD_LABELS.get(k, k), audit.show(b.get(k)), audit.show(a.get(k))))
    else:
        src = a if r["action"] == "create" else b
        for k, v in src.items():
            if k in ("id",) or v in (None, "", 0, False) or k in audit.MASK_COLS:
                continue
            if k.endswith("_json") or k in ("stored_key", "thumb_key", "storage", "items_json"):
                continue
            out.append((FIELD_LABELS.get(k, k), "", audit.show(v)))
            if len(out) >= 8:
                break
    return out


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, who: str = "", action: str = "", table: str = "", q: str = "",
               d1: str = "", d2: str = "", auto: int = 0, page: int = 1):
    user = _admin(request)
    page = max(1, page)
    per = 60  # 묶음 단위
    conds = []
    if who.isdigit():
        conds.append(T.c.user_id == int(who))
    if action in ACTION_LABELS:
        conds.append(T.c.action == action)
    if table:
        conds.append(T.c.table_name == table)
    if q.strip():
        like = f"%{q.strip()}%"
        conds.append(or_(T.c.label.ilike(like), T.c.user_name.ilike(like), T.c.after_json.ilike(like), T.c.before_json.ilike(like)))
    try:
        if d1:
            conds.append(T.c.created_at >= datetime.strptime(d1, "%Y-%m-%d"))
        if d2:
            conds.append(T.c.created_at < datetime.strptime(d2, "%Y-%m-%d") + timedelta(days=1))
    except ValueError:
        pass
    if not auto:
        conds.append(or_(T.c.auto == False, T.c.auto.is_(None)))  # noqa: E712

    with engine.connect() as conn:
        gq = sa_select(T.c.group_id, func.max(T.c.id).label("mx")).where(*conds).group_by(T.c.group_id) \
            .order_by(func.max(T.c.id).desc()).limit(per + 1).offset((page - 1) * per)
        groups = conn.execute(gq).fetchall()
        has_next = len(groups) > per
        gids = [g.group_id for g in groups[:per]]
        rows = []
        if gids:
            rows = [dict(r._mapping) for r in conn.execute(
                sa_select(T).where(T.c.group_id.in_(gids), *conds).order_by(T.c.id)).fetchall()]
        tables = [r[0] for r in conn.execute(sa_select(T.c.table_name).distinct()).fetchall() if r[0]]
        today_n = conn.execute(sa_select(func.count()).select_from(T).where(
            T.c.created_at >= datetime.combine(date.today(), datetime.min.time()),
            or_(T.c.auto == False, T.c.auto.is_(None)), T.c.action.in_(["create", "update", "delete"]))).scalar()  # noqa: E712

    by_g = {}
    for r in rows:
        r["changes"] = _fmt_changes(r)
        r["tlabel"] = audit.TABLE_LABELS.get(r["table_name"], r["table_name"] or "")
        r["alabel"] = ACTION_LABELS.get(r["action"], r["action"])
        r["revertable"] = r["action"] in ("create", "update", "delete") and not r["reverted_by"] and bool(audit.model_for(r["table_name"]))
        by_g.setdefault(r["group_id"], []).append(r)
    glist = []
    for gid in gids:
        recs = by_g.get(gid, [])
        if not recs:
            continue
        f = recs[0]
        glist.append({"id": gid, "at": f["created_at"], "who": f["user_name"] or "시스템", "path": f["path"],
                      "note": f["note"], "recs": recs, "auto": f["auto"],
                      "revertable": any(x["revertable"] for x in recs),
                      "reverted": all(x["reverted_by"] for x in recs if x["action"] in ("create", "update", "delete")) and any(x["action"] in ("create", "update", "delete") for x in recs)})
    with Session(engine) as s:
        users = s.exec(__import__("sqlmodel").select(User).order_by(User.name)).all()
    return templates.TemplateResponse(request, "audit.html", {
        "user": user, "groups": glist, "users": users, "tables": tables, "TL": audit.TABLE_LABELS, "AL": ACTION_LABELS,
        "f": {"who": who, "action": action, "table": table, "q": q, "d1": d1, "d2": d2, "auto": auto},
        "page": page, "has_next": has_next, "today_n": today_n,
    })


def _load(conn, ids=None, group_id=None):
    stmt = sa_select(T)
    if group_id:
        stmt = stmt.where(T.c.group_id == group_id)
    if ids:
        stmt = stmt.where(T.c.id.in_(ids))
    return [dict(r._mapping) for r in conn.execute(stmt.order_by(T.c.id.desc())).fetchall()]


@router.post("/api/audit/revert")
async def audit_revert(request: Request):
    """묶음(group_id) 또는 기록 1건(id) 되돌리기. preview=true 면 확인만."""
    user = _admin(request)
    body = await request.json()
    gid = (body.get("group_id") or "").strip()
    rid = body.get("id")
    preview = bool(body.get("preview"))
    with engine.connect() as conn:
        recs = _load(conn, group_id=gid) if gid else _load(conn, ids=[int(rid)]) if str(rid).isdigit() else []
    recs = [r for r in recs if r["action"] in ("create", "update", "delete") and not r["reverted_by"]]
    if not recs:
        return JSONResponse({"ok": False, "msg": "되돌릴 내용이 없습니다(이미 되돌렸거나 로그인 기록)."}, status_code=400)
    problems, warns = [], []
    with Session(engine) as s:
        for r in recs:
            p = audit.plan_revert(s, r)
            nm = f"{audit.TABLE_LABELS.get(r['table_name'], r['table_name'])} '{r['label'] or r['row_id']}'"
            if not p["ok"]:
                problems.append(f"{nm}: {p['msg']}")
            elif p.get("warn"):
                ws = [FIELD_LABELS.get(w, w) for w in p["warn"]]
                warns.append(f"{nm}: 이후에 다시 바뀐 칸이 있음 → {', '.join(ws)}")
    if problems:
        return JSONResponse({"ok": False, "msg": "되돌릴 수 없는 항목이 있습니다.\n- " + "\n- ".join(problems)}, status_code=400)
    if preview:
        return JSONResponse({"ok": True, "n": len(recs), "warn": warns})

    # 실제 되돌리기 — 하나의 묶음으로 기록 (누가 되돌렸는지 남음)
    import uuid
    new_gid = uuid.uuid4().hex[:16]
    a = audit.current_actor() or {}
    audit.set_actor(user, path=a.get("path", "/api/audit/revert"), method="POST", ip=a.get("ip", ""),
                    note=f"되돌리기: {recs[-1]['created_at']:%m/%d %H:%M} {recs[-1]['user_name']}의 작업", group_id=new_gid)
    try:
        with Session(engine) as s:
            for r in recs:  # 최근 것부터 거꾸로
                audit.apply_revert(s, r)
                s.flush()
            s.commit()
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"되돌리기 실패: {type(e).__name__} — {str(e)[:200]}"}, status_code=500)
    with engine.begin() as conn:
        mark = conn.execute(sa_select(func.min(T.c.id)).where(T.c.group_id == new_gid)).scalar() or -1
        conn.execute(sa_update(T).where(T.c.id.in_([r["id"] for r in recs])).values(reverted_by=mark))
    return JSONResponse({"ok": True, "n": len(recs)})
