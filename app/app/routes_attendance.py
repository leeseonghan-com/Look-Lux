"""근태 관리 — 정직원(출퇴근 버튼) + 알바(일당 등록) 분리"""
import io
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlmodel import Session, select

from database import engine, Attendance, Worker, WorkerCheckin, Project, User
from permissions import is_admin
from template_utils import templates

router = APIRouter()


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        return s.get(User, uid)


def _parse_date(s: str, default=None) -> Optional[date]:
    """안전한 날짜 파싱 — 잘못된 입력은 default 반환"""
    if not s or not str(s).strip():
        return default
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return default


def _range_for(period: str, from_date: Optional[date], to_date: Optional[date], today: date):
    """기간 필터 — period: '7d', '30d', 'month', 'year', 'custom', 'all'"""
    if period == "custom" and from_date and to_date:
        return from_date, to_date
    if period == "month":
        first = today.replace(day=1)
        return first, today
    if period == "year":
        return today.replace(month=1, day=1), today
    if period == "30d":
        return today - timedelta(days=30), today
    if period == "all":
        return None, None
    # 기본: 7일
    return today - timedelta(days=7), today


@router.get("/attendance", response_class=HTMLResponse)
def attendance_list(
    request: Request,
    project_id: str = "",
    tab: str = "alba",
    period: str = "7d",
    from_date: str = "",
    to_date: str = "",
):
    """근태 메인 — tab=alba (알바 일당) / tab=fulltime (정직원 출퇴근)
    period: 7d / 30d / month / year / custom / all"""
    user = _user(request)
    today = date.today()
    # project_id 안전 파싱 (빈 문자열 → None)
    try:
        project_id = int(project_id) if project_id and str(project_id).strip() else None
    except (ValueError, TypeError):
        project_id = None
    from_d = _parse_date(from_date)
    to_d = _parse_date(to_date)
    range_from, range_to = _range_for(period, from_d, to_d, today)
    with Session(engine, expire_on_commit=False) as s:
        if tab == "fulltime":
            # 정직원: 오늘 출퇴근 현황 + 선택 기간 기록
            workers_q = s.exec(
                select(Worker).where(Worker.is_active == True, Worker.employee_type == "정직원").order_by(Worker.name)
            ).all()
            workers_today = []
            for w in workers_q:
                ci = s.exec(
                    select(WorkerCheckin).where(
                        WorkerCheckin.worker_id == w.id,
                        WorkerCheckin.work_date == today
                    )
                ).first()
                workers_today.append({
                    "id": w.id, "name": w.name, "phone": w.phone,
                    "check_in": ci.check_in if ci else None,
                    "check_out": ci.check_out if ci else None,
                    "ci_id": ci.id if ci else None,
                })

            # 기간 필터링된 기록
            q = select(WorkerCheckin)
            if range_from:
                q = q.where(WorkerCheckin.work_date >= range_from)
            if range_to:
                q = q.where(WorkerCheckin.work_date <= range_to)
            q = q.order_by(WorkerCheckin.work_date.desc(), WorkerCheckin.check_in.desc())
            recent_ci = s.exec(q).all()
            recent_rows = []
            total_hours = 0.0
            for ci in recent_ci:
                w = s.get(Worker, ci.worker_id)
                hrs = None
                if ci.check_in and ci.check_out:
                    hrs = round((ci.check_out - ci.check_in).total_seconds() / 3600, 1)
                    total_hours += hrs
                recent_rows.append({
                    "id": ci.id, "date": ci.work_date,
                    "worker_id": ci.worker_id,
                    "worker_name": w.name if w else "?",
                    "check_in": ci.check_in, "check_out": ci.check_out,
                    "hours": hrs,
                    "memo": ci.memo or "",
                })

            return templates.TemplateResponse(request, "attendance.html", {
                "user": user, "tab": "fulltime",
                "workers_today": workers_today,
                "recent_rows": recent_rows,
                "today": today,
                "period": period,
                "from_date": (range_from.isoformat() if range_from else ""),
                "to_date": (range_to.isoformat() if range_to else ""),
                "total_hours": round(total_hours, 1),
                "is_admin": is_admin(user),
            })

        # 기본: 알바 일당 기록 탭
        q = select(Attendance)
        if range_from:
            q = q.where(Attendance.work_date >= range_from)
        if range_to:
            q = q.where(Attendance.work_date <= range_to)
        if project_id:
            q = q.where(Attendance.project_id == project_id)
        q = q.order_by(Attendance.work_date.desc())
        records = s.exec(q).all()
        rows = []
        for a in records:
            w = s.get(Worker, a.worker_id)
            p = s.get(Project, a.project_id)
            rows.append({
                "id": a.id, "date": a.work_date,
                "worker_name": w.name if w else "?",
                "project_name": p.name if p else "?",
                "days": a.days, "daily_wage": a.daily_wage,
                "total_wage": a.total_wage, "pay_status": a.pay_status,
                "pay_date": a.pay_date, "memo": a.memo,
                "bonus_amount": a.bonus_amount or 0,
                "bonus_memo": a.bonus_memo or "",
            })
        projects = s.exec(select(Project)).all()
        workers = s.exec(
            select(Worker).where(Worker.is_active == True, Worker.employee_type == "알바")
        ).all()
        projects_data = [{"id": p.id, "name": p.name} for p in projects]
        workers_data = [{"id": w.id, "name": w.name, "default_daily_wage": w.default_daily_wage} for w in workers]
        total_amount = sum(r["total_wage"] for r in rows)
        unpaid_amount = sum(r["total_wage"] for r in rows if r["pay_status"] == "미지급")

    return templates.TemplateResponse(request, "attendance.html", {
        "user": user, "tab": "alba", "rows": rows,
        "projects": projects_data, "workers": workers_data,
        "selected_project_id": project_id, "today": date.today(),
        "total_amount": total_amount, "unpaid_amount": unpaid_amount,
        "period": period,
        "from_date": (range_from.isoformat() if range_from else ""),
        "to_date": (range_to.isoformat() if range_to else ""),
        "is_admin": is_admin(user),
    })


# ============================================================
# 정직원 출퇴근 버튼
# ============================================================
@router.post("/attendance/checkin")
def worker_checkin(request: Request, worker_id: int = Form(...)):
    """정직원 출근 — 오늘 날짜에 이미 출근 기록 있으면 무시"""
    _user(request)
    today = date.today()
    now = datetime.now()
    with Session(engine, expire_on_commit=False) as s:
        existing = s.exec(
            select(WorkerCheckin).where(
                WorkerCheckin.worker_id == worker_id,
                WorkerCheckin.work_date == today
            )
        ).first()
        if existing:
            if not existing.check_in:
                existing.check_in = now
                s.add(existing)
        else:
            s.add(WorkerCheckin(worker_id=worker_id, work_date=today, check_in=now))
        s.commit()
    return RedirectResponse("/attendance?tab=fulltime&checkin=1", status_code=303)


@router.post("/attendance/checkout")
def worker_checkout(request: Request, worker_id: int = Form(...)):
    """정직원 퇴근 — 오늘 날짜의 출근 기록에 퇴근 시간 기록"""
    _user(request)
    today = date.today()
    now = datetime.now()
    with Session(engine, expire_on_commit=False) as s:
        existing = s.exec(
            select(WorkerCheckin).where(
                WorkerCheckin.worker_id == worker_id,
                WorkerCheckin.work_date == today
            )
        ).first()
        if existing:
            existing.check_out = now
            s.add(existing)
        else:
            # 출근 기록 없이 퇴근 → 자동 보정
            s.add(WorkerCheckin(worker_id=worker_id, work_date=today, check_out=now))
        s.commit()
    return RedirectResponse("/attendance?tab=fulltime&checkout=1", status_code=303)


@router.get("/attendance/new", response_class=HTMLResponse)
def attendance_new(request: Request, project_id: str = ""):
    """근무 등록 — ?project_id=N 으로 들어오면 해당 프로젝트가 자동 선택됨"""
    try:
        project_id = int(project_id) if project_id and str(project_id).strip() else None
    except (ValueError, TypeError):
        project_id = None
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        projects = s.exec(
            select(Project)
            .where(Project.status.in_(["준비중", "진행중", "완료"]))
            .order_by(Project.created_at.desc(), Project.id.desc())
        ).all()
        workers = s.exec(
            select(Worker)
            .where(Worker.is_active == True)
            .order_by(Worker.created_at.desc(), Worker.id.desc())
        ).all()
        projects_data = [{"id": p.id, "name": p.name, "code": p.code, "event_date": p.event_date} for p in projects]
        workers_data = [{
            "id": w.id, "name": w.name,
            "default_daily_wage": w.default_daily_wage,
            "employee_type": w.employee_type or "알바",
        } for w in workers]
        # 미리 선택할 프로젝트 정보
        preselect = None
        if project_id:
            for p in projects_data:
                if p["id"] == project_id:
                    preselect = p
                    break
    return templates.TemplateResponse(request, "attendance_new.html", {
        "user": user, "projects": projects_data, "workers": workers_data,
        "today": date.today(),
        "preselect_project": preselect,
    })


def _safe_int(v, default=0):
    try:
        return int(str(v).replace(",", "").replace(" ", "").strip() or default)
    except (ValueError, TypeError):
        return default


@router.post("/attendance/new")
def attendance_create(
    request: Request,
    project_id: int = Form(...),
    worker_id: int = Form(...),
    work_date: str = Form(...),
    days: float = Form(1.0),
    daily_wage: str = Form("0"),
    bonus_amount: str = Form("0"),
    bonus_memo: str = Form(""),
    memo: str = Form(""),
):
    user = _user(request)
    wage = _safe_int(daily_wage, 0)
    bonus = _safe_int(bonus_amount, 0)
    with Session(engine, expire_on_commit=False) as s:
        a = Attendance(
            project_id=project_id, worker_id=worker_id,
            work_date=datetime.strptime(work_date, "%Y-%m-%d").date(),
            days=days, daily_wage=wage,
            total_wage=int(days * wage) + bonus,  # 일당 합계 + 추가금액
            bonus_amount=bonus,
            bonus_memo=(bonus_memo or "").strip(),
            memo=memo,
            registered_by=user.id,
        )
        s.add(a)
        s.commit()
    return RedirectResponse("/attendance", status_code=303)


@router.get("/attendance/{aid}/edit", response_class=HTMLResponse)
def attendance_edit(request: Request, aid: int):
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        a = s.get(Attendance, aid)
        if not a:
            raise HTTPException(404)
        a_dict = {
            "id": a.id, "project_id": a.project_id, "worker_id": a.worker_id,
            "work_date": a.work_date, "days": a.days, "daily_wage": a.daily_wage,
            "memo": a.memo, "pay_status": a.pay_status,
            "bonus_amount": a.bonus_amount or 0,
            "bonus_memo": a.bonus_memo or "",
        }
        projects = s.exec(select(Project).order_by(Project.created_at.desc(), Project.id.desc())).all()
        workers = s.exec(select(Worker).order_by(Worker.created_at.desc(), Worker.id.desc())).all()
        projects_data = [{"id": p.id, "name": p.name} for p in projects]
        workers_data = [{"id": w.id, "name": w.name} for w in workers]
    return templates.TemplateResponse(request, "attendance_edit.html", {
        "user": user, "a": a_dict,
        "projects": projects_data, "workers": workers_data,
    })


@router.post("/attendance/{aid}/edit")
def attendance_update(
    request: Request, aid: int,
    project_id: int = Form(...),
    worker_id: int = Form(...),
    work_date: str = Form(...),
    days: float = Form(...),
    daily_wage: str = Form("0"),
    bonus_amount: str = Form("0"),
    bonus_memo: str = Form(""),
    memo: str = Form(""),
):
    _user(request)
    wage = _safe_int(daily_wage, 0)
    bonus = _safe_int(bonus_amount, 0)
    with Session(engine, expire_on_commit=False) as s:
        a = s.get(Attendance, aid)
        if not a:
            raise HTTPException(404)
        a.project_id = project_id
        a.worker_id = worker_id
        a.work_date = datetime.strptime(work_date, "%Y-%m-%d").date()
        a.days = days
        a.daily_wage = wage
        a.bonus_amount = bonus
        a.bonus_memo = (bonus_memo or "").strip()
        a.total_wage = int(days * wage) + bonus  # 일당 합계 + 추가금액
        a.memo = memo
        s.add(a)
        s.commit()
    return RedirectResponse("/attendance", status_code=303)


@router.post("/attendance/{aid}/pay")
def attendance_pay(request: Request, aid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        a = s.get(Attendance, aid)
        if a:
            a.pay_status = "지급완료" if a.pay_status == "미지급" else "미지급"
            a.pay_date = date.today() if a.pay_status == "지급완료" else None
            s.add(a)
            s.commit()
    return RedirectResponse(request.headers.get("referer", "/attendance"), status_code=303)


@router.post("/attendance/{aid}/delete")
def attendance_delete(request: Request, aid: int):
    _user(request)
    with Session(engine, expire_on_commit=False) as s:
        a = s.get(Attendance, aid)
        if a:
            s.delete(a)
            s.commit()
    return RedirectResponse("/attendance", status_code=303)


# ============================================================
# 정직원 출퇴근(WorkerCheckin) 수정 / 삭제
# ============================================================
@router.get("/attendance/checkin/{cid}/edit", response_class=HTMLResponse)
def checkin_edit(request: Request, cid: int):
    """출퇴근 기록 수정 화면 (직원·관리자 모두 가능)"""
    user = _user(request)
    with Session(engine, expire_on_commit=False) as s:
        ci = s.get(WorkerCheckin, cid)
        if not ci:
            raise HTTPException(404, "출퇴근 기록을 찾을 수 없습니다.")
        worker = s.get(Worker, ci.worker_id)
        # 시간을 HH:MM 형식으로 표시하기 쉽게 변환
        ci_dict = {
            "id": ci.id, "worker_id": ci.worker_id,
            "worker_name": worker.name if worker else "?",
            "work_date": ci.work_date,
            "check_in_date": ci.check_in.date() if ci.check_in else ci.work_date,
            "check_in_time": ci.check_in.strftime("%H:%M") if ci.check_in else "",
            "check_out_date": ci.check_out.date() if ci.check_out else ci.work_date,
            "check_out_time": ci.check_out.strftime("%H:%M") if ci.check_out else "",
            "memo": ci.memo or "",
        }
    return templates.TemplateResponse(request, "checkin_edit.html", {
        "user": user, "ci": ci_dict, "is_admin": is_admin(user),
    })


@router.post("/attendance/checkin/{cid}/edit")
def checkin_update(
    request: Request, cid: int,
    work_date: str = Form(""),
    check_in_date: str = Form(""), check_in_time: str = Form(""),
    check_out_date: str = Form(""), check_out_time: str = Form(""),
    memo: str = Form(""),
):
    """출퇴근 시간 수정 — 직원·관리자 모두 가능. 잘못된 입력은 친절히 거부."""
    _user(request)
    parsed_work_date = _parse_date(work_date) or date.today()

    def _combine(d_str: str, t_str: str, default_date: date):
        """날짜 + HH:MM 시간 → datetime. 잘못되면 None"""
        d = _parse_date(d_str) or default_date
        t_str = (t_str or "").strip()
        if not t_str:
            return None
        try:
            hh, mm = t_str.split(":")
            return datetime.combine(d, datetime.min.time()).replace(hour=int(hh), minute=int(mm))
        except (ValueError, TypeError):
            return None

    ci_dt = _combine(check_in_date, check_in_time, parsed_work_date)
    co_dt = _combine(check_out_date, check_out_time, parsed_work_date)
    with Session(engine, expire_on_commit=False) as s:
        ci = s.get(WorkerCheckin, cid)
        if not ci:
            raise HTTPException(404, "출퇴근 기록을 찾을 수 없습니다.")
        ci.work_date = parsed_work_date
        ci.check_in = ci_dt
        ci.check_out = co_dt
        ci.memo = (memo or "").strip()
        s.add(ci)
        s.commit()
    return RedirectResponse("/attendance?tab=fulltime", status_code=303)


@router.post("/attendance/checkin/{cid}/delete")
def checkin_delete(request: Request, cid: int):
    """출퇴근 기록 삭제 — 관리자만 가능"""
    user = _user(request)
    if not is_admin(user):
        raise HTTPException(403, "출퇴근 기록 삭제는 관리자 권한이 필요합니다.")
    with Session(engine, expire_on_commit=False) as s:
        ci = s.get(WorkerCheckin, cid)
        if ci:
            s.delete(ci)
            s.commit()
    return RedirectResponse("/attendance?tab=fulltime", status_code=303)


# ============================================================
# 엑셀 다운로드 — 알바 일당 / 정직원 출퇴근
# ============================================================
def _excel_response(workbook, filename: str) -> StreamingResponse:
    """openpyxl Workbook을 StreamingResponse로 반환"""
    buf = io.BytesIO()
    workbook.save(buf)
    buf.seek(0)
    from urllib.parse import quote
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/attendance/export")
def attendance_export(
    request: Request,
    tab: str = "alba",
    period: str = "month",
    from_date: str = "",
    to_date: str = "",
    project_id: str = "",
):
    """근태 엑셀 다운로드 — 알바 일당 / 정직원 출퇴근"""
    _user(request)
    try:
        project_id = int(project_id) if project_id and str(project_id).strip() else None
    except (ValueError, TypeError):
        project_id = None
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    today = date.today()
    range_from, range_to = _range_for(period, _parse_date(from_date), _parse_date(to_date), today)
    wb = Workbook()
    ws = wb.active
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0A0E1A")
    center = Alignment(horizontal="center", vertical="center")

    with Session(engine, expire_on_commit=False) as s:
        if tab == "fulltime":
            ws.title = "정직원 출퇴근"
            headers = ["날짜", "이름", "출근시각", "퇴근시각", "근무시간(h)", "메모"]
            ws.append(headers)
            for cell in ws[1]:
                cell.font = header_font; cell.fill = header_fill; cell.alignment = center
            q = select(WorkerCheckin)
            if range_from: q = q.where(WorkerCheckin.work_date >= range_from)
            if range_to: q = q.where(WorkerCheckin.work_date <= range_to)
            q = q.order_by(WorkerCheckin.work_date.desc(), WorkerCheckin.check_in.desc())
            for ci in s.exec(q).all():
                w = s.get(Worker, ci.worker_id)
                hrs = ""
                if ci.check_in and ci.check_out:
                    hrs = round((ci.check_out - ci.check_in).total_seconds() / 3600, 2)
                ws.append([
                    ci.work_date.isoformat() if ci.work_date else "",
                    w.name if w else "?",
                    ci.check_in.strftime("%Y-%m-%d %H:%M") if ci.check_in else "",
                    ci.check_out.strftime("%Y-%m-%d %H:%M") if ci.check_out else "",
                    hrs,
                    ci.memo or "",
                ])
            ws.column_dimensions["A"].width = 12
            ws.column_dimensions["B"].width = 12
            ws.column_dimensions["C"].width = 20
            ws.column_dimensions["D"].width = 20
            ws.column_dimensions["E"].width = 12
            ws.column_dimensions["F"].width = 30
            filename = f"정직원_출퇴근_{(range_from or '').__str__()}_{(range_to or '').__str__()}.xlsx"
        else:
            ws.title = "알바 일당"
            headers = ["날짜", "근무자", "프로젝트", "일수", "일당", "추가금액", "추가메모", "총지급액", "지급상태", "지급일", "메모"]
            ws.append(headers)
            for cell in ws[1]:
                cell.font = header_font; cell.fill = header_fill; cell.alignment = center
            q = select(Attendance)
            if range_from: q = q.where(Attendance.work_date >= range_from)
            if range_to: q = q.where(Attendance.work_date <= range_to)
            if project_id: q = q.where(Attendance.project_id == project_id)
            q = q.order_by(Attendance.work_date.desc())
            for a in s.exec(q).all():
                w = s.get(Worker, a.worker_id)
                p = s.get(Project, a.project_id)
                ws.append([
                    a.work_date.isoformat() if a.work_date else "",
                    w.name if w else "?",
                    p.name if p else "?",
                    a.days, a.daily_wage,
                    a.bonus_amount or 0, a.bonus_memo or "",
                    a.total_wage, a.pay_status,
                    a.pay_date.isoformat() if a.pay_date else "",
                    a.memo or "",
                ])
            for col, width in zip("ABCDEFGHIJK", [12,12,18,8,12,12,20,14,12,12,30]):
                ws.column_dimensions[col].width = width
            filename = f"알바_일당_{(range_from or '').__str__()}_{(range_to or '').__str__()}.xlsx"

    return _excel_response(wb, filename)
