"""데이터 관리 — Excel 내보내기/불러오기 + 완전 백업(ZIP)

전체 데이터(프로젝트, 비용, 근태, 거래처, 장비, 근무자)를
한 개의 Excel 파일(시트별)로 백업하고 복원할 수 있습니다.

또한 DB 파일 + 업로드된 모든 이미지(영수증/로고/직원사진)를
한 개의 ZIP 파일로 완전 백업 · 복원할 수 있습니다.

[안전 원칙]
- Excel 불러오기는 항상 "추가" 모드 — 기존 데이터 보존
- 완전 백업 복원은 관리자만 접근, 확인 절차 필수
- 코드 중복 시 자동 skip (기존 유지)
"""
import io
import os
import json
import shutil
import zipfile
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
from sqlmodel import Session, select
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

from database import (
    engine, User, DATA_DIR, DB_PATH,
    Project, Expense, Worker, Attendance, WorkerCheckin, Vendor,
    Equipment, EquipmentUnit, MaintenanceLog, Item,
    Quote, QuoteItem, CompanySettings,
)
from permissions import is_admin, require_admin_user
from template_utils import templates

router = APIRouter()


def _user(request: Request):
    uid = request.session.get("user_id")
    if not uid:
        raise HTTPException(303, headers={"Location": "/login"})
    with Session(engine, expire_on_commit=False) as s:
        return s.get(User, uid)


# ============================================================
# 1. 시트 스키마 (열 이름과 모델 필드 매핑)
# ============================================================
SHEET_SCHEMA = {
    "프로젝트": {
        "model": Project,
        "fields": [
            ("프로젝트 코드", "code"),
            ("프로젝트명", "name"),
            ("거래처ID", "vendor_id"),
            ("행사일", "event_date"),
            ("장소", "location"),
            ("공급가액", "supply_amount"),
            ("부가세", "vat_amount"),
            ("총매출", "revenue"),
            ("VAT 모드", "vat_mode"),
            ("진행상태", "status"),
            ("정산상태", "settlement_status"),
            ("세금계산서일", "invoice_date"),
            ("정산예정일", "settle_due_date"),
            ("입금일", "paid_date"),
            ("결제수단", "payment_method"),
            ("결제메모", "payment_memo"),
            ("증빙없는현금매출", "cash_no_invoice"),
            ("세무제외메모", "tax_excluded_note"),
            ("특이사항", "special_notes"),
            ("메모", "memo"),
        ],
        "unique_field": "code",
    },
    "하청외주지급": {
        "model": Expense,
        "fields": [
            ("프로젝트ID", "project_id"),
            ("발주일", "expense_date"),
            ("역할", "category"),
            ("거래처명", "vendor_name"),
            ("작업내용", "description"),
            ("상세사양", "spec_detail"),
            ("지급액", "amount"),
            ("결제수단", "payment_method"),
            ("지급상태", "pay_status"),
            ("지급예정일", "pay_due_date"),
            ("지급일", "pay_date"),
            ("지급메모", "pay_memo"),
            ("세금계산서", "has_tax_invoice"),
            ("첨부파일", "receipt_image"),
        ],
        "unique_field": None,  # 자유 추가
    },
    "근무자": {
        "model": Worker,
        "fields": [
            ("이름", "name"),
            ("전화", "phone"),
            ("은행", "bank"),
            ("계좌", "account"),
            ("기본일당", "default_daily_wage"),
            ("활성여부", "is_active"),
            ("메모", "memo"),
        ],
        "unique_field": "name",
    },
    "근태": {
        "model": Attendance,
        "fields": [
            ("프로젝트ID", "project_id"),
            ("근무자ID", "worker_id"),
            ("근무일", "work_date"),
            ("일수", "days"),
            ("일당", "daily_wage"),
            ("총임금", "total_wage"),
            ("지급상태", "pay_status"),
            ("지급일", "pay_date"),
            ("메모", "memo"),
        ],
        "unique_field": None,
    },
    "거래처": {
        "model": Vendor,
        "fields": [
            ("거래처명", "name"),
            ("담당자", "contact_person"),
            ("연락처", "phone"),
            ("이메일", "email"),
            ("주소", "address"),
            ("유형", "vendor_type"),
            ("사업자번호", "biz_number"),
            ("메모", "memo"),
        ],
        "unique_field": "name",
    },
    "장비": {
        "model": Equipment,
        "fields": [
            ("장비명", "name"),
            ("모델", "model"),
            ("제조사", "manufacturer"),
            ("카테고리", "category"),
            ("규격", "spec"),
            ("메모", "memo"),
        ],
        "unique_field": "name",
    },
    "장비개체": {
        "model": EquipmentUnit,
        "fields": [
            ("장비ID", "equipment_id"),
            ("관리코드", "asset_code"),
            ("시리얼", "serial_number"),
            ("구입처", "purchase_vendor"),
            ("구입일", "purchase_date"),
            ("구입금액", "purchase_price"),
            ("1일렌탈가", "rental_price_daily"),
            ("상태", "status"),
            ("보관위치", "location"),
            ("관리부서", "department"),
            ("담당자", "manager"),
            ("최근점검일", "last_check_date"),
            ("메모", "memo"),
        ],
        "unique_field": "asset_code",
    },
    "유지보수이력": {
        "model": MaintenanceLog,
        "fields": [
            ("장비개체ID", "unit_id"),
            ("일자", "log_date"),
            ("유형", "log_type"),
            ("제목", "title"),
            ("내용", "description"),
            ("업체", "vendor"),
            ("비용", "cost"),
            ("처리후상태", "status_after"),
        ],
        "unique_field": None,
    },
    "정직원출퇴근": {
        "model": WorkerCheckin,
        "fields": [
            ("근무자ID", "worker_id"),
            ("근무일", "work_date"),
            ("출근시각", "check_in"),
            ("퇴근시각", "check_out"),
            ("메모", "memo"),
        ],
        "unique_field": None,
    },
    "물품": {
        "model": Item,
        "fields": [
            ("품목코드", "code"),
            ("품명", "name"),
            ("규격", "spec"),
            ("단위", "unit"),
            ("납품단가", "consumer_price"),
            ("렌탈일단가", "rental_daily"),
            ("렌탈보증금", "rental_deposit"),
            ("카테고리", "category"),
            ("연결장비ID", "equipment_id"),
            ("활성여부", "is_active"),
            ("메모", "memo"),
        ],
        "unique_field": "code",
    },
    "견적서": {
        "model": Quote,
        "fields": [
            ("견적번호", "quote_number"),
            ("견적일", "quote_date"),
            ("견적유형", "quote_type"),
            ("프로젝트ID", "project_id"),
            ("거래처명", "vendor_name"),
            ("거래처담당자", "vendor_contact"),
            ("거래처연락처", "vendor_phone"),
            ("프로젝트명", "project_name"),
            ("행사일", "event_date"),
            ("렌탈일수", "rental_days"),
            ("공급가합계", "subtotal"),
            ("할인", "discount"),
            ("할인후공급가", "after_discount"),
            ("부가세", "vat"),
            ("총액", "total"),
            ("VAT모드", "vat_mode"),
            ("유효기간", "valid_until"),
            ("결제조건", "payment_terms"),
            ("납기조건", "delivery_terms"),
            ("비고", "notes"),
        ],
        "unique_field": "quote_number",
    },
    "견적서품목": {
        "model": QuoteItem,
        "fields": [
            ("견적서ID", "quote_id"),
            ("순서", "seq"),
            ("품명", "name"),
            ("규격", "spec"),
            ("수량", "quantity"),
            ("단위", "unit"),
            ("단가", "unit_price"),
            ("금액", "amount"),
            ("비고", "memo"),
        ],
        "unique_field": None,
    },
    "직원계정": {
        "model": User,
        "fields": [
            ("아이디", "username"),
            ("이름", "name"),
            ("전화번호", "phone"),
            ("역할", "role"),
            ("근무구분", "employee_type"),
            ("활성여부", "is_active"),
            ("권한설정", "permissions_json"),
        ],
        "unique_field": "username",
    },
    "회사설정": {
        "model": CompanySettings,
        "fields": [
            ("회사명", "company_name"),
            ("대표자", "representative"),
            ("사업자번호", "biz_number"),
            ("업태", "biz_type"),
            ("종목", "biz_item"),
            ("주소", "address"),
            ("연락처", "phone"),
            ("이메일", "email"),
            ("입금계좌", "bank_account"),
            ("견적서_결제조건", "default_payment_terms"),
            ("견적서_납기조건", "default_delivery_terms"),
            ("견적서_유효일", "default_valid_days"),
            ("견적서_하단특기", "quote_footer_notes"),
        ],
        "unique_field": None,
    },
}


def _cell_value(obj, field: str):
    """모델 필드 값을 Excel 셀 친화적인 값으로 변환"""
    v = getattr(obj, field, None)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "예" if v else "아니오"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _parse_value(value, field_name: str, model):
    """Excel 셀 값을 모델 필드 값으로 변환"""
    if value is None or value == "":
        return None
    # 모델의 타입 추론
    annotations = getattr(model, "__annotations__", {})
    target_type = annotations.get(field_name)
    s_val = str(value).strip()
    if s_val == "":
        return None

    # bool 처리 (한글/영문 모두 허용)
    if target_type in (bool, Optional[bool]) or field_name in (
        "is_active", "has_evidence", "cash_no_invoice"
    ):
        return s_val in ("예", "TRUE", "True", "true", "1", "Yes", "yes", "Y", "y")

    # int
    if field_name in (
        "vendor_id", "project_id", "worker_id", "equipment_id", "unit_id",
        "supply_amount", "vat_amount", "revenue", "amount", "daily_wage",
        "default_daily_wage", "total_wage", "purchase_price", "rental_price_daily",
        "cost",
    ):
        try:
            # 소수점/쉼표 제거
            cleaned = s_val.replace(",", "").split(".")[0]
            return int(cleaned) if cleaned else 0
        except (ValueError, TypeError):
            return 0

    # float
    if field_name in ("days",):
        try:
            return float(s_val.replace(",", ""))
        except (ValueError, TypeError):
            return 0.0

    # date
    if field_name in (
        "event_date", "invoice_date", "settle_due_date", "paid_date",
        "expense_date", "work_date", "pay_date", "pay_due_date",
        "quote_date", "valid_until",
        "purchase_date", "last_check_date", "log_date",
    ):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return datetime.fromisoformat(s_val).date()
        except (ValueError, TypeError):
            try:
                return datetime.strptime(s_val, "%Y-%m-%d").date()
            except Exception:
                return None

    # datetime (시간 포함)
    if field_name in ("check_in", "check_out", "created_at"):
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        try:
            return datetime.fromisoformat(s_val.replace("Z", "").replace("T", " "))
        except (ValueError, TypeError):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s_val, fmt)
                except (ValueError, TypeError):
                    continue
            return None

    # 기본 문자열
    return s_val


# ============================================================
# 메뉴 페이지
# ============================================================
@router.get("/backup", response_class=HTMLResponse)
def backup_page(request: Request):
    user = _user(request)
    # 완전 백업용 통계 (파일 개수 · 크기 미리보기)
    data_stats = _get_data_dir_stats()
    return templates.TemplateResponse(request, "backup.html", {
        "user": user, "schema": SHEET_SCHEMA,
        "is_admin": is_admin(user),
        "data_stats": data_stats,
    })


# ============================================================
# ⭐ 완전 백업 (DB + 이미지 파일 전부) — ZIP 다운로드
# ============================================================
def _get_data_dir_stats() -> dict:
    """data/ 폴더 통계 (UI 표시용)"""
    result = {
        "db_size_kb": 0,
        "receipt_count": 0,
        "receipt_size_kb": 0,
        "company_count": 0,
        "company_size_kb": 0,
        "worker_count": 0,
        "worker_size_kb": 0,
        "total_size_kb": 0,
    }
    try:
        db_path = Path(DB_PATH)
        if db_path.exists():
            result["db_size_kb"] = round(db_path.stat().st_size / 1024, 1)

        for key, subdir in [
            ("receipt", "receipts"),
            ("company", "company"),
            ("worker", "worker_photos"),
        ]:
            p = Path(DATA_DIR) / subdir
            if p.exists():
                files = [f for f in p.rglob("*") if f.is_file()]
                result[f"{key}_count"] = len(files)
                result[f"{key}_size_kb"] = round(sum(f.stat().st_size for f in files) / 1024, 1)
        result["total_size_kb"] = round(
            result["db_size_kb"] + result["receipt_size_kb"]
            + result["company_size_kb"] + result["worker_size_kb"], 1
        )
    except Exception:
        pass
    return result


@router.get("/backup/full-download")
def backup_full_download(request: Request):
    """DB + 영수증 + 로고 + 직원사진 전부를 하나의 ZIP으로 다운로드.
    복원 시 그대로 업로드하면 완전 복원 가능.
    """
    require_admin_user(request)

    # 메모리에 zip 생성 (파일 크기 보통 수 MB ~ 수십 MB)
    buf = io.BytesIO()
    manifest = {
        "backup_type": "full_backup_v1",
        "created_at": datetime.now().isoformat(),
        "app_name": "정산관리 시스템",
        "notes": "복원 시 관리자만 /backup 페이지에서 '완전 복원'으로 업로드하세요.",
        "files": [],
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        # 1) DB 파일 — SQLite 동시 접근 안전을 위해 임시 복사 후 zip
        db_src = Path(DB_PATH)
        if db_src.exists():
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                # SQLite는 파일 복사가 안전 (WAL 모드가 아니면)
                shutil.copy2(str(db_src), tmp_path)
                zf.write(tmp_path, arcname="data/app.db")
                manifest["files"].append({
                    "path": "data/app.db",
                    "size": Path(tmp_path).stat().st_size,
                })
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # 2) 이미지 폴더들 (receipts, company, worker_photos)
        for subdir in ["receipts", "company", "worker_photos"]:
            src = Path(DATA_DIR) / subdir
            if not src.exists():
                continue
            for f in src.rglob("*"):
                if not f.is_file():
                    continue
                # data/receipts/xxx.jpg 형식으로 zip 내부 저장
                rel = f.relative_to(Path(DATA_DIR))
                arcname = f"data/{rel.as_posix()}"
                zf.write(str(f), arcname=arcname)
                manifest["files"].append({
                    "path": arcname,
                    "size": f.stat().st_size,
                })

        # 3) manifest.json (파일 목록 · 생성 시각)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    buf.seek(0)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"정산관리_완전백업_{ts}.zip"
    from urllib.parse import quote
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
    }
    return StreamingResponse(buf, media_type="application/zip", headers=headers)


@router.post("/backup/full-restore")
async def backup_full_restore(
    request: Request,
    file: UploadFile = File(...),
    confirm: str = Form(""),
):
    """완전 백업 ZIP 파일에서 DB + 이미지 전부 복원.
    ⚠️ 기존 데이터는 완전히 대체됨 — 관리자 전용 + 확인 필수.
    """
    require_admin_user(request)

    if confirm != "REPLACE_ALL":
        raise HTTPException(400, "복원 확인 문자열이 일치하지 않습니다.")

    # 업로드 파일 임시 저장
    contents = await file.read()
    if not contents:
        raise HTTPException(400, "빈 파일입니다.")
    if len(contents) > 500 * 1024 * 1024:  # 500MB 상한
        raise HTTPException(400, "파일이 너무 큽니다 (500MB 초과).")

    try:
        zf = zipfile.ZipFile(io.BytesIO(contents), "r")
    except zipfile.BadZipFile:
        raise HTTPException(400, "ZIP 파일이 손상되었거나 형식이 잘못되었습니다.")

    # manifest 검증
    if "manifest.json" not in zf.namelist():
        raise HTTPException(400, "완전 백업 ZIP이 아닙니다 (manifest.json 없음).")
    try:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        if manifest.get("backup_type") != "full_backup_v1":
            raise HTTPException(400, "지원하지 않는 백업 형식입니다.")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "manifest.json 형식이 잘못되었습니다.")

    # 복원 전 현재 데이터를 백업 (안전장치)
    safety_dir = Path(DATA_DIR) / "_pre_restore_backup"
    safety_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safety_backup = safety_dir / f"before_restore_{ts}.db"
    if Path(DB_PATH).exists():
        try:
            shutil.copy2(DB_PATH, safety_backup)
        except OSError:
            pass  # 안전 백업 실패해도 복원은 진행

    # 기존 이미지 폴더 백업 (rename)
    for subdir in ["receipts", "company", "worker_photos"]:
        src = Path(DATA_DIR) / subdir
        if src.exists() and any(src.iterdir()):
            bak = safety_dir / f"{subdir}_{ts}"
            try:
                shutil.move(str(src), str(bak))
                src.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    # 실제 복원 — DB는 마지막에 (실행 중 파일 잠금 우려)
    db_content = None
    restored_files = 0
    for name in zf.namelist():
        if name == "manifest.json":
            continue
        # 경로 순회 공격 방지
        if name.startswith("/") or ".." in name.split("/"):
            continue
        # data/ 하위만 허용
        if not name.startswith("data/"):
            continue
        rel = name[5:]  # 'data/' 제거
        if not rel:
            continue
        target = Path(DATA_DIR) / rel

        if rel == "app.db":
            db_content = zf.read(name)  # 나중에 처리
            continue

        # 디렉토리 안전 생성
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zf.open(name) as src_f, open(target, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            restored_files += 1
        except OSError:
            pass

    # DB 최후 교체
    if db_content is not None:
        try:
            # 임시 파일로 쓰고 rename (원자적 교체)
            tmp_path = str(Path(DB_PATH).with_suffix(".db.restore_tmp"))
            with open(tmp_path, "wb") as f:
                f.write(db_content)
            # engine을 dispose하고 파일 교체
            try:
                engine.dispose()
            except Exception:
                pass
            shutil.move(tmp_path, DB_PATH)
            restored_files += 1
        except OSError as ex:
            raise HTTPException(500, f"DB 복원 실패: {ex}")

    return templates.TemplateResponse(request, "backup_restored.html", {
        "message": f"✅ 완전 복원 완료 — {restored_files}개 파일 복원됨",
        "detail": (
            f"복원 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}<br>"
            f"백업 생성 시각: {manifest.get('created_at', '(알 수 없음)')}<br>"
            f"안전 백업 저장 위치: <code>{safety_dir}</code><br><br>"
            f"⚠️ <b>중요:</b> 사용자 세션이 오래된 정보를 유지할 수 있으니, "
            f"로그아웃 후 다시 로그인해주세요."
        ),
    })


# ============================================================
# 2. Excel 내보내기 (전체 데이터)
# ============================================================
@router.get("/backup/export")
def export_excel(request: Request):
    _user(request)
    wb = Workbook()
    # 기본 시트 제거
    wb.remove(wb.active)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0A0E1A")
    header_align = Alignment(horizontal="center", vertical="center")

    with Session(engine, expire_on_commit=False) as s:
        for sheet_name, spec in SHEET_SCHEMA.items():
            ws = wb.create_sheet(sheet_name)
            # 헤더 행
            for col_idx, (label, _) in enumerate(spec["fields"], 1):
                cell = ws.cell(row=1, column=col_idx, value=label)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
            # 데이터 행
            rows = s.exec(select(spec["model"])).all()
            for row_idx, obj in enumerate(rows, 2):
                for col_idx, (_, field) in enumerate(spec["fields"], 1):
                    ws.cell(row=row_idx, column=col_idx, value=_cell_value(obj, field))
            # 컬럼 폭 자동
            for col_idx, (label, _) in enumerate(spec["fields"], 1):
                col_letter = ws.cell(row=1, column=col_idx).column_letter
                ws.column_dimensions[col_letter].width = max(12, len(label) * 2)
            # 첫 행 고정
            ws.freeze_panes = "A2"

    # 메모 시트 (도움말)
    info_ws = wb.create_sheet("_사용설명", 0)
    info_ws["A1"] = "정산관리 시스템 데이터 백업 파일"
    info_ws["A1"].font = Font(bold=True, size=14)
    info_ws["A3"] = f"내보낸 시각: {datetime.now().isoformat()}"
    info_ws["A5"] = "📌 불러올 때 주의사항:"
    info_ws["A6"] = "  1. 헤더 행(1행)은 절대 수정하지 마세요"
    info_ws["A7"] = "  2. ID 컬럼은 자동 부여되니 비워두셔도 됩니다"
    info_ws["A8"] = "  3. 일자는 'YYYY-MM-DD' 형식으로 입력"
    info_ws["A9"] = "  4. 예/아니오는 한글 그대로 또는 TRUE/FALSE 입력 가능"
    info_ws["A10"] = "  5. 같은 코드/이름이면 중복으로 간주되어 건너뜁니다"
    info_ws["A12"] = "📌 시트별 unique 키 (중복 방지 기준):"
    for sheet_name, spec in SHEET_SCHEMA.items():
        unique = spec.get("unique_field") or "(없음 - 무조건 추가)"
        info_ws[f"A{14 + list(SHEET_SCHEMA.keys()).index(sheet_name)}"] = f"  · {sheet_name}: {unique}"
    info_ws.column_dimensions["A"].width = 80

    # 스트리밍 응답
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"정산관리_백업_{date.today().isoformat()}.xlsx"
    # 한글 파일명 RFC 5987
    from urllib.parse import quote
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
    }
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


# ============================================================
# 2-B. 레거시 양식 Excel 내보내기 (이전 정산관리 템플릿 호환)
#     - 사람이 읽기 쉬운 이모지 시트명 + 한글 컬럼명
#     - 사용자의 익숙한 엑셀 양식 그대로 백업/공유 가능
# ============================================================
@router.get("/backup/export-legacy")
def export_excel_legacy(request: Request):
    """이전 정산관리 엑셀 양식으로 내보내기 — 사용자에게 익숙한 형식"""
    _user(request)
    wb = Workbook()
    wb.remove(wb.active)
    title_font = Font(bold=True, size=14, color="0A0E1A")
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0A0E1A")
    header_align = Alignment(horizontal="center", vertical="center")

    with Session(engine, expire_on_commit=False) as s:
        # ───── 📋 프로젝트목록 ─────
        ws = wb.create_sheet("📋 프로젝트목록")
        ws["A1"] = "프로젝트 목록 · 정산 현황"
        ws["A1"].font = title_font
        headers = ["프로젝트명", "거래처명", "담당자", "연락처", "행사일",
                   "계산서 발행일", "프로젝트 금액(매출)", "정산예정일", "실제 정산일",
                   "정산상태", "진행상태", "프로젝트 코드", "장소", "메모"]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(2, c, h); cell.font = header_font; cell.fill = header_fill; cell.alignment = header_align
        vendors_map = {v.id: v for v in s.exec(select(Vendor)).all()}
        for i, p in enumerate(s.exec(select(Project)).all(), 3):
            v = vendors_map.get(p.vendor_id) if p.vendor_id else None
            ws.cell(i, 1, p.name); ws.cell(i, 2, v.name if v else "")
            ws.cell(i, 3, v.contact_person if v else ""); ws.cell(i, 4, v.phone if v else "")
            ws.cell(i, 5, p.event_date.isoformat() if p.event_date else "")
            ws.cell(i, 6, p.invoice_date.isoformat() if p.invoice_date else "")
            ws.cell(i, 7, p.revenue or 0)
            ws.cell(i, 8, p.settle_due_date.isoformat() if p.settle_due_date else "")
            ws.cell(i, 9, p.paid_date.isoformat() if p.paid_date else "")
            ws.cell(i, 10, p.settlement_status or "")
            ws.cell(i, 11, p.status or "")
            ws.cell(i, 12, p.code or "")
            ws.cell(i, 13, p.location or "")
            ws.cell(i, 14, p.memo or "")
        ws.freeze_panes = "A3"
        for col, width in zip("ABCDEFGHIJKLMN", [25,18,12,15,12,12,16,12,12,10,10,15,15,25]):
            ws.column_dimensions[col].width = width

        # ───── 💰 비용처리 ─────
        ws = wb.create_sheet("💰 비용처리")
        ws["A1"] = "비용처리 상세내역 (전체 저장소)"
        ws["A1"].font = title_font
        headers = ["프로젝트명", "지출일자", "비용항목", "거래처/지급처", "내용", "금액", "결제수단", "증빙여부", "비고"]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(2, c, h); cell.font = header_font; cell.fill = header_fill; cell.alignment = header_align
        projects_map = {p.id: p for p in s.exec(select(Project)).all()}
        for i, e in enumerate(s.exec(select(Expense)).all(), 3):
            p = projects_map.get(e.project_id)
            ws.cell(i, 1, p.name if p else "")
            ws.cell(i, 2, e.expense_date.isoformat() if e.expense_date else "")
            ws.cell(i, 3, e.category or "")
            ws.cell(i, 4, getattr(e, "vendor_name", "") or "")
            ws.cell(i, 5, e.description or "")
            ws.cell(i, 6, e.amount or 0)
            ws.cell(i, 7, e.payment_method or "")
            ws.cell(i, 8, "있음" if getattr(e, "has_evidence", True) else "없음")
            ws.cell(i, 9, getattr(e, "tax_excluded_note", "") or "")
        ws.freeze_panes = "A3"
        for col, width in zip("ABCDEFGHI", [25,12,12,18,30,12,12,10,20]):
            ws.column_dimensions[col].width = width

        # ───── 👥 알바인건비 ─────
        ws = wb.create_sheet("👥 알바인건비")
        ws["A1"] = "알바·인건비 상세내역"
        ws["A1"].font = title_font
        headers = ["프로젝트명", "근무자명", "연락처", "근무일자", "일수", "일당", "추가금액", "지급액", "지급상태", "지급일", "메모"]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(2, c, h); cell.font = header_font; cell.fill = header_fill; cell.alignment = header_align
        workers_map = {w.id: w for w in s.exec(select(Worker)).all()}
        for i, a in enumerate(s.exec(select(Attendance)).all(), 3):
            p = projects_map.get(a.project_id); w = workers_map.get(a.worker_id)
            ws.cell(i, 1, p.name if p else "")
            ws.cell(i, 2, w.name if w else "")
            ws.cell(i, 3, w.phone if w else "")
            ws.cell(i, 4, a.work_date.isoformat() if a.work_date else "")
            ws.cell(i, 5, a.days or 1)
            ws.cell(i, 6, a.daily_wage or 0)
            ws.cell(i, 7, a.bonus_amount or 0)
            ws.cell(i, 8, a.total_wage or 0)
            ws.cell(i, 9, a.pay_status or "미지급")
            ws.cell(i, 10, a.pay_date.isoformat() if a.pay_date else "")
            ws.cell(i, 11, a.memo or "")
        ws.freeze_panes = "A3"
        for col, width in zip("ABCDEFGHIJK", [25,12,15,12,8,12,12,14,10,12,20]):
            ws.column_dimensions[col].width = width

        # ───── 🏢 거래처별집계 ─────
        ws = wb.create_sheet("🏢 거래처별집계")
        ws["A1"] = "거래처별 매출 집계"
        ws["A1"].font = title_font
        headers = ["거래처명", "담당자", "연락처", "사업자번호", "이메일", "주소", "메모"]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(2, c, h); cell.font = header_font; cell.fill = header_fill; cell.alignment = header_align
        for i, v in enumerate(s.exec(select(Vendor).order_by(Vendor.name)).all(), 3):
            ws.cell(i, 1, v.name); ws.cell(i, 2, v.contact_person or "")
            ws.cell(i, 3, v.phone or ""); ws.cell(i, 4, v.biz_number or "")
            ws.cell(i, 5, v.email or ""); ws.cell(i, 6, v.address or "")
            ws.cell(i, 7, v.memo or "")
        ws.freeze_panes = "A3"

        # ───── 📦 물품단가표 ─────
        ws = wb.create_sheet("📦 물품단가표")
        ws["A1"] = "물품 단가표"
        ws["A1"].font = title_font
        headers = ["품목코드", "품명", "규격", "단위", "납품단가", "렌탈일단가", "렌탈보증금", "카테고리", "비고"]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(2, c, h); cell.font = header_font; cell.fill = header_fill; cell.alignment = header_align
        for i, it in enumerate(s.exec(select(Item).where(Item.is_active == True)).all(), 3):
            ws.cell(i, 1, it.code or ""); ws.cell(i, 2, it.name); ws.cell(i, 3, it.spec or "")
            ws.cell(i, 4, it.unit or ""); ws.cell(i, 5, it.consumer_price or 0)
            ws.cell(i, 6, it.rental_daily or 0); ws.cell(i, 7, it.rental_deposit or 0)
            ws.cell(i, 8, it.category or ""); ws.cell(i, 9, it.memo or "")
        ws.freeze_panes = "A3"

        # ───── 📚 견적이력 ─────
        ws = wb.create_sheet("📚 견적이력")
        ws["A1"] = "견적 이력 관리"
        ws["A1"].font = title_font
        headers = ["견적번호", "견적일자", "거래처명", "담당자", "프로젝트명", "공급가액", "할인금액", "총 합계액", "상태", "비고"]
        for c, h in enumerate(headers, 1):
            cell = ws.cell(2, c, h); cell.font = header_font; cell.fill = header_fill; cell.alignment = header_align
        for i, q in enumerate(s.exec(select(Quote)).all(), 3):
            ws.cell(i, 1, q.quote_number); ws.cell(i, 2, q.quote_date.isoformat() if q.quote_date else "")
            ws.cell(i, 3, q.vendor_name or ""); ws.cell(i, 4, q.vendor_contact or "")
            ws.cell(i, 5, q.project_name or ""); ws.cell(i, 6, q.subtotal or 0)
            ws.cell(i, 7, q.discount or 0); ws.cell(i, 8, q.total or 0)
            ws.cell(i, 9, q.status or ""); ws.cell(i, 10, q.notes or "")
        ws.freeze_panes = "A3"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"정산관리_{date.today().isoformat()}.xlsx"
    from urllib.parse import quote
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ============================================================
# 3. Excel 미리보기 + 임시 저장
# ============================================================
# 업로드된 파일을 일시적으로 메모리에 저장 (간단화: 세션 기반)
_PREVIEW_CACHE = {}  # user_id -> {sheet_name: [rows]}


# ============================================================
# 레거시 엑셀 양식(이모지 시트명·한글 컬럼)도 받아주는 시트명 별칭 + 컬럼 별칭
# ============================================================
SHEET_ALIASES = {
    # 표준 시트명 ← 입력 가능한 별칭들
    "프로젝트": ["📋 프로젝트목록", "프로젝트목록", "프로젝트 목록"],
    "비용": ["💰 비용처리", "비용처리", "지출내역"],
    "근태": ["👥 알바인건비", "알바인건비", "알바 인건비"],
    "근무자": ["근무자목록", "👥 근무자"],
    "거래처": ["🏢 거래처별집계", "거래처별집계", "거래처 목록"],
    "물품": ["📦 물품단가표", "물품단가표"],
    "장비": ["🎬 렌탈단가표", "렌탈단가표"],
    "견적서": ["📚 견적이력", "견적이력", "📝 견적서"],
}

# 레거시 컬럼명 ← 표준 필드명 매핑 (시트별)
COLUMN_ALIASES = {
    "프로젝트": {
        "프로젝트명": "name", "거래처명": "vendor_name_text",  # 거래처명은 별도 처리
        "담당자": "contact", "연락처": "phone",
        "행사 시작일": "event_date", "행사일": "event_date",
        "행사 종료일": "event_end_date",
        "계산서 발행일": "invoice_date", "세금계산서일": "invoice_date",
        "프로젝트 금액(매출)": "revenue", "프로젝트 금액": "revenue", "매출": "revenue", "총매출": "revenue",
        "공급가액": "supply_amount", "부가세": "vat_amount",
        "정산예정일": "settle_due_date", "실제 정산일": "paid_date", "입금일": "paid_date",
        "정산상태": "settlement_status", "진행상태": "status",
        "프로젝트 코드": "code", "코드": "code",
        "장소": "location", "VAT 모드": "vat_mode",
        "결제수단": "payment_method", "결제메모": "payment_memo",
        "특이사항": "special_notes", "메모": "memo",
    },
    "비용": {
        "프로젝트명": "project_name_text",  # 이름으로 매칭 (별도 처리)
        "프로젝트ID": "project_id",
        "지출일자": "expense_date", "날짜": "expense_date",
        "비용항목": "category", "항목": "category", "분류": "category",
        "거래처/지급처": "vendor_name", "거래처명": "vendor_name", "지급처": "vendor_name",
        "내용": "description", "적요": "description",
        "금액": "amount",
        "결제수단": "payment_method", "지급방법": "payment_method",
        "증빙여부": "has_evidence_text",  # "세금계산서"/"카드매출전표"/"없음" 등
        "비고": "memo", "메모": "memo",
    },
    "근태": {
        "프로젝트명": "project_name_text",
        "프로젝트ID": "project_id",
        "근무자명": "worker_name_text", "근무자ID": "worker_id",
        "연락처": "phone",
        "근무일자": "work_date", "근무일": "work_date",
        "근무시작": "_check_in_time", "근무종료": "_check_out_time",
        "시급": "hourly_wage",  # 일당 자동 환산
        "일당": "daily_wage", "일수": "days",
        "지급액(자동)": "total_wage", "지급액": "total_wage", "총임금": "total_wage",
        "지급상태": "pay_status", "지급일": "pay_date",
        "지급방법": "payment_method", "비고": "memo", "메모": "memo",
    },
    "거래처": {
        "거래처명": "name",
        "프로젝트 수": "_skip", "총 매출액": "_skip", "정산완료액": "_skip", "미수금액": "_skip",
        "최근 거래일": "_skip",
        "담당자": "contact_person", "연락처": "phone", "이메일": "email",
        "주소": "address", "유형": "vendor_type",
        "사업자번호": "biz_number", "메모": "memo",
    },
    "물품": {
        "품목코드": "code", "품명": "name", "품목명": "name",
        "규격/사양": "spec", "규격": "spec",
        "단위": "unit",
        "수입단가": "_skip",  # 시스템엔 없는 필드
        "대리점가": "_skip", "소비자가": "consumer_price",
        "대리점마진율": "_skip", "소비자마진율": "_skip",
        "납품단가": "consumer_price", "렌탈일단가": "rental_daily", "렌탈보증금": "rental_deposit",
        "카테고리": "category", "비고": "memo", "메모": "memo",
    },
    "장비": {
        "장비코드": "_skip",  # 시스템은 자동 부여
        "장비명": "name", "카테고리": "category", "규격/사양": "spec", "규격": "spec",
        "단위": "_skip", "1일 단가": "_skip", "보증금": "_skip",
        "모델": "model", "제조사": "manufacturer", "비고": "memo", "메모": "memo",
    },
    "견적서": {
        "견적번호": "quote_number", "견적일자": "quote_date", "견적일": "quote_date",
        "거래처명": "vendor_name", "담당자": "vendor_contact",
        "프로젝트명": "project_name",
        "공급가액": "subtotal", "할인금액": "discount", "총 합계액": "total", "총액": "total",
        "상태": "status", "비고": "notes", "메모": "notes",
    },
}


def _resolve_sheet(sheet_name: str) -> Optional[str]:
    """엑셀의 시트명을 표준 시트명으로 매핑 — 별칭 해석"""
    sheet_name = (sheet_name or "").strip()
    if sheet_name in SHEET_SCHEMA:
        return sheet_name
    for std, aliases in SHEET_ALIASES.items():
        if sheet_name == std or sheet_name in aliases:
            return std
    return None


def _parse_evidence_text(s) -> bool:
    """레거시 '증빙여부' 텍스트를 has_evidence 불리언으로"""
    if s is None:
        return True
    txt = str(s).strip()
    if not txt or txt in ("없음", "X", "x", "-", "아니오", "N", "n"):
        return False
    return True


def _hours_to_days(start_t, end_t) -> float:
    """레거시 '근무시작/근무종료' 텍스트(HH:MM)에서 일수 환산"""
    if not start_t or not end_t:
        return 1.0
    try:
        # datetime.time 객체 또는 "HH:MM" 문자열
        def _to_min(t):
            if hasattr(t, 'hour'):
                return t.hour * 60 + t.minute
            t = str(t).strip()
            if ':' in t:
                h, m = t.split(':')[:2]
                return int(h) * 60 + int(m)
            return 0
        total_min = _to_min(end_t) - _to_min(start_t)
        if total_min <= 0:
            return 1.0
        # 8시간을 1일로 환산 (간단 기준)
        return round(total_min / 60 / 8, 2)
    except Exception:
        return 1.0


@router.post("/backup/preview")
async def preview_upload(request: Request, file: UploadFile = File(...)):
    user = _user(request)
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Excel 파일(.xlsx)만 업로드 가능합니다")

    content = await file.read()
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise HTTPException(400, f"파일을 읽을 수 없습니다: {e}")

    preview = {}
    issues = []
    # 시트별 처리 — 표준 시트명 + 레거시 별칭 모두 받음
    for ws_name in wb.sheetnames:
        std_sheet = _resolve_sheet(ws_name)
        if not std_sheet:
            continue
        spec = SHEET_SCHEMA[std_sheet]
        ws = wb[ws_name]
        rows_iter = ws.iter_rows(values_only=True)

        # 헤더 행 탐지 — 첫 5행 안에서 가장 일치도 높은 행을 헤더로 채택
        label_to_field = {label: field for label, field in spec["fields"]}
        # 레거시 컬럼 별칭 병합
        legacy_aliases = COLUMN_ALIASES.get(std_sheet, {})
        for legacy_label, std_field in legacy_aliases.items():
            label_to_field[legacy_label] = std_field

        header_row_idx = None
        header = None
        candidates = []
        for i in range(5):
            try:
                row = next(rows_iter)
            except StopIteration:
                break
            candidates.append(row)
        # 일치도가 가장 높은 후보 채택
        best_match = -1
        for i, cand in enumerate(candidates):
            match = sum(1 for h in cand if h and str(h).strip() in label_to_field)
            if match > best_match:
                best_match = match
                header = cand
                header_row_idx = i
        if not header or best_match < 1:
            issues.append(f"[{ws_name}] 알 수 있는 헤더가 없습니다 (시트는 인식, 컬럼 매칭 실패)")
            continue

        # 컬럼 → 필드 매핑
        col_to_field = {}
        for col_idx, h in enumerate(header):
            if h is None: continue
            label = str(h).strip()
            if label in label_to_field:
                col_to_field[col_idx] = label_to_field[label]

        # 데이터 행 파싱 (헤더 다음 행부터 끝까지)
        # 위에서 next() 한 행은 이미 소비됨 → 나머지 candidates + 이후 rows_iter
        all_data_rows = candidates[header_row_idx + 1:]
        for r in rows_iter:
            all_data_rows.append(r)

        parsed_rows = []
        for row in all_data_rows:
            if not row or all(v is None or v == "" for v in row):
                continue
            d = {}
            # 임시 필드 (project_name_text 등) 저장
            extras = {}
            for col_idx, val in enumerate(row):
                if col_idx not in col_to_field:
                    continue
                field = col_to_field[col_idx]
                if field == "_skip":
                    continue
                # 수식 결과가 빈 문자열인 경우
                if isinstance(val, str) and val.startswith("="):
                    continue
                # 임시 필드는 extras에 보관 (후처리)
                if field.startswith("_") or field.endswith("_text") or field in ("hourly_wage",):
                    extras[field] = val
                    continue
                d[field] = _parse_value(val, field, spec["model"])

            # 시트별 후처리 (레거시 → 표준)
            if std_sheet == "비용":
                # has_evidence 변환
                if "has_evidence_text" in extras:
                    d["has_evidence"] = _parse_evidence_text(extras["has_evidence_text"])
                # 프로젝트명 → project_id 해석 (DB 조회)
                pn = extras.get("project_name_text")
                if pn and not d.get("project_id"):
                    with Session(engine, expire_on_commit=False) as ss:
                        proj = ss.exec(select(Project).where(Project.name == str(pn).strip())).first()
                        if proj:
                            d["project_id"] = proj.id
            elif std_sheet == "근태":
                # 시급 + 시작/종료 → daily_wage + days
                hr = extras.get("hourly_wage")
                ci_t = extras.get("_check_in_time")
                co_t = extras.get("_check_out_time")
                if hr and ci_t and co_t:
                    days = _hours_to_days(ci_t, co_t)
                    try:
                        hr_int = int(float(str(hr).replace(",", "")))
                    except (ValueError, TypeError):
                        hr_int = 0
                    if not d.get("days"):
                        d["days"] = days
                    if not d.get("daily_wage"):
                        d["daily_wage"] = hr_int * 8  # 8시간 기준 일당
                # 프로젝트명 → project_id
                pn = extras.get("project_name_text")
                if pn and not d.get("project_id"):
                    with Session(engine, expire_on_commit=False) as ss:
                        proj = ss.exec(select(Project).where(Project.name == str(pn).strip())).first()
                        if proj:
                            d["project_id"] = proj.id
                # 근무자명 → worker_id
                wn = extras.get("worker_name_text")
                if wn and not d.get("worker_id"):
                    with Session(engine, expire_on_commit=False) as ss:
                        worker = ss.exec(select(Worker).where(Worker.name == str(wn).strip())).first()
                        if worker:
                            d["worker_id"] = worker.id
            elif std_sheet == "프로젝트":
                # vendor_name_text → Vendor를 찾거나 자동 생성
                vn = extras.get("vendor_name_text")
                if vn and not d.get("vendor_id"):
                    with Session(engine, expire_on_commit=False) as ss:
                        v = ss.exec(select(Vendor).where(Vendor.name == str(vn).strip())).first()
                        if not v:
                            v = Vendor(name=str(vn).strip())
                            ss.add(v); ss.commit(); ss.refresh(v)
                        d["vendor_id"] = v.id

            if d:
                parsed_rows.append(d)
        if std_sheet in preview:
            preview[std_sheet].extend(parsed_rows)
        else:
            preview[std_sheet] = parsed_rows
        issues.append(f"[{ws_name}] → '{std_sheet}' 시트 인식, {len(parsed_rows)}건 파싱됨")

    # 캐시에 저장 (간단 메모리 보관, 5분 TTL은 생략 — 다음 업로드 시 덮어씀)
    _PREVIEW_CACHE[user.id] = preview

    # 요약 통계
    summary = []
    for sheet_name, rows in preview.items():
        spec = SHEET_SCHEMA[sheet_name]
        unique_field = spec.get("unique_field")
        # 중복 체크 — 기존 DB에 이미 있는 행 수
        existing_count = 0
        new_count = 0
        if unique_field:
            with Session(engine, expire_on_commit=False) as s:
                existing_keys = set()
                existing_objs = s.exec(select(spec["model"])).all()
                for o in existing_objs:
                    k = getattr(o, unique_field, None)
                    if k is not None and k != "":
                        existing_keys.add(str(k))
                for r in rows:
                    key = r.get(unique_field)
                    if key is not None and str(key) in existing_keys:
                        existing_count += 1
                    else:
                        new_count += 1
        else:
            new_count = len(rows)
        summary.append({
            "sheet": sheet_name,
            "total": len(rows),
            "new": new_count,
            "duplicate": existing_count,
            "unique_field": unique_field or "(자유 추가)",
        })

    return templates.TemplateResponse(request, "backup_preview.html", {
        "user": user, "summary": summary, "issues": issues,
        "has_preview": bool(preview),
    })


@router.post("/backup/apply")
def apply_upload(request: Request):
    user = _user(request)
    preview = _PREVIEW_CACHE.get(user.id)
    if not preview:
        raise HTTPException(400, "미리보기 데이터가 없습니다. 다시 업로드해주세요.")

    inserted = {}
    skipped = {}
    updated = {}
    errors = []

    # 싱글톤 테이블 (id=1 한 건만 존재) — INSERT 대신 UPDATE
    SINGLETON_SHEETS = {"회사설정"}

    for sheet_name, rows in preview.items():
        spec = SHEET_SCHEMA[sheet_name]
        unique_field = spec.get("unique_field")
        inserted[sheet_name] = 0
        skipped[sheet_name] = 0
        updated[sheet_name] = 0

        # 시트별로 독립 트랜잭션 — 하나가 실패해도 다른 시트는 정상 처리
        try:
            with Session(engine, expire_on_commit=False) as s:
                # ───── 싱글톤 시트 (회사설정 등) — UPDATE 처리 ─────
                if sheet_name in SINGLETON_SHEETS:
                    if not rows:
                        continue
                    row_data = rows[0]
                    existing = s.exec(select(spec["model"])).first()
                    if existing:
                        # 기존 레코드 업데이트 (None 아닌 값만)
                        for k, v in row_data.items():
                            if k == "id" or v is None:
                                continue
                            if hasattr(existing, k):
                                setattr(existing, k, v)
                        s.add(existing)
                        updated[sheet_name] += 1
                    else:
                        # 없으면 새로 INSERT (id 제거)
                        clean = {k: v for k, v in row_data.items() if k != "id" and v is not None}
                        try:
                            obj = spec["model"](**clean)
                            s.add(obj)
                            inserted[sheet_name] += 1
                        except Exception as ex:
                            skipped[sheet_name] += 1
                            errors.append(f"[{sheet_name}] 신규 추가 실패: {ex}")
                    s.commit()
                    continue

                # ───── 일반 시트 ─────
                # 기존 unique 키 수집
                existing_keys = set()
                if unique_field:
                    existing_objs = s.exec(select(spec["model"])).all()
                    for o in existing_objs:
                        k = getattr(o, unique_field, None)
                        if k is not None and k != "":
                            existing_keys.add(str(k))

                for idx, r in enumerate(rows):
                    # ID 컬럼 제거 — 항상 DB가 자동 부여
                    clean = {k: v for k, v in r.items() if v is not None and k != "id"}
                    if not clean:
                        skipped[sheet_name] += 1
                        continue

                    # 중복 검사 (unique_field 있을 때만)
                    if unique_field:
                        key = clean.get(unique_field)
                        if key is not None and str(key) in existing_keys:
                            skipped[sheet_name] += 1
                            continue
                        if key is not None:
                            existing_keys.add(str(key))

                    # 모델이 받는 필드만 남김 — 알 수 없는 필드 제거
                    model_fields = set(getattr(spec["model"], "__fields__", {}).keys())
                    if model_fields:
                        clean = {k: v for k, v in clean.items() if k in model_fields}

                    try:
                        obj = spec["model"](**clean)
                        s.add(obj)
                        # 행 단위로 flush — 한 행 실패가 다른 행에 안 번지게
                        s.flush()
                        inserted[sheet_name] += 1
                    except Exception as ex:
                        s.rollback()
                        skipped[sheet_name] += 1
                        errors.append(f"[{sheet_name}] 행 {idx+1} 실패: {str(ex)[:120]}")
                s.commit()
        except Exception as ex:
            errors.append(f"[{sheet_name}] 전체 처리 실패: {str(ex)[:200]}")

    # 캐시 삭제
    _PREVIEW_CACHE.pop(user.id, None)

    return templates.TemplateResponse(request, "backup_done.html", {
        "user": user, "inserted": inserted, "skipped": skipped,
        "updated": updated, "errors": errors,
    })


@router.post("/backup/cancel")
def cancel_upload(request: Request):
    user = _user(request)
    _PREVIEW_CACHE.pop(user.id, None)
    return RedirectResponse("/backup", status_code=303)
