"""SQLite 데이터베이스 + 모델 정의"""
import hashlib
import os
import secrets
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from sqlmodel import SQLModel, Field, create_engine, Session

# 데이터 저장 경로:
# - 로컬:   data/ (프로젝트 폴더 내)
# - 클라우드: DATA_DIR 환경변수로 지정 (/data) — Fly volume 등
# - 데스크톱 앱: launcher.py가 OS별 사용자 디렉토리 지정
DATA_DIR = os.environ.get("DATA_DIR", "data")
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(f"{DATA_DIR}/receipts").mkdir(parents=True, exist_ok=True)
Path(f"{DATA_DIR}/company").mkdir(parents=True, exist_ok=True)

DB_PATH = f"{DATA_DIR}/app.db"
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, connect_args={"check_same_thread": False})


# ============================================================
# 모델
# ============================================================

class User(SQLModel, table=True):
    """사용자 (관리자/직원)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    name: str
    role: str = "staff"  # admin / staff
    phone: str = ""
    is_active: bool = True
    # 직원 권한 (admin은 무조건 모두 허용, staff만 적용)
    # JSON: {"projects": true, "expenses": true, "attendance": true, "quotes": false,
    #        "products": false, "vendors": false, "equipment": false, "view_amounts": false}
    permissions_json: str = "{}"
    employee_type: str = "정직원"  # 정직원 / 알바생 / 관리자 (정직원만 출퇴근 버튼 사용)
    worker_id: Optional[int] = Field(default=None, foreign_key="worker.id", index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Vendor(SQLModel, table=True):
    """거래처"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    contact_person: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    vendor_type: str = "consumer"  # consumer(소비자) / dealer(대리점)
    biz_number: str = ""  # 사업자번호
    memo: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Project(SQLModel, table=True):
    """프로젝트(행사)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(unique=True, index=True)  # P-2026-001
    name: str
    vendor_id: Optional[int] = Field(default=None, foreign_key="vendor.id")
    event_date: Optional[date] = None
    location: str = ""
    revenue: int = 0  # 총 매출액 (공급가액 + 부가세) — 표시·집계용 단일 진실
    supply_amount: int = 0  # 공급가액 (부가세 별도 금액)
    vat_amount: int = 0  # 부가세액
    vat_mode: str = "supply"  # 'supply' = 공급가 입력 / 'total' = 총액 입력 / 'cash' = 현금거래(무증빙)
    categories: str = ""  # 카테고리 (쉼표 구분): "음향,조명,영상" 등
    status: str = "준비중"  # 준비중/진행중/완료/취소
    settlement_status: str = "미수금"  # 미수금/입금완료
    invoice_date: Optional[date] = None  # 세금계산서 발행일
    settle_due_date: Optional[date] = None  # 정산예정일
    paid_date: Optional[date] = None        # 실제 입금일 (결제 완료 일자)
    payment_method: str = ""                # 현금/카드/계좌이체/세금계산서(추후)
    payment_memo: str = ""                  # 결제 관련 메모 (예: '카드 5/15 결제')
    cash_no_invoice: bool = False           # 세금계산서 없이 현금으로 받은 매출 (증빙 없음)
    tax_excluded_note: str = ""             # 세무 신고 제외 사유 메모 (증빙없는 현금매출)
    special_notes: str = ""                 # 프로젝트별 특이사항 (자유 입력, 여러 줄)
    memo: str = ""                          # 일반 메모
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Expense(SQLModel, table=True):
    """비용처리 (재료비/식비/교통비/기타)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    expense_date: date
    category: str  # 재료비/식비/교통비/장비대여/기타
    vendor_id: Optional[int] = Field(default=None, foreign_key="vendor.id", index=True)  # 거래처 마스터 연결 (선택)
    vendor_name: str = ""  # 텍스트 — vendor_id 없는 경우 직접 입력값 / 있는 경우 캐시
    description: str
    amount: int  # 금액
    payment_method: str = "현금"  # 현금/카드/계좌이체
    receipt_image: str = ""  # 영수증 사진 파일명
    has_evidence: bool = True  # 증빙(영수증·세금계산서·카드매출전표) 여부
    tax_excluded_note: str = ""  # 세무 신고 제외 사유 메모 (증빙없는 거래 시)
    registered_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Worker(SQLModel, table=True):
    """근무자 마스터 (정직원 / 알바)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    phone: str = ""
    bank: str = ""
    account: str = ""
    default_daily_wage: int = 150000  # 기본 일당 (15만원)
    employee_type: str = "알바"       # 정직원 / 알바
    monthly_salary: int = 0           # 정직원의 경우 월급
    resident_no: str = ""             # 주민등록번호 (000000-0000000 형식)
    photo_filename: str = ""          # 사진 파일명
    # 정직원 인사 정보
    hire_date: Optional[date] = None         # 입사일
    resign_date: Optional[date] = None       # 퇴사일 (재직중이면 None)
    employment_status: str = "재직"           # 재직 / 휴직 / 퇴사
    position: str = ""                        # 직책/직급
    memo: str = ""
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class WorkerCheckin(SQLModel, table=True):
    """정직원 출퇴근 기록 (일자별 출근/퇴근 시간)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    worker_id: int = Field(foreign_key="worker.id", index=True)
    work_date: date = Field(index=True)
    check_in: Optional[datetime] = None      # 출근 시각
    check_out: Optional[datetime] = None     # 퇴근 시각
    project_id: Optional[int] = Field(default=None, foreign_key="project.id")  # 선택사항
    memo: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Attendance(SQLModel, table=True):
    """알바 근무 기록 (일당 방식)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="project.id", index=True)
    worker_id: int = Field(foreign_key="worker.id", index=True)
    work_date: date
    days: float = 1.0  # 근무 일수 (0.5일 가능)
    daily_wage: int = 150000  # 일당 단가
    total_wage: int = 0  # 일수 × 일당 + 추가금액(bonus_amount)
    bonus_amount: int = 0  # 오퍼비/추가금액 (별도 책정되는 금액)
    bonus_memo: str = ""    # 추가금액 사유 메모 (예: "오퍼비", "야간수당")
    pay_status: str = "미지급"  # 미지급/지급완료
    pay_date: Optional[date] = None
    registered_by: Optional[int] = Field(default=None, foreign_key="user.id")
    memo: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Item(SQLModel, table=True):
    """단가표 (납품가 + 렌탈가 동시 보유 가능)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(unique=True, index=True)  # I-001
    name: str = Field(index=True)
    spec: str = ""
    unit: str = "EA"
    import_price: int = 0       # 수입단가 (내부 원가)
    dealer_price: int = 0       # 대리점가 (내부 원가)
    consumer_price: int = 0     # 납품 단가 (소비자가)
    rental_daily: int = 0       # 렌탈 1일 단가
    rental_deposit: int = 0     # 렌탈 보증금
    # 호환성: category 필드는 유지하되 "겸용"이 기본값 (기존 데이터는 마이그레이션에서 변환)
    category: str = "겸용"      # 겸용/물품/렌탈 (구분용 - 점진적 제거 예정)
    # 보유장비 연동
    equipment_id: Optional[int] = Field(default=None, foreign_key="equipment.id", index=True)
    memo: str = ""
    is_active: bool = True


class Quote(SQLModel, table=True):
    """견적서 헤더"""
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_number: str = Field(unique=True, index=True)  # Q-2026-001
    quote_date: date
    event_date: Optional[date] = None    # 행사일 (선택)
    quote_type: str = "납품"  # 납품 / 렌탈
    rental_days: int = 1  # 렌탈일 경우 사용 일수
    vendor_id: Optional[int] = Field(default=None, foreign_key="vendor.id")
    vendor_name: str = ""  # 직접 입력도 가능
    vendor_contact: str = ""
    vendor_phone: str = ""
    project_name: str = ""
    subtotal: int = 0
    discount: int = 0
    after_discount: int = 0
    vat: int = 0
    total: int = 0
    # 부가세 모드: 'supply'(공급가, VAT 10% 추가) / 'total'(총액, VAT 자동 분리) / 'cash'(현금 무증빙, VAT 없음)
    vat_mode: str = "supply"
    status: str = "견적"  # 견적/수주/실주/보류/취소
    valid_until: Optional[date] = None
    payment_terms: str = "세금계산서 발행 후 30일 이내 입금"
    delivery_terms: str = "발주일로부터 협의 후 결정"
    notes: str = ""
    # 수주 확정 시 자동 생성된 프로젝트 ID (없으면 미연결)
    project_id: Optional[int] = Field(default=None, foreign_key="project.id", index=True)
    created_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class QuoteItem(SQLModel, table=True):
    """견적서 품목"""
    id: Optional[int] = Field(default=None, primary_key=True)
    quote_id: int = Field(foreign_key="quote.id", index=True)
    seq: int  # 순번
    name: str
    spec: str = ""
    quantity: float = 1
    unit: str = "EA"
    unit_price: int = 0        # 판매/납품 단가
    cost_price: int = 0        # 원가 (납품용 견적에서 마진 계산용)
    amount: int = 0            # quantity * unit_price
    memo: str = ""


class Equipment(SQLModel, table=True):
    """장비 마스터 (장비 종류 단위 — 예: 'LED 무빙라이트 230W')"""
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)            # 장비명 (대분류)
    model: str = ""                          # 모델명 (예: Sharpy 230)
    manufacturer: str = ""                   # 제조사
    category: str = "조명"                    # 조명/음향/영상/구조물/전원/기타
    spec: str = ""                           # 규격/사양 (한 줄 요약)
    image: str = ""                          # 대표 이미지
    memo: str = ""                           # 비고
    # ★ 카테고리별 상세 사양 (JSON 문자열) — 카테고리에 따라 다른 필드 저장
    # 조명: power_w, weight_kg, ch_count, ch1_name, ch1_value, ..., ch4_name, ch4_value, notes
    # 음향: power_w, weight_kg, rms_power_w, peak_power_w, spl_db, freq_range, notes
    spec_data: str = "{}"
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class EquipmentUnit(SQLModel, table=True):
    """장비 개체 (같은 종류 여러 대 각각)"""
    id: Optional[int] = Field(default=None, primary_key=True)
    equipment_id: int = Field(foreign_key="equipment.id", index=True)
    asset_code: str = Field(unique=True, index=True)  # 관리코드 예: EQ-2026-0001
    serial_number: str = ""                  # 제조사 시리얼 번호
    purchase_vendor: str = ""                # 구입처
    purchase_date: Optional[date] = None     # 구입일자
    purchase_price: int = 0                  # 구입금액
    rental_price_daily: int = 0              # 1일 렌탈가
    status: str = "보유중"                    # 보유중/대여중/수리중/폐기
    location: str = ""                       # 보관 위치 (창고 A-3 등)
    department: str = ""                     # 관리부서
    manager: str = ""                        # 담당자
    last_check_date: Optional[date] = None   # 최근 점검일
    memo: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)


class MaintenanceLog(SQLModel, table=True):
    """유지보수 / AS / 수리 / 점검 이력"""
    id: Optional[int] = Field(default=None, primary_key=True)
    unit_id: int = Field(foreign_key="equipmentunit.id", index=True)
    log_date: date
    log_type: str = "점검"                    # 점검/고장/AS/수리/청소/펌웨어
    title: str                               # 짧은 제목
    description: str = ""                    # 상세 내용
    vendor: str = ""                         # AS 업체
    cost: int = 0                            # 수리비
    status_after: str = ""                   # 처리 후 상태 (정상복구/부분고장/폐기 등)
    registered_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class RentalLog(SQLModel, table=True):
    """장비 대여 이력 — 언제 어디로 나갔는지"""
    id: Optional[int] = Field(default=None, primary_key=True)
    unit_id: int = Field(foreign_key="equipmentunit.id", index=True)
    project_id: Optional[int] = Field(default=None, foreign_key="project.id")
    out_date: date                           # 반출일
    in_date: Optional[date] = None           # 반입일
    destination: str = ""                    # 대여 장소
    note: str = ""                           # 메모
    registered_by: Optional[int] = Field(default=None, foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CompanySettings(SQLModel, table=True):
    """회사 정보 (싱글톤 — id=1만 사용)"""
    id: Optional[int] = Field(default=1, primary_key=True)
    company_name: str = ""           # 상호
    representative: str = ""          # 대표자명
    biz_number: str = ""              # 사업자등록번호
    biz_type: str = ""                # 업태
    biz_item: str = ""                # 종목
    phone: str = ""                   # 대표 전화
    fax: str = ""                     # 팩스
    email: str = ""                   # 이메일
    address: str = ""                 # 주소
    bank_account: str = ""            # 입금 계좌 (은행 + 계좌번호)
    logo_image: str = ""              # 로고 파일명
    stamp_image: str = ""             # 도장 파일명
    # 견적서 기본 양식
    default_payment_terms: str = "세금계산서 발행 후 30일 이내 입금"
    default_delivery_terms: str = "발주일로부터 협의 후 결정"
    default_valid_days: int = 30      # 견적서 유효기간(일)
    quote_footer_notes: str = ""      # 견적서 하단 특기사항 기본값
    # ===== 견적서 디자인/양식 (사용자 커스터마이징) =====
    quote_theme: str = "classic"      # classic / charcoal / mustard
    quote_color_primary: str = ""     # 직접 지정 색상 (비우면 테마 기본값)
    quote_color_accent: str = ""
    quote_title_normal: str = "견  적  서"        # 납품용 견적서 제목
    quote_title_rental: str = "렌 탈 견 적 서"     # 렌탈용 견적서 제목
    quote_greeting: str = "아래와 같이 견적합니다."  # 인사문구
    quote_footer_message: str = ""                 # 견적서 하단 메시지 (감사문구 등)
    # 표시 항목 켜고 끄기
    show_spec: bool = True            # 규격 컬럼
    show_unit: bool = True            # 단위 컬럼
    show_memo: bool = False           # 비고 컬럼
    show_bank_in_quote: bool = True   # 입금계좌 표시
    show_stamp_in_sign: bool = True   # 하단 서명란 도장
    show_logo: bool = True            # 로고 표시
    # 폰트 크기 (배율)
    quote_font_scale: int = 100       # 100 = 기본, 110 = 1.1배 등
    # ===== 견적서 자유 드래그 레이아웃 (JSON으로 저장) =====
    # 각 요소의 (x, y, width, height, rotation) 위치 정보를 JSON 문자열로 저장
    # 비어있으면 기본 배치(템플릿 기본 위치) 사용
    quote_layout_json: str = ""
    # ===== 장비 관리코드 prefix (카테고리별) =====
    # JSON 형식: {"음향":"SC","조명":"LB","영상":"VC","구조물":"ST","전원":"PW","기타":"EQ"}
    # 비어있으면 기본값 사용
    equipment_prefixes_json: str = ""


# 장비 카테고리별 기본 prefix
DEFAULT_EQUIPMENT_PREFIXES = {
    "음향": "SC",      # Sound
    "조명": "LB",      # Lighting (Bar/Beam)
    "영상": "VC",      # Video Camera/Content
    "구조물": "ST",    # Structure
    "전원": "PW",      # Power
    "기타": "EQ",      # Equipment
}


# ============================================================
# 유틸
# ============================================================

def _add_column_if_missing(conn, table: str, column: str, ddl: str):
    """SQLite/PostgreSQL 공용 — 컬럼이 없으면 ALTER TABLE ADD COLUMN.
    SQLAlchemy Inspector를 써서 DB 방언에 무관하게 동작."""
    try:
        from sqlalchemy import inspect
        insp = inspect(conn)
        # 테이블이 없으면 skip (create_all로 곧 만들어짐)
        if not insp.has_table(table):
            return
        cols = [c["name"] for c in insp.get_columns(table)]
        if column not in cols:
            # PostgreSQL은 IF NOT EXISTS 지원 (안전장치)
            try:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {ddl}")
            except Exception:
                # IF NOT EXISTS 미지원 (구버전 SQLite) → 일반 ADD COLUMN
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            print(f"[MIGRATE] {table}.{column} 추가됨")
    except Exception as e:
        print(f"[WARN] migration skip: {table}.{column} - {e}")


def _safe_exec(conn, sql: str):
    """마이그레이션용 SQL을 안전하게 실행 (실패해도 다음 단계 진행)"""
    try:
        conn.exec_driver_sql(sql)
    except Exception as e:
        print(f"[WARN] sql skip: {sql[:60]}... - {e}")


def init_db():
    import os
    os.makedirs("data", exist_ok=True)
    os.makedirs(f"{DATA_DIR}/receipts", exist_ok=True)
    os.makedirs(f"{DATA_DIR}/company", exist_ok=True)
    try:
        SQLModel.metadata.create_all(engine)
    except Exception as e:
        print(f"[WARN] create_all failed: {e}")

    # 마이그레이션: 새 컬럼 추가 + 기존 데이터 환산 (각 단계 독립 실행)
    try:
        with engine.begin() as conn:
            # ── 부가세 필드 (이전 마이그레이션) ──
            _add_column_if_missing(conn, "project", "supply_amount", "supply_amount INTEGER DEFAULT 0 NOT NULL")
            _add_column_if_missing(conn, "project", "vat_amount", "vat_amount INTEGER DEFAULT 0 NOT NULL")
            _add_column_if_missing(conn, "project", "vat_mode", "vat_mode VARCHAR DEFAULT 'supply' NOT NULL")
            _safe_exec(conn, """
                UPDATE project
                SET supply_amount = revenue,
                    vat_amount = CAST(revenue * 0.1 AS INTEGER),
                    revenue = revenue + CAST(revenue * 0.1 AS INTEGER)
                WHERE supply_amount = 0 AND revenue > 0
            """)
            # ── 증빙 없는 현금거래 필드 ──
            _add_column_if_missing(conn, "expense", "has_evidence", "has_evidence BOOLEAN DEFAULT 1 NOT NULL")
            _add_column_if_missing(conn, "expense", "tax_excluded_note", "tax_excluded_note VARCHAR DEFAULT '' NOT NULL")
            _add_column_if_missing(conn, "project", "cash_no_invoice", "cash_no_invoice BOOLEAN DEFAULT 0 NOT NULL")
            _add_column_if_missing(conn, "project", "tax_excluded_note", "tax_excluded_note VARCHAR DEFAULT '' NOT NULL")
            # ── 카테고리 필드 ──
            _add_column_if_missing(conn, "project", "categories", "categories VARCHAR DEFAULT '' NOT NULL")
            # ── 면세 → 현금거래(무증빙) 리네이밍 ──
            _safe_exec(conn, "UPDATE project SET vat_mode = 'cash' WHERE vat_mode = 'exempt'")
            # ── 견적서 → 프로젝트 연결 필드 ──
            _add_column_if_missing(conn, "quote", "project_id", "project_id INTEGER")
            # ── 견적서: 행사일 + 부가세 모드 ──
            _add_column_if_missing(conn, "quote", "event_date", "event_date DATE")
            _add_column_if_missing(conn, "quote", "vat_mode", "vat_mode VARCHAR DEFAULT 'supply' NOT NULL")
            # ── 거래처 이메일 필드 (이미 모델에 있지만 기존 DB 안전 추가) ──
            _add_column_if_missing(conn, "vendor", "email", "email VARCHAR DEFAULT '' NOT NULL")
            # ── 장비 관리코드 prefix 필드 ──
            _add_column_if_missing(conn, "companysettings", "equipment_prefixes_json",
                                   "equipment_prefixes_json VARCHAR DEFAULT '' NOT NULL")
            # ── Item에 equipment_id 연결 필드 ──
            _add_column_if_missing(conn, "item", "equipment_id", "equipment_id INTEGER")
            # ── User 권한 시스템 ──
            _add_column_if_missing(conn, "user", "is_active", "is_active BOOLEAN DEFAULT 1 NOT NULL")
            _add_column_if_missing(conn, "user", "permissions_json", "permissions_json VARCHAR DEFAULT '{}' NOT NULL")
            _add_column_if_missing(conn, "user", "employee_type", "employee_type VARCHAR DEFAULT '정직원' NOT NULL")
            _add_column_if_missing(conn, "user", "worker_id", "worker_id INTEGER")
            # ── Worker 정직원/알바 구분 ──
            _add_column_if_missing(conn, "worker", "employee_type", "employee_type VARCHAR DEFAULT '알바' NOT NULL")
            _add_column_if_missing(conn, "worker", "monthly_salary", "monthly_salary INTEGER DEFAULT 0 NOT NULL")
            # ── Worker 추가 필드: 주민등록번호 / 사진 ──
            _add_column_if_missing(conn, "worker", "resident_no", "resident_no VARCHAR DEFAULT '' NOT NULL")
            _add_column_if_missing(conn, "worker", "photo_filename", "photo_filename VARCHAR DEFAULT '' NOT NULL")
            # ── Worker 인사 필드: 입사일·퇴사일·재직상태·직책 ──
            _add_column_if_missing(conn, "worker", "hire_date", "hire_date DATE")
            _add_column_if_missing(conn, "worker", "resign_date", "resign_date DATE")
            _add_column_if_missing(conn, "worker", "employment_status",
                                   "employment_status VARCHAR DEFAULT '재직' NOT NULL")
            _add_column_if_missing(conn, "worker", "position", "position VARCHAR DEFAULT '' NOT NULL")
            # ── Attendance: 오퍼비/추가금액 필드 ──
            _add_column_if_missing(conn, "attendance", "bonus_amount", "bonus_amount INTEGER DEFAULT 0 NOT NULL")
            _add_column_if_missing(conn, "attendance", "bonus_memo", "bonus_memo VARCHAR DEFAULT '' NOT NULL")
            # ── Equipment: 카테고리별 상세 사양 (JSON) ──
            _add_column_if_missing(conn, "equipment", "spec_data", "spec_data VARCHAR DEFAULT '{}' NOT NULL")
            # ── Expense: 거래처 마스터 연결 ──
            _add_column_if_missing(conn, "expense", "vendor_id", "vendor_id INTEGER")
            # ── QuoteItem: 원가 (납품 견적 마진 계산용) ──
            _add_column_if_missing(conn, "quoteitem", "cost_price", "cost_price INTEGER DEFAULT 0 NOT NULL")
            # 기존 role 'owner' → 'admin' 변환
            conn.exec_driver_sql("UPDATE user SET role = 'admin' WHERE role IN ('owner','manager')")
    except Exception as e:
        # 전체 마이그레이션이 실패해도 서버 시작은 막지 않음
        import traceback
        print(f"[WARN] migration block failed: {e}")
        traceback.print_exc()
    # 초기 관리자 계정 시드
    with Session(engine, expire_on_commit=False) as s:
        if not s.query(User).first():
            admin = User(
                username="admin",
                password_hash=hash_password("admin1234"),
                name="대표",
                role="admin",
                employee_type="관리자",
                is_active=True,
            )
            s.add(admin)
            s.commit()
        # 회사 설정 기본 레코드
        if not s.query(CompanySettings).first():
            s.add(CompanySettings(id=1, company_name="(주)회사명"))
            s.commit()


QUOTE_THEMES = {
    "classic": {
        "name": "클래식 네이비",
        "primary": "#1F3A5F",
        "accent": "#C9A961",
        "header_bg": "#1F3A5F",
        "header_fg": "#FFFFFF",
        "label_bg": "#F4F1EA",
        "total_bg": "#1F3A5F",
        "total_fg": "#C9A961",
        "discount_color": "#E74C3C",
        "discount_bg": "#FFF3E0",
    },
    "charcoal": {
        "name": "모던 차콜",
        "primary": "#0A0E1A",
        "accent": "#52525B",
        "header_bg": "#18181B",
        "header_fg": "#FFFFFF",
        "label_bg": "#F4F4F5",
        "total_bg": "#0A0E1A",
        "total_fg": "#FFFFFF",
        "discount_color": "#B91C1C",
        "discount_bg": "#FEF2F2",
    },
    "mustard": {
        "name": "머스타드 골드",
        "primary": "#78350F",
        "accent": "#B45309",
        "header_bg": "#78350F",
        "header_fg": "#FEF3C7",
        "label_bg": "#FEF3C7",
        "total_bg": "#78350F",
        "total_fg": "#FEF3C7",
        "discount_color": "#B91C1C",
        "discount_bg": "#FFF7ED",
    },
}


def get_equipment_prefixes() -> dict:
    """장비 카테고리별 prefix 매핑 반환. 사용자 설정이 없으면 기본값."""
    import json
    with Session(engine, expire_on_commit=False) as s:
        cs = s.get(CompanySettings, 1)
        if not cs or not cs.equipment_prefixes_json:
            return dict(DEFAULT_EQUIPMENT_PREFIXES)
        try:
            data = json.loads(cs.equipment_prefixes_json)
            # 기본값과 병합 (사용자가 일부만 변경한 경우 나머지는 기본값)
            merged = dict(DEFAULT_EQUIPMENT_PREFIXES)
            merged.update(data)
            return merged
        except (json.JSONDecodeError, TypeError):
            return dict(DEFAULT_EQUIPMENT_PREFIXES)


def save_equipment_prefixes(prefixes: dict) -> None:
    """장비 카테고리별 prefix 저장 (JSON)"""
    import json
    with Session(engine, expire_on_commit=False) as s:
        cs = s.get(CompanySettings, 1)
        if not cs:
            cs = CompanySettings(id=1)
        # 비어있지 않은 값만 저장 (빈 값은 기본값으로 폴백)
        clean = {k: v.strip() for k, v in prefixes.items() if v and v.strip()}
        cs.equipment_prefixes_json = json.dumps(clean, ensure_ascii=False)
        s.add(cs)
        s.commit()


def get_company_settings():
    """회사 설정 dict로 반환 (없으면 생성). 견적 테마 색상도 함께 계산."""
    with Session(engine, expire_on_commit=False) as s:
        cs = s.get(CompanySettings, 1)
        if not cs:
            cs = CompanySettings(id=1, company_name="(주)회사명")
            s.add(cs)
            s.commit()
            s.refresh(cs)

        theme_key = cs.quote_theme or "classic"
        theme = QUOTE_THEMES.get(theme_key, QUOTE_THEMES["classic"]).copy()
        # 사용자 직접 색상 오버라이드
        if cs.quote_color_primary:
            theme["primary"] = cs.quote_color_primary
            theme["header_bg"] = cs.quote_color_primary
            theme["total_bg"] = cs.quote_color_primary
        if cs.quote_color_accent:
            theme["accent"] = cs.quote_color_accent
            theme["total_fg"] = cs.quote_color_accent

        return {
            "company_name": cs.company_name, "representative": cs.representative,
            "biz_number": cs.biz_number, "biz_type": cs.biz_type, "biz_item": cs.biz_item,
            "phone": cs.phone, "fax": cs.fax, "email": cs.email, "address": cs.address,
            "bank_account": cs.bank_account,
            "logo_image": cs.logo_image, "stamp_image": cs.stamp_image,
            "default_payment_terms": cs.default_payment_terms,
            "default_delivery_terms": cs.default_delivery_terms,
            "default_valid_days": cs.default_valid_days,
            "quote_footer_notes": cs.quote_footer_notes,
            # 견적 양식 커스터마이징
            "quote_theme": theme_key,
            "quote_theme_data": theme,
            "quote_color_primary": cs.quote_color_primary,
            "quote_color_accent": cs.quote_color_accent,
            "quote_title_normal": cs.quote_title_normal or "견  적  서",
            "quote_title_rental": cs.quote_title_rental or "렌 탈 견 적 서",
            "quote_greeting": cs.quote_greeting or "아래와 같이 견적합니다.",
            "quote_footer_message": cs.quote_footer_message,
            "show_spec": cs.show_spec if cs.show_spec is not None else True,
            "show_unit": cs.show_unit if cs.show_unit is not None else True,
            "show_memo": cs.show_memo if cs.show_memo is not None else False,
            "show_bank_in_quote": cs.show_bank_in_quote if cs.show_bank_in_quote is not None else True,
            "show_stamp_in_sign": cs.show_stamp_in_sign if cs.show_stamp_in_sign is not None else True,
            "show_logo": cs.show_logo if cs.show_logo is not None else True,
            "quote_font_scale": cs.quote_font_scale or 100,
            # 드래그 레이아웃 (JSON 문자열)
            "quote_layout_json": cs.quote_layout_json or "",
        }


def hash_password(pw: str) -> str:
    """PBKDF2-SHA256 (16바이트 솔트 + 200,000 iterations)"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(pw: str, hashed: str) -> bool:
    try:
        algo, salt, expected = hashed.split("$", 2)
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000)
        return secrets.compare_digest(dk.hex(), expected)
    except Exception:
        return False


def get_session():
    with Session(engine, expire_on_commit=False) as session:
        yield session
