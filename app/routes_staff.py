"""프로젝트 투입 인력 배치 — 월/주 단위 달력(타임라인) + 중복 배치 표시

- 한 사람이 여러 프로젝트에 동시에 배치될 수 있음 (막지 않고 ⚠ 로 표시만)
- 근무자(Worker) 선택 또는 이름 직접 입력 모두 가능
"""
import calendar
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from database import engine, Project, Worker, User, ProjectStaff, STAFF_ROLES, CalendarEvent, EVENT_KINDS
from template_utils import templates

router = APIRouter()

PALETTE = [
    ("#DBEAFE", "#1E40AF"), ("#DCFCE7", "#166534"), ("#FEF3C7", "#92400E"),
    ("#FCE7F3", "#9D174D"), ("#EDE9FE", "#5B21B6"), ("#CFFAFE", "#155E75"),
    ("#FFEDD5", "#9A3412"), ("#E0E7FF", "#3730A3"), ("#F1F5F9", "#334155"),
    ("#FEE2E2", "#991B1B"),
]
WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        u = s.get(User, uid)
    from permissions import has_permission
    if not has_permission(u, "projects"):
        raise HTTPException(403, "프로젝트 접근 권한이 없습니다.")
    return u


def _d(v: str) -> Optional[date]:
    try:
        return datetime.strptime((v or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _color(pid: int):
    return PALETTE[(pid or 0) % len(PALETTE)]


def _proj_end(p) -> Optional[date]:
    if not p.event_date:
        return None
    e = getattr(p, "event_end_date", None)
    return e if e and e > p.event_date else p.event_date


# ────────────────────────────────────────────────────────────
# 달력 / 타임라인
# ────────────────────────────────────────────────────────────
@router.get("/staffing", response_class=HTMLResponse)
def staffing_board(request: Request, view: str = "month", d: str = "", group: str = "project"):
    user = _user(request)
    base = _d(d) or date.today()
    view = "week" if view == "week" else "month"
    group = "person" if group == "person" else "project"

    if view == "week":
        start = base - timedelta(days=base.weekday())
        end = start + timedelta(days=6)
        prev_d, next_d = start - timedelta(days=7), start + timedelta(days=7)
        title = f"{start.year}년 {start.month}월 {start.day}일 ~ {end.month}월 {end.day}일"
    else:
        start = base.replace(day=1)
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        prev_d = (start - timedelta(days=1)).replace(day=1)
        next_d = end + timedelta(days=1)
        title = f"{start.year}년 {start.month}월"

    days = []
    cur = start
    today = date.today()
    while cur <= end:
        days.append({"date": cur, "day": cur.day, "wd": WEEKDAYS[cur.weekday()],
                     "weekend": cur.weekday() >= 5, "today": cur == today})
        cur += timedelta(days=1)
    idx = {x["date"]: i for i, x in enumerate(days)}

    with Session(engine, expire_on_commit=False) as s:
        rows_raw = s.exec(
            select(ProjectStaff).where(ProjectStaff.start_date <= end, ProjectStaff.end_date >= start)
        ).all()
        pids = {r.project_id for r in rows_raw}
        projects = {p.id: p for p in s.exec(select(Project).where(Project.id.in_(pids))).all()} if pids else {}
        # 이 기간 행사 (배치 없는 것도 표시)
        period_projects = [p for p in s.exec(select(Project).where(Project.event_date != None)).all()
                           if p.status != "취소" and p.event_date <= end and (_proj_end(p) or p.event_date) >= start]
        for p in period_projects:
            projects.setdefault(p.id, p)

    # 사람별 일자 점유 → 중복 배치 판정
    def pkey(r):
        return f"w{r.worker_id}" if r.worker_id else f"n{(r.person_name or '').strip()}"

    occupancy = {}  # (person_key, date) -> set(project_id)
    for r in rows_raw:
        c = max(r.start_date, start)
        while c <= min(r.end_date, end):
            occupancy.setdefault((pkey(r), c), set()).add(r.project_id)
            c += timedelta(days=1)

    def make_bar(r, label):
        s0, e0 = max(r.start_date, start), min(r.end_date, end)
        bg, fg = _color(r.project_id)
        conflict_days = []
        c = s0
        while c <= e0:
            if len(occupancy.get((pkey(r), c), ())) > 1:
                conflict_days.append(c)
            c += timedelta(days=1)
        return {
            "id": r.id, "col": idx[s0] + 1, "span": (e0 - s0).days + 1,
            "label": label, "bg": bg, "fg": fg, "offer": bool(getattr(r, "is_offer", False)),
            "project_id": r.project_id, "conflict": bool(conflict_days),
            "tip": f"{label} · {r.start_date:%m/%d}~{r.end_date:%m/%d}" + (" · 오퍼" if getattr(r, "is_offer", False) else "")
                   + (" · ⚠ 같은 날 다른 프로젝트와 중복" if conflict_days else ""),
        }

    groups = []
    if group == "person":
        by = {}
        for r in rows_raw:
            by.setdefault(pkey(r), {"name": r.person_name or "?", "items": []})["items"].append(r)
        for k, g in sorted(by.items(), key=lambda kv: kv[1]["name"]):
            items = sorted(g["items"], key=lambda r: r.start_date)
            lanes = []  # 겹치는 배치는 줄을 나눠 표시
            for r in items:
                p = projects.get(r.project_id)
                bar = make_bar(r, p.name if p else "(삭제된 프로젝트)")
                for lane in lanes:
                    if lane[-1]["_end"] < r.start_date:
                        bar["_end"] = r.end_date; lane.append(bar); break
                else:
                    bar["_end"] = r.end_date; lanes.append([bar])
            n_days = len({c for (pk, c) in occupancy if pk == k})
            groups.append({"name": g["name"], "sub": f"{n_days}일 투입", "lanes": lanes,
                           "conflict": any(b["conflict"] for ln in lanes for b in ln)})
    else:
        by = {}
        for r in rows_raw:
            by.setdefault(r.project_id, []).append(r)
        order = sorted(projects.values(), key=lambda p: (p.event_date or date.max, p.id))
        for p in order:
            items = sorted(by.get(p.id, []), key=lambda r: (r.start_date, r.person_name))
            bg, fg = _color(p.id)
            ev = None
            if p.event_date:
                s0, e0 = max(p.event_date, start), min(_proj_end(p), end)
                if s0 <= e0:
                    ev = {"col": idx[s0] + 1, "span": (e0 - s0).days + 1, "bg": bg, "fg": fg}
            lanes = [[dict(make_bar(r, (r.person_name or "?")), _end=r.end_date)]
                     for r in items]
            groups.append({"name": p.name, "sub": f"{len({pkey(r) for r in items})}명", "lanes": lanes,
                           "event": ev, "project_id": p.id, "color": (bg, fg),
                           "conflict": any(b["conflict"] for ln in lanes for b in ln)})

    # 일자별 투입 인원 수
    day_counts = []
    for x in days:
        day_counts.append(len({pk for (pk, c) in occupancy if c == x["date"]}))

    legend = []
    for p in sorted(projects.values(), key=lambda p: (p.event_date or date.max)):
        bg, fg = _color(p.id)
        legend.append({"id": p.id, "name": p.name, "bg": bg, "fg": fg,
                       "period": f"{p.event_date:%m/%d}" + (f"~{_proj_end(p):%m/%d}" if p.event_date and _proj_end(p) != p.event_date else "") if p.event_date else "일정미정"})

    total_conflicts = sum(1 for v in occupancy.values() if len(v) > 1)
    with Session(engine, expire_on_commit=False) as s:
        allp = [p for p in s.exec(select(Project)).all() if p.status != "취소"]
    lo, hi = start - timedelta(days=45), end + timedelta(days=45)
    pick = sorted([p for p in allp if not p.event_date or lo <= p.event_date <= hi],
                  key=lambda p: (p.event_date is None, p.event_date or date.max))
    pick_projects = [{"id": p.id, "name": p.name,
                      "period": (f"{p.event_date:%m/%d}" + (f"~{_proj_end(p):%m/%d}" if _proj_end(p) != p.event_date else "")) if p.event_date else "일정미정"}
                     for p in pick]
    return templates.TemplateResponse(request, "staffing.html", {
        "user": user, "view": view, "group": group, "title": title,
        "days": days, "day_counts": day_counts, "groups": groups, "legend": legend,
        "prev_d": prev_d.isoformat(), "next_d": next_d.isoformat(), "today": today.isoformat(),
        "base": base.isoformat(), "total_conflicts": total_conflicts,
        "n_people": len({pkey(r) for r in rows_raw}),
        "pick_projects": pick_projects,
    })


# ────────────────────────────────────────────────────────────
# 배치 등록 / 삭제
# ────────────────────────────────────────────────────────────
@router.post("/projects/{pid}/staff")
async def staff_add(request: Request, pid: int):
    _user(request)
    form = await request.form()
    st = _d(form.get("start_date", ""))
    en = _d(form.get("end_date", "")) or st
    role = (form.get("role") or "").strip()
    memo = (form.get("memo") or "").strip()
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404)
        if not st:
            st = p.event_date or date.today()
            en = _proj_end(p) or st
        if en < st:
            st, en = en, st
        names = []
        for wid in form.getlist("worker_ids"):
            try:
                w = s.get(Worker, int(wid))
            except (TypeError, ValueError):
                w = None
            if w:
                names.append((w.id, w.name))
        for nm in (form.get("person_names") or "").replace("\n", ",").split(","):
            nm = nm.strip()
            if nm:
                names.append((None, nm))
        for wid, nm in names:
            s.add(ProjectStaff(project_id=pid, worker_id=wid, person_name=nm, role=role,
                               start_date=st, end_date=en, memo=memo))
        s.commit()
    back = form.get("back") or f"/projects/{pid}#staff"
    return RedirectResponse(back if back.startswith("/") else f"/projects/{pid}", status_code=303)


@router.post("/staff/{sid}/delete")
async def staff_delete(request: Request, sid: int):
    _user(request)
    form = await request.form()
    with Session(engine, expire_on_commit=False) as s:
        r = s.get(ProjectStaff, sid)
        pid = r.project_id if r else None
        if r:
            s.delete(r)
            s.commit()
    back = form.get("back") or (f"/projects/{pid}#staff" if pid else "/staffing")
    return RedirectResponse(back if back.startswith("/") else "/staffing", status_code=303)


@router.post("/staff/{sid}/edit")
async def staff_edit(request: Request, sid: int):
    _user(request)
    form = await request.form()
    with Session(engine, expire_on_commit=False) as s:
        r = s.get(ProjectStaff, sid)
        if not r:
            raise HTTPException(404)
        st, en = _d(form.get("start_date", "")), _d(form.get("end_date", ""))
        if st:
            r.start_date = st
        if en:
            r.end_date = en
        if r.end_date < r.start_date:
            r.start_date, r.end_date = r.end_date, r.start_date
        r.role = (form.get("role") or r.role or "").strip()
        s.add(r)
        s.commit()
        pid = r.project_id
    back = form.get("back") or f"/projects/{pid}#staff"
    return RedirectResponse(back if back.startswith("/") else "/staffing", status_code=303)


def project_staff_context(session, p):
    """프로젝트 상세 화면용 — 배치 목록 + 선택 가능한 근무자 + 다른 프로젝트 중복 경고"""
    rows = session.exec(select(ProjectStaff).where(ProjectStaff.project_id == p.id)
                        .order_by(ProjectStaff.start_date, ProjectStaff.person_name)).all()
    workers = session.exec(select(Worker).where(Worker.is_active == True).order_by(Worker.name)).all()
    out = []
    for r in rows:
        q = select(ProjectStaff).where(ProjectStaff.id != r.id, ProjectStaff.project_id != p.id,
                                       ProjectStaff.start_date <= r.end_date, ProjectStaff.end_date >= r.start_date)
        q = q.where(ProjectStaff.worker_id == r.worker_id) if r.worker_id else \
            q.where(ProjectStaff.worker_id == None, ProjectStaff.person_name == r.person_name)
        others = session.exec(q).all()
        other_names = []
        for o in others:
            op = session.get(Project, o.project_id)
            if op:
                other_names.append(f"{op.name} ({o.start_date:%m/%d}~{o.end_date:%m/%d})")
        out.append({"id": r.id, "name": r.person_name, "offer": bool(getattr(r, "is_offer", False)),
                    "start": r.start_date, "end": r.end_date,
                    "days": (r.end_date - r.start_date).days + 1,
                    "conflicts": other_names})
    return {
        "staff_rows": out,
        "staff_workers": [{"id": w.id, "name": w.name, "type": w.employee_type} for w in workers],
        "staff_roles": STAFF_ROLES,
        "staff_default_start": p.event_date.isoformat() if p.event_date else date.today().isoformat(),
        "staff_default_end": (_proj_end(p) or p.event_date or date.today()).isoformat(),
    }


# ════════════════════════════════════════════════════════════
# 간편 배치 API — 달력/상세 화면 팝업에서 바로 저장
#   팝업: 행 = 사람, 열 = 프로젝트 날짜, 칸 탭 = 배치/해제
# ════════════════════════════════════════════════════════════
from fastapi.responses import JSONResponse


def _project_days(p):
    if not p.event_date:
        return []
    end = _proj_end(p)
    out, c = [], p.event_date
    while c <= end:
        out.append(c)
        c += timedelta(days=1)
    return out


def _to_ranges(days):
    """날짜 목록 → 연속 구간 [(시작, 끝), ...]"""
    ds = sorted(set(days))
    out = []
    for d in ds:
        if out and (d - out[-1][1]).days == 1:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(a, b) for a, b in out]


@router.get("/api/staffing/project/{pid}")
def staffing_project_get(request: Request, pid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404)
        rows = s.exec(select(ProjectStaff).where(ProjectStaff.project_id == pid)).all()
        people = {}
        for r in rows:
            k = f"w{r.worker_id}" if r.worker_id else f"n{r.person_name}"
            e = people.setdefault(k, {"key": k, "worker_id": r.worker_id, "name": r.person_name,
                                      "offer": bool(getattr(r, "is_offer", False)), "days": set()})
            c = r.start_date
            while c <= r.end_date:
                e["days"].add(c)
                c += timedelta(days=1)
            if getattr(r, "is_offer", False):
                e["offer"] = True
        days = set(_project_days(p))
        for e in people.values():
            days |= e["days"]
        if not days:
            days = {date.today()}
        days = sorted(days)
        # 다른 프로젝트 일정 (겹침 표시용): 앞뒤 7일 여유
        lo, hi = days[0] - timedelta(days=7), days[-1] + timedelta(days=7)
        busy = {}
        others = s.exec(select(ProjectStaff).where(ProjectStaff.project_id != pid,
                                                   ProjectStaff.start_date <= hi,
                                                   ProjectStaff.end_date >= lo)).all()
        pnames = {}
        for o in others:
            k = f"w{o.worker_id}" if o.worker_id else f"n{o.person_name}"
            if o.project_id not in pnames:
                op = s.get(Project, o.project_id)
                pnames[o.project_id] = op.name if op else "?"
            c = max(o.start_date, lo)
            while c <= min(o.end_date, hi):
                busy.setdefault(k, {}).setdefault(c.isoformat(), []).append(pnames[o.project_id])
                c += timedelta(days=1)
        workers = s.exec(select(Worker).where(Worker.is_active == True).order_by(Worker.name)).all()
        from permissions import is_employed
        return JSONResponse({
            "project": {"id": p.id, "name": p.name,
                        "event_days": [d.isoformat() for d in _project_days(p)]},
            "days": [d.isoformat() for d in days],
            "people": [{**{k: v for k, v in e.items() if k != "days"},
                        "days": sorted(d.isoformat() for d in e["days"])}
                       for e in sorted(people.values(), key=lambda e: e["name"])],
            "workers": [{"id": w.id, "name": w.name, "type": w.employee_type or "알바"}
                        for w in workers if (w.employee_type != "정직원" or is_employed(w))],
            "busy": busy,
        })


@router.post("/api/staffing/project/{pid}")
async def staffing_project_save(request: Request, pid: int):
    """팝업 저장 — 이 프로젝트의 배치를 통째로 교체"""
    _user(request)
    data = await request.json()
    with Session(engine, expire_on_commit=False) as s:
        p = s.get(Project, pid)
        if not p:
            raise HTTPException(404)
        for r in s.exec(select(ProjectStaff).where(ProjectStaff.project_id == pid)).all():
            s.delete(r)
        n = 0
        for person in data.get("people", []):
            name = (person.get("name") or "").strip()
            wid = person.get("worker_id")
            if wid:
                w = s.get(Worker, int(wid))
                if not w:
                    continue
                wid, name = w.id, w.name
            if not name:
                continue
            days = [d for d in (_d(x) for x in person.get("days", [])) if d]
            for a, b in _to_ranges(days):
                s.add(ProjectStaff(project_id=pid, worker_id=wid or None, person_name=name,
                                   is_offer=bool(person.get("offer")), start_date=a, end_date=b))
                n += 1
        s.commit()
    return JSONResponse({"ok": True, "ranges": n})


# ════════════════════════════════════════════════════════════
# 프로젝트 캘린더 (월간) — 여러 날 행사는 막대로 이어서 표시
# ════════════════════════════════════════════════════════════
@router.get("/project-calendar", response_class=HTMLResponse)
def project_calendar(request: Request, d: str = ""):
    user = _user(request)
    from routes_project import unified_status
    base = _d(d) or date.today()
    first = base.replace(day=1)
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    grid_start = first - timedelta(days=first.weekday())          # 월요일 시작
    grid_end = last + timedelta(days=6 - last.weekday())
    can_settle = bool(getattr(request.state, "can_view_settle", False))
    today = date.today()

    with Session(engine, expire_on_commit=False) as s:
        projs = [p for p in s.exec(select(Project).where(Project.event_date != None)).all()
                 if p.event_date <= grid_end and _proj_end(p) >= grid_start]
        staff_cnt = {}
        for r in s.exec(select(ProjectStaff).where(ProjectStaff.project_id.in_([p.id for p in projs]))).all() if projs else []:
            staff_cnt.setdefault(r.project_id, set()).add(f"w{r.worker_id}" if r.worker_id else f"n{r.person_name}")
        undated = s.exec(select(Project).where(Project.event_date == None, Project.status != "취소")).all()
        events = s.exec(select(CalendarEvent).where(CalendarEvent.start_date <= grid_end,
                                                    CalendarEvent.end_date >= grid_start)).all()

    def style_of(p):
        u = unified_status(p)
        if u["key"] in ("미수금", "입금완료") and not can_settle:
            return {"label": "완료", "bg": "#E5E7EB", "fg": "#374151"}
        return {"label": u["label"], "bg": u["bg"], "fg": u["color"]}

    weeks = []
    ws = grid_start
    while ws <= grid_end:
        we = ws + timedelta(days=6)
        days = [{"date": ws + timedelta(days=i), "day": (ws + timedelta(days=i)).day,
                 "out": (ws + timedelta(days=i)).month != first.month,
                 "today": (ws + timedelta(days=i)) == today,
                 "weekend": i >= 5} for i in range(7)]
        items = sorted([p for p in projs if p.event_date <= we and _proj_end(p) >= ws],
                       key=lambda p: (p.event_date, -((_proj_end(p) - p.event_date).days)))
        lanes = []
        # ── 별도 일정 먼저 배치 ──
        for ev in sorted([e for e in events if e.start_date <= we and e.end_date >= ws], key=lambda e: e.start_date):
            s0, e0 = max(ev.start_date, ws), min(ev.end_date, we)
            bar = {"event": True, "id": ev.id, "name": ev.title, "col": (s0 - ws).days + 1, "span": (e0 - s0).days + 1,
                   "cont_l": ev.start_date < ws, "cont_r": ev.end_date > we, "kind": ev.kind,
                   "color": ev.color or "#6366F1", "_s": s0, "_e": e0,
                   "tip": f"[{ev.kind}] {ev.title} · {ev.start_date:%m/%d}" + (f"~{ev.end_date:%m/%d}" if ev.end_date != ev.start_date else "") + (f" · {ev.memo}" if ev.memo else "")}
            for lane in lanes:
                if all(b["_e"] < s0 or b["_s"] > e0 for b in lane):
                    lane.append(bar); break
            else:
                lanes.append([bar])
        for p in items:
            s0, e0 = max(p.event_date, ws), min(_proj_end(p), we)
            st = style_of(p)
            bar = {"id": p.id, "name": p.name, "col": (s0 - ws).days + 1, "span": (e0 - s0).days + 1,
                   "cont_l": p.event_date < ws, "cont_r": _proj_end(p) > we,
                   "status": st["label"], "bg": st["bg"], "fg": st["fg"], "cancel": p.status == "취소",
                   "staff": len(staff_cnt.get(p.id, ())), "_s": s0, "_e": e0,
                   "tip": f"{p.name} · {event_period_label(p)} · {st['label']}" + (f" · 인력 {len(staff_cnt.get(p.id, ()))}명" if staff_cnt.get(p.id) else "")}
            for lane in lanes:
                if all(b["_e"] < s0 or b["_s"] > e0 for b in lane):
                    lane.append(bar); break
            else:
                lanes.append([bar])
        weeks.append({"days": days, "lanes": lanes})
        ws = we + timedelta(days=1)

    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = last + timedelta(days=1)
    month_projs = [p for p in projs if p.event_date <= last and _proj_end(p) >= first]
    return templates.TemplateResponse(request, "project_calendar.html", {
        "user": user, "weeks": weeks, "title": f"{first.year}년 {first.month}월",
        "prev_d": prev_m.isoformat(), "next_d": next_m.isoformat(), "today": today.isoformat(),
        "month_count": len(month_projs), "weekdays": WEEKDAYS,
        "undated": [{"id": p.id, "name": p.name} for p in undated][:30],
        "event_kinds": EVENT_KINDS,
    })


def event_period_label(p) -> str:
    e = _proj_end(p)
    return f"{p.event_date:%m/%d}" + (f"~{e:%m/%d}" if e and e != p.event_date else "")



# ── 별도 일정 등록·수정·삭제 ──
@router.get("/api/calendar-events/{eid}")
def cal_event_get(request: Request, eid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(CalendarEvent, eid)
        if not e:
            raise HTTPException(404)
        return JSONResponse({"id": e.id, "title": e.title, "start_date": e.start_date.isoformat(),
                             "end_date": e.end_date.isoformat(), "kind": e.kind, "memo": e.memo or ""})


@router.post("/calendar-events/save")
async def cal_event_save(request: Request):
    user = _user(request)
    form = await request.form()
    title = (form.get("title") or "").strip()
    st = _d(form.get("start_date", "")) or date.today()
    en = _d(form.get("end_date", "")) or st
    if en < st:
        st, en = en, st
    kind = (form.get("kind") or "일정").strip()
    color = dict(EVENT_KINDS).get(kind, "#6366F1")
    back = form.get("back") or f"/project-calendar?d={st.isoformat()}"
    if not title:
        return RedirectResponse(back, status_code=303)
    with Session(engine, expire_on_commit=False) as s:
        eid = form.get("id")
        e = s.get(CalendarEvent, int(eid)) if eid and str(eid).isdigit() else None
        if not e:
            e = CalendarEvent(title=title, start_date=st, end_date=en, created_by=user.id if user else None)
        e.title, e.start_date, e.end_date, e.kind, e.color = title, st, en, kind, color
        e.memo = (form.get("memo") or "").strip()
        s.add(e)
        s.commit()
    return RedirectResponse(back if back.startswith("/") else "/project-calendar", status_code=303)


@router.post("/calendar-events/{eid}/delete")
async def cal_event_delete(request: Request, eid: int):
    _user(request)
    form = await request.form()
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(CalendarEvent, eid)
        if e:
            s.delete(e)
            s.commit()
    back = form.get("back") or "/project-calendar"
    return RedirectResponse(back if back.startswith("/") else "/project-calendar", status_code=303)


@router.post("/calendar-events/{eid}/to-project")
async def cal_event_to_project(request: Request, eid: int):
    """별도 일정 → 정식 프로젝트로 전환 (등록 화면으로 이어짐)"""
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        e = s.get(CalendarEvent, eid)
        if not e:
            raise HTTPException(404)
    from urllib.parse import quote as _q
    return RedirectResponse(f"/projects/new?date={e.start_date.isoformat()}&end={e.end_date.isoformat()}"
                            f"&name={_q(e.title)}&from_event={e.id}", status_code=303)
