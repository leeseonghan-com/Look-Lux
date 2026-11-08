"""활동 기록(감사 로그) — 누가 · 언제 · 무엇을 등록/수정/삭제했는지 자동 기록 + 되돌리기

- 모든 저장(등록·수정·삭제)을 DB 저장 순간에 자동으로 잡아서 기록 (화면마다 코드를 넣지 않아도 됨)
- 한 번의 작업(요청)에서 바뀐 여러 건은 같은 묶음(group)으로 기록 → 묶음 전체 되돌리기
- 기록은 같은 트랜잭션 안에서 쓰므로, 저장이 실패하면 기록도 남지 않음
"""
import contextvars
import json
import uuid
from datetime import datetime, date, timedelta
from typing import Optional

from sqlalchemy import event, inspect, insert, Table, Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.orm import Session as _SASession
from sqlmodel import SQLModel

# ── 현재 작업자 (요청마다 설정) ──
_actor = contextvars.ContextVar("audit_actor", default=None)

TABLE_LABELS = {
    "project": "프로젝트", "quote": "견적서", "quoteitem": "견적 품목", "expense": "하청/외주 발주",
    "attendance": "알바 근무", "workercheckin": "정직원 출퇴근", "worker": "근무자", "vendor": "거래처",
    "user": "직원 계정", "projectstaff": "인력 배치", "calendarevent": "캘린더 일정", "item": "단가표 품목",
    "equipment": "장비", "equipmentunit": "장비 개체", "maintenancelog": "정비 기록", "rentallog": "대여 기록",
    "companysettings": "회사 설정", "filefolder": "자료실 폴더", "fileitem": "자료(파일)",
}
SKIP_TABLES = {"auditlog"}
MASK_COLS = {"password_hash": "(비밀번호)", "punch_token": "(출퇴근 키)"}
IGNORE_COLS = {"created_at"}
LABEL_COLS = ("name", "title", "quote_number", "code", "asset_code", "person_name", "description", "username", "company_name")

metadata = SQLModel.metadata
audit_table = Table(
    "auditlog", metadata,
    Column("id", Integer, primary_key=True),
    Column("group_id", String(40), index=True),
    Column("created_at", DateTime, index=True),
    Column("user_id", Integer, index=True, nullable=True),
    Column("user_name", String(80), default=""),
    Column("action", String(20), index=True),        # create / update / delete / login / revert
    Column("table_name", String(40), index=True),
    Column("row_id", Integer, nullable=True, index=True),
    Column("label", String(200), default=""),
    Column("before_json", Text, default=""),
    Column("after_json", Text, default=""),
    Column("changed", Text, default=""),               # 바뀐 칸 이름(쉼표)
    Column("path", String(200), default=""),
    Column("method", String(8), default=""),
    Column("ip", String(60), default=""),
    Column("auto", Boolean, default=False),            # 화면 조회 중 자동 갱신(상태 자동계산 등)
    Column("reverted_by", Integer, nullable=True),     # 이 기록을 되돌린 '되돌리기' 묶음의 기록 id
    Column("note", String(200), default=""),
    extend_existing=True,
)


def set_actor(user=None, path="", method="", ip="", note="", group_id=None):
    _actor.set({
        "user_id": getattr(user, "id", None), "user_name": (getattr(user, "name", None) or getattr(user, "username", None) or ""),
        "path": path[:200], "method": method, "ip": ip[:60], "note": note,
        "group_id": group_id or uuid.uuid4().hex[:16],
    })


def clear_actor():
    _actor.set(None)


def current_actor():
    return _actor.get()


# ── 값 변환 ──
def _ser(v):
    if isinstance(v, datetime):
        return {"__dt": v.isoformat()}
    if isinstance(v, date):
        return {"__d": v.isoformat()}
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def deser(v):
    if isinstance(v, dict):
        if "__dt" in v:
            return datetime.fromisoformat(v["__dt"])
        if "__d" in v:
            return date.fromisoformat(v["__d"])
    return v


def show(v):
    """화면 표시용 문자열"""
    v = deser(v)
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "예" if v else "아니오"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, int) and abs(v) >= 1000:
        return f"{v:,}"
    s = str(v)
    return s if len(s) <= 120 else s[:117] + "…"


def _cols(obj):
    return [c.key for c in inspect(obj).mapper.column_attrs]


def _snapshot(obj):
    out = {}
    for k in _cols(obj):
        if k in IGNORE_COLS:
            continue
        v = getattr(obj, k, None)
        out[k] = MASK_COLS[k] if (k in MASK_COLS and v) else _ser(v)
    return out


def _label(obj):
    for k in LABEL_COLS:
        v = getattr(obj, k, None)
        if v:
            return str(v)[:200]
    return ""


def _tname(obj):
    t = getattr(obj, "__tablename__", None) or getattr(getattr(obj, "__table__", None), "name", "")
    return str(t)


# ── 저장 순간 자동 기록 ──
@event.listens_for(_SASession, "after_flush")
def _after_flush(session, ctx):
    if session.info.get("audit_off"):
        return
    a = _actor.get() or {"user_id": None, "user_name": "시스템", "path": "", "method": "", "ip": "", "note": "",
                         "group_id": uuid.uuid4().hex[:16]}
    rows = []
    now = datetime.now()
    auto = (a.get("method") == "GET")
    base = {"group_id": a["group_id"], "created_at": now, "user_id": a["user_id"], "user_name": a["user_name"] or "시스템",
            "path": a["path"], "method": a["method"], "ip": a["ip"], "auto": auto, "note": a.get("note", "")}
    try:
        for obj in list(session.new):
            t = _tname(obj)
            if not t or t in SKIP_TABLES:
                continue
            rows.append({**base, "action": "create", "table_name": t, "row_id": getattr(obj, "id", None),
                         "label": _label(obj), "before_json": "", "after_json": json.dumps(_snapshot(obj), ensure_ascii=False), "changed": ""})
        for obj in list(session.dirty):
            t = _tname(obj)
            if not t or t in SKIP_TABLES or not session.is_modified(obj, include_collections=False):
                continue
            st = inspect(obj)
            before, after, changed = {}, {}, []
            for k in _cols(obj):
                if k in IGNORE_COLS:
                    continue
                h = st.attrs[k].history
                if not h.has_changes():
                    continue
                old = h.deleted[0] if h.deleted else None
                new = h.added[0] if h.added else getattr(obj, k, None)
                if old == new:
                    continue
                changed.append(k)
                before[k] = MASK_COLS.get(k, _ser(old)) if k in MASK_COLS else _ser(old)
                after[k] = MASK_COLS.get(k, _ser(new)) if k in MASK_COLS else _ser(new)
            if not changed:
                continue
            rows.append({**base, "action": "update", "table_name": t, "row_id": getattr(obj, "id", None),
                         "label": _label(obj), "before_json": json.dumps(before, ensure_ascii=False),
                         "after_json": json.dumps(after, ensure_ascii=False), "changed": ",".join(changed)})
        for obj in list(session.deleted):
            t = _tname(obj)
            if not t or t in SKIP_TABLES:
                continue
            rows.append({**base, "action": "delete", "table_name": t, "row_id": getattr(obj, "id", None),
                         "label": _label(obj), "before_json": json.dumps(_snapshot(obj), ensure_ascii=False), "after_json": "", "changed": ""})
        if rows:
            session.connection().execute(insert(audit_table), rows)
    except Exception as e:   # 기록 실패가 실제 저장을 막지 않도록
        print(f"[AUDIT] 기록 실패: {e}")


def log_event(engine, action, label="", user=None, note=""):
    """로그인처럼 DB 변경이 없는 활동 기록"""
    a = _actor.get() or {}
    try:
        with engine.begin() as conn:
            conn.execute(insert(audit_table), [{
                "group_id": uuid.uuid4().hex[:16], "created_at": datetime.now(),
                "user_id": getattr(user, "id", None), "user_name": getattr(user, "name", None) or getattr(user, "username", "") or "",
                "action": action, "table_name": "", "row_id": None, "label": label[:200],
                "before_json": "", "after_json": "", "changed": "", "path": a.get("path", ""), "method": a.get("method", ""),
                "ip": a.get("ip", ""), "auto": False, "note": note[:200]}])
    except Exception as e:
        print(f"[AUDIT] 이벤트 기록 실패: {e}")


def purge_old(engine, days=730):
    """2년 지난 기록 정리 (서버 시작 시)"""
    try:
        with engine.begin() as conn:
            conn.execute(audit_table.delete().where(audit_table.c.created_at < datetime.now() - timedelta(days=days)))
    except Exception as e:
        print(f"[AUDIT] 정리 실패: {e}")


# ── 되돌리기 ──
def model_for(table_name):
    def walk(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from walk(sub)
    for c in walk(SQLModel):
        if getattr(c, "__tablename__", None) == table_name and hasattr(c, "__table__"):
            return c
    return None


def plan_revert(session, rec) -> dict:
    """되돌리기 미리 확인: 가능 여부 + 경고(이후 다시 바뀐 칸)"""
    M = model_for(rec["table_name"])
    if not M:
        return {"ok": False, "msg": "되돌릴 수 없는 종류의 기록입니다."}
    if rec["action"] not in ("create", "update", "delete"):
        return {"ok": False, "msg": "로그인 같은 활동은 되돌릴 대상이 아닙니다."}
    if rec.get("reverted_by"):
        return {"ok": False, "msg": "이미 되돌린 기록입니다."}
    obj = session.get(M, rec["row_id"]) if rec["row_id"] else None
    warn = []
    if rec["action"] == "update":
        if not obj:
            return {"ok": False, "msg": "이 항목은 이후에 삭제되었습니다. 삭제 기록을 먼저 되돌리세요."}
        after = json.loads(rec["after_json"] or "{}")
        for k, v in after.items():
            if k in MASK_COLS:
                continue
            if _ser(getattr(obj, k, None)) != v:
                warn.append(k)
        if any(k in MASK_COLS for k in after):
            warn.append("비밀번호·출퇴근 키는 보안상 되돌리지 않습니다")
    elif rec["action"] == "create":
        if not obj:
            return {"ok": False, "msg": "이미 삭제되어 있습니다."}
    elif rec["action"] == "delete":
        if obj:
            return {"ok": False, "msg": "같은 번호의 항목이 이미 다시 있습니다."}
        before = json.loads(rec["before_json"] or "{}")
        if any(k in MASK_COLS for k in before):
            return {"ok": False, "msg": "삭제된 직원 계정은 보안상 되돌릴 수 없습니다. 새로 만들어주세요."}
    return {"ok": True, "warn": warn}


def apply_revert(session, rec):
    M = model_for(rec["table_name"])
    obj = session.get(M, rec["row_id"]) if rec["row_id"] else None
    if rec["action"] == "update":
        before = json.loads(rec["before_json"] or "{}")
        for k, v in before.items():
            if k in MASK_COLS or not hasattr(obj, k):
                continue
            setattr(obj, k, deser(v))
        session.add(obj)
    elif rec["action"] == "create":
        session.delete(obj)
    elif rec["action"] == "delete":
        before = {k: deser(v) for k, v in json.loads(rec["before_json"] or "{}").items() if k not in MASK_COLS}
        cols = set(_cols_of_model(M))
        session.add(M(**{k: v for k, v in before.items() if k in cols}))


def _cols_of_model(M):
    return [c.key for c in inspect(M).column_attrs]
