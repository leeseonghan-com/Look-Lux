"""📁 자료실 — 사진·영상·문서를 프로젝트별로 한곳에 (구글 드라이브 대체)

저장소
  - 기본: 서버 저장공간 DATA_DIR/files  (Railway Volume)
  - 선택: S3 호환 저장소(Cloudflare R2 등) — 환경변수 S3_BUCKET 등을 넣으면 자동 전환
업로드
  - 8MB 조각으로 나눠 전송 → 대용량 영상·끊김에도 이어올리기
  - 폴더째 올리기(하위 폴더 구조 유지), 구글 드라이브 ZIP 통째로 가져오기
미리보기
  - 사진: 썸네일(아이폰 HEIC 포함), 촬영일 자동 인식
  - 영상: 첫 장면 썸네일 + 재생 시간, 브라우저에서 바로 재생(구간 이동 가능)
"""
import io
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, FileResponse, Response
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from database import engine, User, Project, FileFolder, FileItem, DATA_DIR
from template_utils import templates

router = APIRouter()

ROOT = Path(DATA_DIR) / "files"
THUMBS = Path(DATA_DIR) / "file_thumbs"
TMP = Path(DATA_DIR) / "upload_tmp"
for _d in (ROOT, THUMBS, TMP):
    _d.mkdir(parents=True, exist_ok=True)

CHUNK = 8 * 1024 * 1024
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
VID_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mts", ".wmv"}
DOC_EXT = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".hwp", ".hwpx", ".txt", ".csv", ".key", ".ai", ".psd", ".zip"}


# ════════════════════════════════════════════════════════════
# 저장소 (로컬 / S3 호환)
# ════════════════════════════════════════════════════════════
class Storage:
    """저장소 — 새 파일은 R2(설정 시)에, 기존 서버 파일은 그대로 서버에서 읽음.
    설정값(Railway Variables):
      R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
      (다른 S3 호환 저장소: S3_ENDPOINT, S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION)"""

    def __init__(self):
        env = os.environ.get
        acc = (env("R2_ACCOUNT_ID") or "").strip()
        self.bucket = (env("R2_BUCKET") or env("S3_BUCKET") or "").strip()
        self.endpoint = (env("S3_ENDPOINT") or (f"https://{acc}.r2.cloudflarestorage.com" if acc else "")).strip() or None
        self.key_id = (env("R2_ACCESS_KEY_ID") or env("S3_ACCESS_KEY") or "").strip()
        self.secret = (env("R2_SECRET_ACCESS_KEY") or env("S3_SECRET_KEY") or "").strip()
        self.region = (env("S3_REGION") or "auto").strip()
        self.s3 = None
        self.error = ""
        if self.bucket:
            try:
                import boto3
                from botocore.config import Config
                self.s3 = boto3.client("s3", endpoint_url=self.endpoint,
                                       aws_access_key_id=self.key_id or None,
                                       aws_secret_access_key=self.secret or None,
                                       region_name=self.region,
                                       config=Config(signature_version="s3v4", retries={"max_attempts": 4},
                                                     connect_timeout=10, read_timeout=120))
            except Exception as e:
                self.error = str(e)
                print(f"[FILES] R2 설정 실패 → 서버 저장공간 사용: {e}")
                self.s3 = None

    @property
    def mode(self):
        return "s3" if self.s3 else "local"

    # ── 저장 ──
    def put(self, local_path: Path, key: str, mime: str) -> str:
        """저장 후 실제 저장 위치('s3'/'local') 반환. R2 실패 시 서버에 저장(자료 유실 방지)."""
        if self.s3:
            try:
                from boto3.s3.transfer import TransferConfig
                self.s3.upload_file(str(local_path), self.bucket, key,
                                    ExtraArgs={"ContentType": mime or "application/octet-stream"},
                                    Config=TransferConfig(multipart_threshold=32 * 1024 * 1024,
                                                          multipart_chunksize=32 * 1024 * 1024, max_concurrency=4))
                local_path.unlink(missing_ok=True)
                return "s3"
            except Exception as e:
                print(f"[FILES] R2 업로드 실패 → 서버에 임시 저장: {e}")
        dst = ROOT / key
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(local_path), dst)
        return "local"

    def local_path(self, key: str) -> Path:
        p = (ROOT / key).resolve()
        if ROOT.resolve() not in p.parents:
            raise HTTPException(400)
        return p

    def url(self, key: str, filename: str, inline: bool, mime: str = ""):
        from urllib.parse import quote
        disp = ("inline" if inline else "attachment") + f"; filename*=UTF-8''{quote(filename)}"
        params = {"Bucket": self.bucket, "Key": key, "ResponseContentDisposition": disp}
        if mime:
            params["ResponseContentType"] = mime
        return self.s3.generate_presigned_url("get_object", Params=params, ExpiresIn=3600)

    def fetch_to(self, key: str, dst: Path, where: str = "local"):
        if where == "s3" and self.s3:
            self.s3.download_file(self.bucket, key, str(dst))
        else:
            shutil.copy(self.local_path(key), dst)

    def delete(self, key: str, where: str = "local"):
        if not key:
            return
        try:
            if where == "s3":
                if self.s3:
                    self.s3.delete_object(Bucket=self.bucket, Key=key)
            else:
                (ROOT / key).unlink(missing_ok=True)
        except Exception as e:
            print(f"[FILES] 삭제 실패 {key}: {e}")

    def test(self) -> dict:
        """연결 시험: 작은 파일 올리기 → 읽기 → 지우기"""
        if not self.bucket:
            return {"ok": False, "msg": "R2 설정값이 없습니다. Railway Variables에 R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY 를 넣어주세요."}
        if not self.s3:
            return {"ok": False, "msg": "R2 연결 준비 실패: " + (self.error or "알 수 없음")}
        k = f"_connection_test/{uuid.uuid4().hex}.txt"
        try:
            self.s3.put_object(Bucket=self.bucket, Key=k, Body=b"ok", ContentType="text/plain")
            body = self.s3.get_object(Bucket=self.bucket, Key=k)["Body"].read()
            self.s3.delete_object(Bucket=self.bucket, Key=k)
            return {"ok": body == b"ok", "msg": "✓ R2 연결 정상 (올리기·읽기·지우기 모두 성공)"}
        except Exception as e:
            msg = str(e)
            hint = ""
            if "NoSuchBucket" in msg:
                hint = " → 버킷 이름(R2_BUCKET)이 Cloudflare에 만든 이름과 같은지 확인하세요."
            elif "InvalidAccessKeyId" in msg or "SignatureDoesNotMatch" in msg or "Unauthorized" in msg or "403" in msg:
                hint = " → Access Key ID / Secret Access Key 를 다시 복사해 넣어주세요. 토큰 권한이 'Object Read & Write'인지도 확인하세요."
            elif "Could not connect" in msg or "Name or service" in msg or "getaddrinfo" in msg:
                hint = " → 계정 ID(R2_ACCOUNT_ID)가 맞는지 확인하세요."
            return {"ok": False, "msg": "R2 연결 실패: " + msg[:300] + hint}


store = Storage()


# ════════════════════════════════════════════════════════════
# 공통
# ════════════════════════════════════════════════════════════
def _user(request: Request):
    u = getattr(request.state, "user", None)
    if not u:
        raise HTTPException(303, headers={"Location": "/login?next=/files"})
    from permissions import has_permission
    if not has_permission(u, "files"):
        raise HTTPException(403, "자료실 권한이 없습니다.")
    return u


def _can_manage(u) -> bool:
    from permissions import has_permission
    return has_permission(u, "files_manage")


def _kind(name: str, mime: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMG_EXT or (mime or "").startswith("image/"):
        return "image"
    if ext in VID_EXT or (mime or "").startswith("video/"):
        return "video"
    if ext in DOC_EXT:
        return "doc"
    return "other"


def _mime(name: str, given: str = "") -> str:
    ext = Path(name).suffix.lower()
    extra = {".heic": "image/heic", ".heif": "image/heif", ".mov": "video/quicktime", ".m4v": "video/mp4",
             ".mts": "video/mp2t", ".hwp": "application/x-hwp", ".hwpx": "application/x-hwp", ".mkv": "video/x-matroska"}
    if ext in extra:
        return extra[ext]
    if given and given != "application/octet-stream":
        return given
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _human(n: int) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return (f"{n:.0f}{u}" if u in ("B", "KB") else f"{n:.1f}{u}")
        n /= 1024


def _ensure_folder_path(s, parts, project_id, parent_id, uid):
    """'행사A/사진' 같은 경로 → 폴더를 차례로 찾거나 만들고 마지막 폴더 id 반환"""
    for name in parts:
        name = name.strip()[:120]
        if not name or name in (".", ".."):
            continue
        f = s.exec(select(FileFolder).where(FileFolder.name == name, FileFolder.parent_id == parent_id,
                                            FileFolder.project_id == project_id)).first()
        if not f:
            f = FileFolder(name=name, parent_id=parent_id, project_id=project_id, created_by=uid)
            s.add(f)
            s.commit()
            s.refresh(f)
        parent_id = f.id
    return parent_id


# ════════════════════════════════════════════════════════════
# 미리보기(썸네일) 만들기
# ════════════════════════════════════════════════════════════
def _make_thumb(src: Path, kind: str, item_id_hint: str) -> dict:
    out = {"thumb_key": "", "width": 0, "height": 0, "duration": 0.0, "taken_at": None}
    dst = THUMBS / f"{item_id_hint}.jpg"
    try:
        if kind == "image":
            from PIL import Image, ImageOps
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except Exception:
                pass
            with Image.open(src) as im:
                try:
                    ex = im.getexif()
                    dt = ex.get(36867) or ex.get_ifd(0x8769).get(36867) or ex.get(306)
                    if dt:
                        out["taken_at"] = datetime.strptime(str(dt)[:19], "%Y:%m:%d %H:%M:%S")
                except Exception:
                    pass
                im = ImageOps.exif_transpose(im)
                out["width"], out["height"] = im.size
                im = im.convert("RGB")
                im.thumbnail((640, 640))
                im.save(dst, "JPEG", quality=78)
                out["thumb_key"] = dst.name
        elif kind == "video" and shutil.which("ffmpeg"):
            try:
                pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                     "stream=width,height:format=duration:format_tags=creation_time", "-of", "json", str(src)],
                                    capture_output=True, text=True, timeout=60)
                info = json.loads(pr.stdout or "{}")
                st = (info.get("streams") or [{}])[0]
                out["width"], out["height"] = int(st.get("width") or 0), int(st.get("height") or 0)
                out["duration"] = float((info.get("format") or {}).get("duration") or 0)
                ct = ((info.get("format") or {}).get("tags") or {}).get("creation_time")
                if ct:
                    out["taken_at"] = datetime.strptime(ct[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                pass
            ss = "1" if out["duration"] > 2 else "0"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-i", str(src), "-frames:v", "1",
                            "-vf", "scale='min(640,iw)':-2", str(dst)], timeout=120)
            if dst.exists() and dst.stat().st_size > 0:
                out["thumb_key"] = dst.name
    except Exception as e:
        print(f"[FILES] 썸네일 실패 {src.name}: {e}")
    return out


def _store_file(s, tmp_path: Path, name: str, mime: str, project_id, folder_id, uid) -> FileItem:
    """임시 파일 → 미리보기 생성 → 저장소 보관 → DB 기록"""
    mime = _mime(name, mime)
    kind = _kind(name, mime)
    hint = uuid.uuid4().hex
    meta = _make_thumb(tmp_path, kind, hint)
    now = datetime.now()
    ext = Path(name).suffix.lower()[:10]
    key = f"{now:%Y/%m}/{hint}{ext}"
    size = tmp_path.stat().st_size
    where = store.put(tmp_path, key, mime)
    it = FileItem(name=name[:240], stored_key=key, storage=where, thumb_key=meta["thumb_key"], size=size, mime=mime, kind=kind,
                  width=meta["width"], height=meta["height"], duration=meta["duration"], taken_at=meta["taken_at"],
                  project_id=project_id, folder_id=folder_id, uploaded_by=uid)
    s.add(it)
    s.commit()
    s.refresh(it)
    return it


# ════════════════════════════════════════════════════════════
# ★ 프로젝트 자료함 → 폴더로 전환 (한 번만 실행)
#   프로젝트별로 올라가 있던 자료를 '프로젝트 이름' 폴더로 옮김. 안의 하위 폴더 구조는 그대로 유지.
# ════════════════════════════════════════════════════════════
_MIGRATED = False


def migrate_project_folders():
    global _MIGRATED
    if _MIGRATED:
        return
    try:
        with Session(engine, expire_on_commit=False) as s:
            s.info["audit_off"] = True
            items = s.exec(select(FileItem).where(FileItem.project_id != None)).all()      # noqa: E711
            folders = s.exec(select(FileFolder).where(FileFolder.project_id != None)).all()  # noqa: E711
            pids = sorted({x.project_id for x in items} | {f.project_id for f in folders})
            moved = 0
            for pid in pids:
                p = s.get(Project, pid)
                base = ((p.name if p else "") or f"프로젝트 {pid}").strip()[:120]
                top = s.exec(select(FileFolder).where(FileFolder.name == base, FileFolder.parent_id == None,  # noqa: E711
                                                      FileFolder.project_id == None)).first()  # noqa: E711
                if not top:
                    top = FileFolder(name=base, parent_id=None, project_id=None)
                    s.add(top)
                    s.flush()
                for f in folders:
                    if f.project_id == pid:
                        if f.parent_id is None:
                            f.parent_id = top.id
                        f.project_id = None
                        s.add(f)
                for it in items:
                    if it.project_id == pid:
                        if it.folder_id is None:
                            it.folder_id = top.id
                        it.project_id = None
                        s.add(it)
                        moved += 1
            s.commit()
            if pids:
                print(f"[FILES] 프로젝트 자료함 {len(pids)}개 → 폴더로 전환 (자료 {moved}개)")
        _MIGRATED = True
    except Exception as e:
        print(f"[FILES] 폴더 전환 실패: {e}")


def _folder_tree(s):
    """모든 폴더 {id: folder} + 폴더별 하위 포함 자료 수"""
    fs = {f.id: f for f in s.exec(select(FileFolder)).all()}
    direct = {}
    for (fid,) in [(r,) for r in s.exec(select(FileItem.folder_id).where(FileItem.is_deleted == False)).all()]:  # noqa: E712
        if fid:
            direct[fid] = direct.get(fid, 0) + 1
    total = {}
    for fid in fs:
        n, cur, seen = direct.get(fid, 0), fid, set()
        total[fid] = total.get(fid, 0)
    for fid, n in direct.items():
        cur, seen = fid, set()
        while cur and cur in fs and cur not in seen:
            seen.add(cur)
            total[cur] = total.get(cur, 0) + n
            cur = fs[cur].parent_id
    return fs, total


def _folder_path(fs, fid):
    parts, cur, seen = [], fid, set()
    while cur and cur in fs and cur not in seen:
        seen.add(cur)
        parts.insert(0, fs[cur].name)
        cur = fs[cur].parent_id
    return " / ".join(parts)


# ════════════════════════════════════════════════════════════
# 화면
# ════════════════════════════════════════════════════════════
@router.get("/files", response_class=HTMLResponse)
def files_page(request: Request, project: str = "", folder: str = "", kind: str = "", q: str = "",
               trash: int = 0, scope: str = ""):
    user = _user(request)
    migrate_project_folders()
    fid = int(folder) if str(folder).isdigit() else None
    # 예전 '프로젝트 자료함' 주소(/files?project=번호) → 같은 이름 폴더로 이동
    #   (프로젝트 화면에서 '자료실 열기'를 누른 경우에만 그 이름의 폴더를 만들어 줌)
    if str(project).isdigit():
        with Session(engine, expire_on_commit=False) as s:
            p = s.get(Project, int(project))
            if not p:
                return RedirectResponse("/files", status_code=303)
            nm = (p.name or f"프로젝트 {p.id}").strip()[:120]
            f = s.exec(select(FileFolder).where(FileFolder.name == nm, FileFolder.parent_id == None)).first()  # noqa: E711
            if not f:
                f = FileFolder(name=nm, parent_id=None, project_id=None, created_by=user.id)
                s.add(f)
                s.commit()
                s.refresh(f)
            return RedirectResponse(f"/files?folder={f.id}", status_code=303)
    with Session(engine, expire_on_commit=False) as s:
        fs, ftotal = _folder_tree(s)
        root_n = len(s.exec(select(FileItem.id).where(FileItem.is_deleted == False, FileItem.folder_id == None)).all())  # noqa: E711,E712
        all_n = len(s.exec(select(FileItem.id).where(FileItem.is_deleted == False)).all())  # noqa: E712
        total_size = sum(x or 0 for x in s.exec(select(FileItem.size).where(FileItem.is_deleted == False)).all())  # noqa: E712
        trash_n = len(s.exec(select(FileItem.id).where(FileItem.is_deleted == True)).all())  # noqa: E712
        recent = (scope == "recent")
        cur_folder, crumbs = (fs.get(fid) if fid else None), []
        if fid and not cur_folder:
            fid = None
        f = cur_folder
        while f:
            crumbs.insert(0, {"id": f.id, "name": f.name})
            f = fs.get(f.parent_id) if f.parent_id else None
        browsing = not trash and not q and not recent
        subfolders = []
        if browsing:
            for f in sorted([x for x in fs.values() if x.parent_id == fid], key=lambda x: x.name):
                nsub = sum(1 for x in fs.values() if x.parent_id == f.id)
                subfolders.append({"id": f.id, "name": f.name, "n": ftotal.get(f.id, 0), "nsub": nsub})
        stmt = select(FileItem).where(FileItem.is_deleted == bool(trash))
        if not trash:
            if q:
                stmt = stmt.where(FileItem.name.ilike(f"%{q.strip()}%"))
            elif browsing:
                stmt = stmt.where(FileItem.folder_id == fid)
        if kind in ("image", "video", "doc", "other"):
            stmt = stmt.where(FileItem.kind == kind)
        items = s.exec(stmt.order_by(FileItem.created_at.desc()).limit(600 if not recent else 200)).all()
        names = {u.id: (u.name or u.username) for u in s.exec(select(User)).all()}
        rows = [{
            "id": it.id, "name": it.name, "kind": it.kind, "size": _human(it.size), "bytes": it.size,
            "thumb": bool(it.thumb_key), "mime": it.mime, "w": it.width, "h": it.height,
            "dur": (f"{int(it.duration // 60)}:{int(it.duration % 60):02d}" if it.duration else ""),
            "date": (it.taken_at or it.created_at), "by": names.get(it.uploaded_by, ""),
            "project": (_folder_path(fs, it.folder_id) if it.folder_id else ""),
            "mine": it.uploaded_by == user.id,
        } for it in items]
        top_folders = [{"id": f.id, "name": f.name, "n": ftotal.get(f.id, 0)}
                       for f in sorted([x for x in fs.values() if x.parent_id is None], key=lambda x: x.name)]
        all_folders = sorted([{"id": f.id, "path": _folder_path(fs, f.id)} for f in fs.values()], key=lambda x: x["path"])
        top_id = crumbs[0]["id"] if crumbs else None
    disk = None
    if store.mode == "local":
        try:
            du = shutil.disk_usage(ROOT)
            disk = {"total": _human(du.total), "free": _human(du.free), "pct": round(du.used / du.total * 100)}
        except Exception:
            disk = None
    return templates.TemplateResponse(request, "files.html", {
        "user": user, "rows": rows, "top_folders": top_folders, "all_folders": all_folders, "top_id": top_id,
        "fid": fid, "crumbs": crumbs, "subfolders": subfolders, "browsing": browsing, "recent": recent,
        "kind": kind, "q": q, "trash": trash, "trash_n": trash_n, "root_n": root_n, "all_n": all_n,
        "total_size": _human(total_size), "disk": disk, "storage_mode": store.mode,
        "can_manage": _can_manage(user), "chunk": CHUNK,
        # 아래는 예전 화면 호환용
        "pid": None, "projects": [], "recent_pids": [], "cur_project": None, "is_common": browsing,
        "scope_root": recent, "common_n": root_n,
    })


def _old_files_page_unused(request: Request, project: str = "", folder: str = "", kind: str = "", q: str = "",
                           trash: int = 0, scope: str = ""):
    user = _user(request)
    pid = int(project) if str(project).isdigit() else None
    fid = int(folder) if str(folder).isdigit() else None
    with Session(engine, expire_on_commit=False) as s:
        projects = s.exec(select(Project).order_by(Project.event_date.desc())).all()
        all_items = s.exec(select(FileItem).where(FileItem.is_deleted == False)).all()
        counts, sizes = {}, {}
        for it in all_items:
            k = it.project_id or 0
            counts[k] = counts.get(k, 0) + 1
            sizes[k] = sizes.get(k, 0) + (it.size or 0)
        total_size = sum(sizes.values())
        trash_n = len(s.exec(select(FileItem.id).where(FileItem.is_deleted == True)).all())

        cur_folder, crumbs = None, []
        if fid:
            cur_folder = s.get(FileFolder, fid)
            if cur_folder:
                pid = cur_folder.project_id
                f = cur_folder
                while f:
                    crumbs.insert(0, {"id": f.id, "name": f.name})
                    f = s.get(FileFolder, f.parent_id) if f.parent_id else None
        is_common = (scope == "common") or (not pid and fid)
        subfolders = []
        if not trash and not q and (pid or is_common):
            fq = select(FileFolder).where(FileFolder.parent_id == (fid if fid else None),
                                          FileFolder.project_id == (pid if pid else None)).order_by(FileFolder.name)
            for f in s.exec(fq).all():
                n = len(s.exec(select(FileItem.id).where(FileItem.folder_id == f.id, FileItem.is_deleted == False)).all())
                nsub = len(s.exec(select(FileFolder.id).where(FileFolder.parent_id == f.id)).all())
                subfolders.append({"id": f.id, "name": f.name, "n": n, "nsub": nsub})

        stmt = select(FileItem).where(FileItem.is_deleted == bool(trash))
        if not trash:
            if q:
                stmt = stmt.where(FileItem.name.ilike(f"%{q.strip()}%"))
                if pid:
                    stmt = stmt.where(FileItem.project_id == pid)
            elif pid or is_common:
                stmt = stmt.where(FileItem.project_id == (pid if pid else None),
                                  FileItem.folder_id == (fid if fid else None))
        if kind in ("image", "video", "doc", "other"):
            stmt = stmt.where(FileItem.kind == kind)
        items = s.exec(stmt.order_by(FileItem.created_at.desc()).limit(600)).all()
        names = {u.id: (u.name or u.username) for u in s.exec(select(User)).all()}
        pmap = {p.id: p for p in projects}
        rows = [{
            "id": it.id, "name": it.name, "kind": it.kind, "size": _human(it.size), "bytes": it.size,
            "thumb": bool(it.thumb_key), "mime": it.mime, "w": it.width, "h": it.height,
            "dur": (f"{int(it.duration // 60)}:{int(it.duration % 60):02d}" if it.duration else ""),
            "date": (it.taken_at or it.created_at), "by": names.get(it.uploaded_by, ""),
            "project": (pmap[it.project_id].name if it.project_id in pmap else ""),
            "mine": it.uploaded_by == user.id,
        } for it in items]
        cur_project = pmap.get(pid) if pid else None
        proj_list = [{"id": p.id, "name": p.name, "date": p.event_date, "n": counts.get(p.id, 0)} for p in projects]
        # 최근 자료가 있는 프로젝트 → 위로
        recent_pids = []
        for it in sorted(all_items, key=lambda x: x.created_at, reverse=True):
            if it.project_id and it.project_id not in recent_pids:
                recent_pids.append(it.project_id)
            if len(recent_pids) >= 6:
                break
    disk = None
    if store.mode == "local":
        try:
            du = shutil.disk_usage(ROOT)
            disk = {"total": _human(du.total), "free": _human(du.free), "pct": round(du.used / du.total * 100)}
        except Exception:
            disk = None
    return templates.TemplateResponse(request, "files.html", {
        "user": user, "rows": rows, "projects": proj_list, "recent_pids": recent_pids,
        "pid": pid, "fid": fid, "cur_project": cur_project, "crumbs": crumbs, "subfolders": subfolders,
        "kind": kind, "q": q, "trash": trash, "trash_n": trash_n, "is_common": bool(is_common),
        "common_n": counts.get(0, 0), "all_n": sum(counts.values()),
        "total_size": _human(total_size), "disk": disk, "storage_mode": store.mode,
        "can_manage": _can_manage(user), "chunk": CHUNK,
        "can_new_project": __import__("permissions").has_permission(user, "projects"),
        "scope_root": (not pid and not is_common and not trash and not q),
    })


@router.get("/api/files/summary/{pid}")
def files_summary(request: Request, pid: int):
    """프로젝트 상세 화면 카드용 — 개수 + 최근 미리보기 8장"""
    _user(request)
    migrate_project_folders()
    with Session(engine, expire_on_commit=False) as s:
        # 프로젝트 이름과 같은 폴더(하위 폴더 포함)의 자료
        p = s.get(Project, pid)
        top = s.exec(select(FileFolder).where(FileFolder.name == (p.name if p else ""),
                                              FileFolder.parent_id == None)).first() if p else None  # noqa: E711
        ids = set()
        if top:
            todo = [top.id]
            while todo:
                c = todo.pop()
                ids.add(c)
                todo += list(s.exec(select(FileFolder.id).where(FileFolder.parent_id == c)).all())
        items = s.exec(select(FileItem).where(FileItem.folder_id.in_(ids), FileItem.is_deleted == False)  # noqa: E712
                       .order_by(FileItem.created_at.desc())).all() if ids else []
    return JSONResponse({
        "count": len(items), "size": _human(sum(i.size or 0 for i in items)),
        "images": sum(1 for i in items if i.kind == "image"), "videos": sum(1 for i in items if i.kind == "video"),
        "docs": sum(1 for i in items if i.kind in ("doc", "other")),
        "recent": [{"id": i.id, "kind": i.kind, "thumb": bool(i.thumb_key), "name": i.name} for i in items[:8]],
    })


# ════════════════════════════════════════════════════════════
# 업로드 (조각 전송 · 이어올리기)
# ════════════════════════════════════════════════════════════
@router.post("/api/files/upload/init")
async def upload_init(request: Request):
    user = _user(request)
    d = await request.json()
    name = Path(str(d.get("name") or "file")).name[:240] or "file"
    up = uuid.uuid4().hex
    meta = {"name": name, "size": int(d.get("size") or 0), "mime": str(d.get("mime") or ""),
            "project_id": int(d["project_id"]) if str(d.get("project_id") or "").isdigit() else None,
            "folder_id": int(d["folder_id"]) if str(d.get("folder_id") or "").isdigit() else None,
            "rel_path": str(d.get("rel_path") or ""), "import_zip": bool(d.get("import_zip")), "uid": user.id}
    (TMP / f"{up}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (TMP / f"{up}.part").write_bytes(b"")
    return JSONResponse({"upload_id": up, "chunk": CHUNK})


@router.post("/api/files/upload/{up}/chunk")
async def upload_chunk(request: Request, up: str, offset: int = 0):
    _user(request)
    if not up.isalnum():
        raise HTTPException(400)
    part = TMP / f"{up}.part"
    if not part.exists():
        raise HTTPException(404, "업로드 세션이 만료되었습니다.")
    cur = part.stat().st_size
    if offset != cur:      # 끊겼다가 이어올릴 때: 서버가 가진 위치를 알려줌
        return JSONResponse({"ok": False, "offset": cur}, status_code=409)
    with open(part, "ab") as f:
        async for blk in request.stream():
            f.write(blk)
    return JSONResponse({"ok": True, "offset": part.stat().st_size})


@router.post("/api/files/upload/{up}/done")
def upload_done(request: Request, up: str):
    user = _user(request)
    if not up.isalnum():
        raise HTTPException(400)
    part, mf = TMP / f"{up}.part", TMP / f"{up}.json"
    if not part.exists() or not mf.exists():
        raise HTTPException(404, "업로드 세션이 만료되었습니다.")
    meta = json.loads(mf.read_text(encoding="utf-8"))
    if meta["size"] and part.stat().st_size != meta["size"]:
        raise HTTPException(400, "파일이 끝까지 전송되지 않았습니다. 다시 시도해주세요.")
    try:
        with Session(engine, expire_on_commit=False) as s:
            if meta.get("import_zip"):
                n = _import_zip(s, part, meta, user.id)
                return JSONResponse({"ok": True, "imported": n})
            folder_id = meta["folder_id"]
            rel = [p for p in meta["rel_path"].replace("\\", "/").split("/")[:-1] if p]
            if rel:
                folder_id = _ensure_folder_path(s, rel, meta["project_id"], folder_id, user.id)
            it = _store_file(s, part, meta["name"], meta["mime"], meta["project_id"], folder_id, user.id)
            return JSONResponse({"ok": True, "id": it.id, "kind": it.kind})
    finally:
        part.unlink(missing_ok=True)
        mf.unlink(missing_ok=True)


def _zip_name(info: zipfile.ZipInfo) -> str:
    n = info.filename
    if not (info.flag_bits & 0x800):        # 윈도우에서 만든 한글 ZIP
        try:
            n = n.encode("cp437").decode("cp949")
        except Exception:
            pass
    return n


def _import_zip(s, zpath: Path, meta: dict, uid) -> int:
    """구글 드라이브/테이크아웃 ZIP → 폴더 구조 그대로 가져오기"""
    n = 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = _zip_name(info).replace("\\", "/")
            parts = [p for p in name.split("/") if p]
            if not parts or parts[-1].startswith(".") or "__MACOSX" in parts:
                continue
            # 테이크아웃 머리 폴더 제거: Takeout/드라이브/...
            while parts and parts[0].lower() in ("takeout", "드라이브", "drive", "내 드라이브", "my drive", "google drive"):
                parts = parts[1:]
            if not parts:
                continue
            fname = parts[-1]
            folder_id = _ensure_folder_path(s, parts[:-1], meta["project_id"], meta["folder_id"], uid)
            tmp = TMP / f"z_{uuid.uuid4().hex}"
            with z.open(info) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            _store_file(s, tmp, fname, "", meta["project_id"], folder_id, uid)
            n += 1
    return n


# ════════════════════════════════════════════════════════════
# 보기 · 내려받기
# ════════════════════════════════════════════════════════════
def _get_item(s, fid: int) -> FileItem:
    it = s.get(FileItem, fid)
    if not it:
        raise HTTPException(404, "자료를 찾을 수 없습니다.")
    return it


@router.get("/files/{fid}/thumb")
def file_thumb(request: Request, fid: int):
    _user(request)
    with Session(engine) as s:
        it = _get_item(s, fid)
        tk = it.thumb_key
    if not tk:
        raise HTTPException(404)
    return FileResponse(THUMBS / tk, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


def _range_response(path: Path, mime: str, filename: str, inline: bool, range_header: Optional[str]):
    from urllib.parse import quote
    size = path.stat().st_size
    disp = ("inline" if inline else "attachment") + f"; filename*=UTF-8''{quote(filename)}"
    headers = {"Accept-Ranges": "bytes", "Content-Disposition": disp, "Cache-Control": "private, max-age=3600"}
    start, end = 0, size - 1
    status = 200
    if range_header and range_header.startswith("bytes="):
        try:
            a, b = range_header[6:].split(",")[0].split("-")
            if a:
                start = int(a)
                end = int(b) if b else size - 1
            else:                              # 마지막 N바이트
                start = max(0, size - int(b))
            end = min(end, size - 1)
            if start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        except ValueError:
            start, end = 0, size - 1
    length = end - start + 1
    headers["Content-Length"] = str(length)

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                blk = f.read(min(1024 * 1024, left))
                if not blk:
                    break
                left -= len(blk)
                yield blk
    return StreamingResponse(gen(), status_code=status, media_type=mime, headers=headers)


@router.get("/files/{fid}/raw")
def file_raw(request: Request, fid: int):
    """미리보기용(브라우저 안에서 열기) — 영상 구간 이동 지원"""
    _user(request)
    with Session(engine) as s:
        it = _get_item(s, fid)
        key, name, mime, where = it.stored_key, it.name, it.mime, (it.storage or "local")
    heic = mime in ("image/heic", "image/heif") and "image/heic" not in (request.headers.get("accept") or "")
    if where == "s3":
        if not store.s3:
            raise HTTPException(503, "R2 설정이 빠져 이 자료를 열 수 없습니다. 관리자에게 문의하세요.")
        if not heic:
            return RedirectResponse(store.url(key, name, True, mime), status_code=302)
        tmp = TMP / f"h_{uuid.uuid4().hex}"
        store.fetch_to(key, tmp, "s3")
        p = tmp
    else:
        p = store.local_path(key)
    if not p or not p.exists():
        raise HTTPException(404, "원본 파일이 없습니다.")
    # 아이폰 HEIC 는 대부분 브라우저가 못 띄움 → 미리보기 JPG 로 대체
    if heic:
        try:
            from PIL import Image, ImageOps
            import pillow_heif
            pillow_heif.register_heif_opener()
            with Image.open(p) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((2400, 2400))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=85)
            if where == "s3":
                p.unlink(missing_ok=True)
            return Response(buf.getvalue(), media_type="image/jpeg")
        except Exception:
            pass
    if where == "s3":
        p.unlink(missing_ok=True)
        return RedirectResponse(store.url(key, name, True, mime), status_code=302)
    return _range_response(p, mime, name, True, request.headers.get("range"))


@router.get("/files/{fid}/download")
def file_download(request: Request, fid: int):
    _user(request)
    with Session(engine) as s:
        it = _get_item(s, fid)
        key, name, mime, where = it.stored_key, it.name, it.mime, (it.storage or "local")
    if where == "s3":
        if not store.s3:
            raise HTTPException(503, "R2 설정이 빠져 이 자료를 받을 수 없습니다. 관리자에게 문의하세요.")
        return RedirectResponse(store.url(key, name, False, mime), status_code=302)
    p = store.local_path(key)
    if not p or not p.exists():
        raise HTTPException(404, "원본 파일이 없습니다.")
    return _range_response(p, mime, name, False, request.headers.get("range"))


@router.post("/api/files/zip")
async def files_zip(request: Request):
    """여러 개 선택 → ZIP 하나로 내려받기"""
    _user(request)
    form = await request.form()
    ids = [int(x) for x in str(form.get("ids") or "").split(",") if x.strip().isdigit()][:2000]
    if not ids:
        raise HTTPException(400, "선택된 자료가 없습니다.")
    tmpdir = Path(tempfile.mkdtemp(dir=TMP))
    zpath = tmpdir / "files.zip"
    used = set()
    title = "자료"
    with Session(engine) as s, zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for fid in ids:
            it = s.get(FileItem, fid)
            if not it or it.is_deleted:
                continue
            nm, i = it.name, 1
            while nm in used:
                stem, ext = os.path.splitext(it.name)
                nm, i = f"{stem} ({i}){ext}", i + 1
            used.add(nm)
            if (it.storage or "local") == "local":
                p = store.local_path(it.stored_key)
                if p and p.exists():
                    z.write(p, nm)
            else:
                tf = tmpdir / uuid.uuid4().hex
                store.fetch_to(it.stored_key, tf, "s3")
                z.write(tf, nm)
                tf.unlink(missing_ok=True)
            if it.project_id and title == "자료":
                pr = s.get(Project, it.project_id)
                title = pr.name if pr else title
    fname = f"{title}_{datetime.now():%Y%m%d}.zip".replace("/", "_")
    return FileResponse(zpath, media_type="application/zip", filename=fname,
                        background=BackgroundTask(shutil.rmtree, tmpdir, True))


@router.get("/files/folder/{fid}/zip")
def folder_zip(request: Request, fid: int):
    """폴더째 내려받기 — 하위 폴더 구조 그대로 ZIP 하나로"""
    _user(request)
    tmpdir = Path(tempfile.mkdtemp(dir=TMP))
    zpath = tmpdir / "folder.zip"
    with Session(engine) as s:
        top = s.get(FileFolder, fid)
        if not top:
            shutil.rmtree(tmpdir, True)
            raise HTTPException(404, "폴더를 찾을 수 없습니다.")
        title = top.name
        n = 0
        missing = []
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            todo, seen = [(top.id, title)], set()
            while todo:
                cid, path = todo.pop()
                if cid in seen:
                    continue
                seen.add(cid)
                used = set()
                for it in s.exec(select(FileItem).where(FileItem.folder_id == cid, FileItem.is_deleted == False)).all():  # noqa: E712
                    nm, i = it.name, 1
                    while nm in used:
                        stem, ext = os.path.splitext(it.name)
                        nm, i = f"{stem} ({i}){ext}", i + 1
                    used.add(nm)
                    arc = f"{path}/{nm}"
                    try:
                        if (it.storage or "local") == "local":
                            p = store.local_path(it.stored_key)
                            if p and p.exists():
                                z.write(p, arc)
                                n += 1
                        else:
                            tf = tmpdir / uuid.uuid4().hex
                            store.fetch_to(it.stored_key, tf, "s3")
                            z.write(tf, arc)
                            tf.unlink(missing_ok=True)
                            n += 1
                    except Exception as e:
                        print(f"[FILES] 폴더 ZIP 누락 {it.name}: {e}")
                        missing.append(arc)
                subs = s.exec(select(FileFolder).where(FileFolder.parent_id == cid)).all()
                if not subs and not used:
                    z.writestr(f"{path}/", "")   # 빈 폴더도 유지
                for sf in subs:
                    todo.append((sf.id, f"{path}/{sf.name}"))
            if missing:
                z.writestr(f"{title}/_내려받지_못한_파일.txt",
                           "아래 파일은 저장소에서 찾지 못해 ZIP에 포함되지 않았습니다.\n"
                           "자료실 화면에서 개별로 다시 시도하거나 관리자에게 알려주세요.\n\n" + "\n".join(missing))
    fname = f"{title}_{datetime.now():%Y%m%d}.zip".replace("/", "_")
    return FileResponse(zpath, media_type="application/zip", filename=fname,
                        background=BackgroundTask(shutil.rmtree, tmpdir, True))


# ════════════════════════════════════════════════════════════
# 자료실에서 바로 새 프로젝트 만들기 (프로젝트 등록 안 된 행사 자료용)
# ════════════════════════════════════════════════════════════
@router.post("/api/files/project")
async def files_project_create(request: Request):
    user = _user(request)
    from permissions import has_permission
    if not has_permission(user, "projects"):
        raise HTTPException(403, "프로젝트 등록 권한이 없습니다. 관리자에게 요청하세요.")
    d = await request.json()
    name = str(d.get("name") or "").strip()[:120]
    if not name:
        raise HTTPException(400, "프로젝트 이름을 입력하세요.")
    from datetime import date as _date

    def _pd(v):
        try:
            return _date.fromisoformat(str(v or "").strip()) if v else None
        except ValueError:
            return None
    d1, d2 = _pd(d.get("event_date")), _pd(d.get("event_end_date"))
    if d1 and d2 and d2 < d1:
        d1, d2 = d2, d1
    if d2 and d1 and d2 == d1:
        d2 = None
    with Session(engine, expire_on_commit=False) as s:
        same = s.exec(select(Project).where(Project.name == name)).first()
        if same and not d.get("force"):
            return JSONResponse({"ok": False, "dup": True, "id": same.id,
                                 "msg": f"'{name}' 이름의 프로젝트가 이미 있습니다."})
        from routes_project import _unique_project_code
        year = (d1 or _date.today()).year
        count = len(s.exec(select(Project.id)).all()) + 1
        code = _unique_project_code(s, f"P-{year}-{count:03d}")
        p = Project(code=code, name=name, event_date=d1, event_end_date=d2,
                    location=str(d.get("location") or "").strip()[:200],
                    memo="[자료실에서 등록]")
        s.add(p)
        s.commit()
        s.refresh(p)
        return JSONResponse({"ok": True, "id": p.id, "code": p.code})


# ════════════════════════════════════════════════════════════
# 정리 (폴더 · 이동 · 이름 · 휴지통)
# ════════════════════════════════════════════════════════════
@router.post("/api/files/folder")
async def folder_create(request: Request):
    user = _user(request)
    d = await request.json()
    name = str(d.get("name") or "").strip()[:120]
    if not name:
        raise HTTPException(400, "폴더 이름을 입력하세요.")
    pid = int(d["project_id"]) if str(d.get("project_id") or "").isdigit() else None
    parent = int(d["parent_id"]) if str(d.get("parent_id") or "").isdigit() else None
    with Session(engine, expire_on_commit=False) as s:
        fid = _ensure_folder_path(s, [name], pid, parent, user.id)
    return JSONResponse({"ok": True, "id": fid})


@router.post("/api/files/folder/{fid}/rename")
async def folder_rename(request: Request, fid: int):
    user = _user(request)
    d = await request.json()
    with Session(engine, expire_on_commit=False) as s:
        f = s.get(FileFolder, fid)
        if not f:
            raise HTTPException(404)
        if not (_can_manage(user) or f.created_by == user.id):
            raise HTTPException(403, "폴더 정리 권한이 없습니다.")
        f.name = str(d.get("name") or f.name).strip()[:120] or f.name
        s.add(f)
        s.commit()
    return JSONResponse({"ok": True})


@router.post("/api/files/folder/{fid}/delete")
def folder_delete(request: Request, fid: int):
    """폴더 삭제 — 안의 자료는 모두 휴지통으로 (복구 가능)"""
    user = _user(request)
    if not _can_manage(user):
        raise HTTPException(403, "폴더 삭제는 '자료실 정리·삭제' 권한이 필요합니다.")
    with Session(engine, expire_on_commit=False) as s:
        f = s.get(FileFolder, fid)
        if not f:
            raise HTTPException(404)
        parent, pid = f.parent_id, f.project_id
        todo, n = [f], 0
        while todo:
            cur = todo.pop()
            todo += s.exec(select(FileFolder).where(FileFolder.parent_id == cur.id)).all()
            for it in s.exec(select(FileItem).where(FileItem.folder_id == cur.id)).all():
                it.is_deleted, it.deleted_at, it.folder_id = True, datetime.utcnow(), None
                s.add(it)
                n += 1
            s.delete(cur)
        s.commit()
    return JSONResponse({"ok": True, "trashed": n, "parent_id": parent, "project_id": pid})


def _ids(d) -> list:
    return [int(x) for x in (d.get("ids") or []) if str(x).isdigit()]


@router.post("/api/files/move")
async def files_move(request: Request):
    user = _user(request)
    d = await request.json()
    pid = int(d["project_id"]) if str(d.get("project_id") or "").isdigit() else None
    folder = int(d["folder_id"]) if str(d.get("folder_id") or "").isdigit() else None
    n = 0
    with Session(engine, expire_on_commit=False) as s:
        if folder:
            f = s.get(FileFolder, folder)
            pid = f.project_id if f else pid
        for fid in _ids(d):
            it = s.get(FileItem, fid)
            if not it:
                continue
            if not (_can_manage(user) or it.uploaded_by == user.id):
                continue
            it.project_id, it.folder_id = pid, folder
            s.add(it)
            n += 1
        s.commit()
    return JSONResponse({"ok": True, "moved": n})


@router.post("/api/files/{fid}/rename")
async def file_rename(request: Request, fid: int):
    user = _user(request)
    d = await request.json()
    with Session(engine, expire_on_commit=False) as s:
        it = _get_item(s, fid)
        if not (_can_manage(user) or it.uploaded_by == user.id):
            raise HTTPException(403, "본인이 올린 자료만 이름을 바꿀 수 있습니다.")
        new = str(d.get("name") or "").strip()[:240]
        if new:
            if not Path(new).suffix and Path(it.name).suffix:
                new += Path(it.name).suffix
            it.name = new
            s.add(it)
            s.commit()
    return JSONResponse({"ok": True})


@router.post("/api/files/trash")
async def files_trash(request: Request):
    """휴지통으로 (30일 보관 후 자동 영구 삭제)"""
    user = _user(request)
    d = await request.json()
    n, denied = 0, 0
    with Session(engine, expire_on_commit=False) as s:
        for fid in _ids(d):
            it = s.get(FileItem, fid)
            if not it:
                continue
            if not (_can_manage(user) or it.uploaded_by == user.id):
                denied += 1
                continue
            it.is_deleted, it.deleted_at = True, datetime.utcnow()
            s.add(it)
            n += 1
        s.commit()
    return JSONResponse({"ok": True, "trashed": n, "denied": denied})


@router.post("/api/files/restore")
async def files_restore(request: Request):
    user = _user(request)
    d = await request.json()
    n = 0
    with Session(engine, expire_on_commit=False) as s:
        for fid in _ids(d):
            it = s.get(FileItem, fid)
            if not it or not (_can_manage(user) or it.uploaded_by == user.id):
                continue
            if it.folder_id and not s.get(FileFolder, it.folder_id):
                it.folder_id = None
            it.is_deleted, it.deleted_at = False, None
            s.add(it)
            n += 1
        s.commit()
    return JSONResponse({"ok": True, "restored": n})


def _purge(s, it):
    store.delete(it.stored_key, it.storage or "local")
    if it.thumb_key:
        (THUMBS / it.thumb_key).unlink(missing_ok=True)
    s.delete(it)


@router.post("/api/files/purge")
async def files_purge(request: Request):
    """휴지통에서 영구 삭제"""
    user = _user(request)
    if not _can_manage(user):
        raise HTTPException(403, "영구 삭제는 '자료실 정리·삭제' 권한이 필요합니다.")
    d = await request.json()
    n = 0
    with Session(engine, expire_on_commit=False) as s:
        q = select(FileItem).where(FileItem.is_deleted == True)
        items = s.exec(q).all() if d.get("all") else [s.get(FileItem, i) for i in _ids(d)]
        for it in items:
            if it and it.is_deleted:
                _purge(s, it)
                n += 1
        s.commit()
    return JSONResponse({"ok": True, "purged": n})


def purge_old_trash(days: int = 30):
    """서버 시작 시 호출 — 30일 지난 휴지통 자료 정리 + 하루 지난 업로드 찌꺼기 삭제"""
    from datetime import timedelta
    try:
        lim = datetime.utcnow() - timedelta(days=days)
        with Session(engine) as s:
            for it in s.exec(select(FileItem).where(FileItem.is_deleted == True, FileItem.deleted_at < lim)).all():
                _purge(s, it)
            s.commit()
        cut = datetime.now().timestamp() - 86400
        for f in TMP.glob("*"):
            if f.is_file() and f.stat().st_mtime < cut:
                f.unlink(missing_ok=True)
    except Exception as e:
        print(f"[FILES] 휴지통 정리 실패: {e}")


# ════════════════════════════════════════════════════════════
# 저장소 설정 (관리자) — R2 연결 확인 · 기존 파일 R2로 옮기기
# ════════════════════════════════════════════════════════════
import threading
_MIG = {"running": False, "done": 0, "fail": 0, "total": 0, "bytes": 0, "last_error": "", "finished_at": None}


def _require_admin(request: Request):
    u = getattr(request.state, "user", None)
    if not u:
        raise HTTPException(303, headers={"Location": "/login?next=/files/storage"})
    from permissions import is_admin
    if not is_admin(u):
        raise HTTPException(403, "저장소 설정은 관리자만 볼 수 있습니다.")
    return u


def _storage_stats():
    with Session(engine) as s:
        rows = s.exec(select(FileItem.storage, FileItem.size)).all()
    st = {"local_n": 0, "local_b": 0, "s3_n": 0, "s3_b": 0}
    for where, size in rows:
        k = "s3" if where == "s3" else "local"
        st[k + "_n"] += 1
        st[k + "_b"] += size or 0
    return st


@router.get("/files/storage", response_class=HTMLResponse)
def storage_page(request: Request):
    user = _require_admin(request)
    st = _storage_stats()
    disk = None
    try:
        du = shutil.disk_usage(ROOT)
        disk = {"total": _human(du.total), "free": _human(du.free), "pct": round(du.used / du.total * 100)}
    except Exception:
        pass
    return templates.TemplateResponse(request, "files_storage.html", {
        "user": user, "mode": store.mode, "bucket": store.bucket, "endpoint": store.endpoint or "",
        "has_key": bool(store.key_id), "error": store.error, "disk": disk,
        "local_n": st["local_n"], "local_size": _human(st["local_b"]),
        "s3_n": st["s3_n"], "s3_size": _human(st["s3_b"]), "mig": _MIG,
    })


@router.post("/api/files/storage/test")
def storage_test(request: Request):
    _require_admin(request)
    return JSONResponse(store.test())


def _migrate_worker():
    try:
        with Session(engine) as s:
            ids = [i for i in s.exec(select(FileItem.id).where(FileItem.storage != "s3")).all()]
        _MIG.update(total=len(ids), done=0, fail=0, bytes=0, last_error="")
        for fid in ids:
            try:
                with Session(engine) as s:
                    it = s.get(FileItem, fid)
                    if not it or it.storage == "s3":
                        continue
                    p = store.local_path(it.stored_key)
                    if not p.exists():
                        raise FileNotFoundError(f"{it.name}: 서버에 원본이 없음")
                    store.s3.upload_file(str(p), store.bucket, it.stored_key,
                                         ExtraArgs={"ContentType": it.mime or "application/octet-stream"})
                    store.s3.head_object(Bucket=store.bucket, Key=it.stored_key)   # 올라간 것 확인 후에만
                    it.storage = "s3"
                    s.add(it)
                    s.commit()
                    _MIG["bytes"] += it.size or 0
                    p.unlink(missing_ok=True)                                      # 서버 사본 삭제(공간 확보)
                _MIG["done"] += 1
            except Exception as e:
                _MIG["fail"] += 1
                _MIG["last_error"] = str(e)[:200]
    finally:
        _MIG["running"] = False
        _MIG["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")


@router.post("/api/files/storage/migrate")
def storage_migrate(request: Request):
    _require_admin(request)
    if not store.s3:
        raise HTTPException(400, "R2가 연결되어 있지 않습니다. 먼저 설정값을 넣고 재배포하세요.")
    t = store.test()
    if not t["ok"]:
        raise HTTPException(400, t["msg"])
    if _MIG["running"]:
        return JSONResponse({"ok": True, "already": True, **_MIG})
    _MIG["running"] = True
    threading.Thread(target=_migrate_worker, daemon=True).start()
    return JSONResponse({"ok": True, **_MIG})


@router.get("/api/files/storage/status")
def storage_status(request: Request):
    _require_admin(request)
    return JSONResponse({**_MIG, **_storage_stats()})
