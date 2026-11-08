"""자동 백업 — 배포·장애로 데이터가 날아가도 되살릴 수 있게

- 서버 시작 직후 1번 + 6시간마다 DB 전체를 스냅샷(압축)으로 저장
- 저장 위치: ① 서버 영구 저장공간(DATA_DIR/auto_backups, 최근 14개) ② R2가 연결돼 있으면 R2에도(_backups/, 최근 60개)
  → 서버 저장공간이 초기화돼도 R2 백업으로 복원 가능
- 데이터가 하나도 없는 상태(초기화 직후)는 백업하지 않음 → 좋은 백업을 빈 백업으로 덮어쓰지 않음
"""
import gzip
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from database import DATA_DIR, DB_PATH, engine

LOCAL_DIR = Path(DATA_DIR) / "auto_backups"
LOCAL_KEEP = 14
R2_PREFIX = "_backups/"
R2_KEEP = 60
INTERVAL_SEC = 6 * 3600
_started = False
_lock = threading.Lock()
last = {"at": None, "ok": None, "msg": ""}


def _store():
    try:
        from routes_files import store
        return store if store.s3 else None
    except Exception:
        return None


def db_counts(path=DB_PATH) -> dict:
    """주요 데이터 개수 (백업 목록·빈 DB 판단용)"""
    out = {}
    try:
        con = sqlite3.connect(path)
        for t, label in (("project", "프로젝트"), ("quote", "견적서"), ("expense", "발주"), ("attendance", "근무"),
                         ("worker", "근무자"), ("vendor", "거래처"), ("fileitem", "자료")):
            try:
                out[label] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                pass
        con.close()
    except Exception:
        pass
    return out


def is_empty(counts=None) -> bool:
    c = counts if counts is not None else db_counts()
    return sum(c.get(k, 0) for k in ("프로젝트", "견적서", "발주", "근무", "근무자", "거래처", "자료")) == 0


def snapshot(reason="자동") -> dict:
    """지금 DB를 압축 백업. 반환: {ok, name, msg}"""
    with _lock:
        try:
            if not Path(DB_PATH).exists():
                return {"ok": False, "msg": "DB 파일이 없습니다."}
            counts = db_counts()
            if is_empty(counts):
                last.update(at=datetime.now(), ok=False, msg="데이터가 비어 있어 백업을 건너뜀(좋은 백업 보호)")
                return {"ok": False, "msg": last["msg"]}
            LOCAL_DIR.mkdir(parents=True, exist_ok=True)
            name = f"app_{datetime.now():%Y%m%d_%H%M%S}.db.gz"
            tmp = Path(tempfile.mkdtemp())
            try:
                raw = tmp / "app.db"
                src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(str(raw))
                src.backup(dst)          # 사용 중에도 안전하게 복사
                dst.close()
                src.close()
                out = LOCAL_DIR / name
                with open(raw, "rb") as fi, gzip.open(out, "wb", compresslevel=6) as fo:
                    shutil.copyfileobj(fi, fo)
                where = ["서버"]
                st = _store()
                if st:
                    try:
                        st.s3.upload_file(str(out), st.bucket, R2_PREFIX + name,
                                          ExtraArgs={"ContentType": "application/gzip"})
                        where.append("R2")
                        _prune_r2(st)
                    except Exception as e:
                        print(f"[BACKUP] R2 백업 업로드 실패: {e}")
                _prune_local()
            finally:
                shutil.rmtree(tmp, True)
            summary = ", ".join(f"{k} {v}" for k, v in counts.items() if v)
            last.update(at=datetime.now(), ok=True, msg=f"{reason} 백업 완료 → {'+'.join(where)} ({summary})")
            print(f"[BACKUP] {last['msg']}")
            return {"ok": True, "name": name, "msg": last["msg"]}
        except Exception as e:
            last.update(at=datetime.now(), ok=False, msg=f"백업 실패: {e}")
            print(f"[BACKUP] {last['msg']}")
            return {"ok": False, "msg": last["msg"]}


def _prune_local():
    fs = sorted(LOCAL_DIR.glob("app_*.db.gz"))
    for f in fs[:-LOCAL_KEEP]:
        f.unlink(missing_ok=True)


def _prune_r2(st):
    try:
        objs = st.s3.list_objects_v2(Bucket=st.bucket, Prefix=R2_PREFIX).get("Contents", [])
        objs = sorted(objs, key=lambda o: o["Key"])
        for o in objs[:-R2_KEEP]:
            st.s3.delete_object(Bucket=st.bucket, Key=o["Key"])
    except Exception as e:
        print(f"[BACKUP] R2 정리 실패: {e}")


def list_backups() -> list:
    rows = {}
    if LOCAL_DIR.exists():
        for f in LOCAL_DIR.glob("app_*.db.gz"):
            rows.setdefault(f.name, {"name": f.name, "size": f.stat().st_size, "local": False, "r2": False})["local"] = True
    st = _store()
    if st:
        try:
            for o in st.s3.list_objects_v2(Bucket=st.bucket, Prefix=R2_PREFIX).get("Contents", []):
                nm = o["Key"][len(R2_PREFIX):]
                if nm.startswith("app_") and nm.endswith(".db.gz"):
                    r = rows.setdefault(nm, {"name": nm, "size": o.get("Size", 0), "local": False, "r2": False})
                    r["r2"] = True
        except Exception as e:
            print(f"[BACKUP] R2 목록 실패: {e}")
    out = []
    for r in rows.values():
        try:
            r["at"] = datetime.strptime(r["name"][4:19], "%Y%m%d_%H%M%S")
        except ValueError:
            r["at"] = None
        out.append(r)
    return sorted(out, key=lambda r: r["name"], reverse=True)


def _fetch(name) -> Path:
    if "/" in name or ".." in name or not (name.startswith("app_") and name.endswith(".db.gz")):
        raise ValueError("잘못된 백업 이름")
    loc = LOCAL_DIR / name
    tmp = Path(tempfile.mkdtemp()) / name
    if loc.exists():
        shutil.copy2(loc, tmp)
    else:
        st = _store()
        if not st:
            raise FileNotFoundError("백업 파일을 찾을 수 없습니다.")
        st.s3.download_file(st.bucket, R2_PREFIX + name, str(tmp))
    return tmp


def peek(name) -> dict:
    """복원 전 미리보기 — 그 백업에 들어 있는 데이터 개수"""
    gz = _fetch(name)
    try:
        raw = gz.with_suffix("")
        with gzip.open(gz, "rb") as fi, open(raw, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        return db_counts(str(raw))
    finally:
        shutil.rmtree(gz.parent, True)


def restore(name) -> dict:
    """백업으로 되돌리기 — 지금 상태도 먼저 백업해 둠(되돌리기의 되돌리기 가능)"""
    gz = _fetch(name)
    try:
        raw = gz.with_suffix("")
        with gzip.open(gz, "rb") as fi, open(raw, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        con = sqlite3.connect(str(raw))
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        if ok != "ok":
            return {"ok": False, "msg": "백업 파일이 손상되어 복원할 수 없습니다."}
        if not is_empty():
            snapshot("복원 직전")
        with _lock:
            engine.dispose()
            src, dst = sqlite3.connect(str(raw)), sqlite3.connect(DB_PATH)
            src.backup(dst)
            dst.close()
            src.close()
            engine.dispose()
        try:
            from database import init_db
            init_db()      # 예전 백업이면 새 칸 자동 추가
        except Exception as e:
            print(f"[BACKUP] 복원 후 정리 경고: {e}")
        try:
            import routes_files
            routes_files._MIGRATED = False
        except Exception:
            pass
        return {"ok": True, "msg": f"{name} 시점으로 복원했습니다.", "counts": db_counts()}
    finally:
        shutil.rmtree(gz.parent, True)


def _loop():
    time.sleep(20)          # 서버가 완전히 뜬 뒤
    snapshot("서버 시작")
    while True:
        time.sleep(INTERVAL_SEC)
        snapshot("정기")


def start():
    global _started
    if _started or os.environ.get("AUTOBACKUP_OFF"):
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="autobackup").start()
