# 정산관리 시스템 — 사용 안내서

엑셀로 관리하던 **프로젝트 정산 · 견적 · 알바 근태**를 웹 프로그램으로 옮긴 시스템입니다.
PC와 스마트폰 모두에서 사용 가능합니다.

---

## 🎯 주요 기능

| 기능 | 설명 |
|---|---|
| 📋 프로젝트 관리 | 행사별 매출/비용/순이익 자동 계산 |
| 💰 비용처리 | **📷 영수증 사진 첨부** (현장에서 폰으로) |
| 👥 알바 근태 | **출근/퇴근 버튼 한 번**으로 시간·금액 자동 |
| 📦 단가표 | 수입가/대리점가/소비자가 관리 (내부용) |
| 📝 견적서 | 단가 자동완성 + **할인금액** + PDF 출력 |
| 🏠 대시보드 | 매출/비용/순이익/미수금 한눈에 |

---

## 🚀 설치 및 실행

### 1단계: Python 설치 확인

터미널(또는 명령 프롬프트)에서:

```bash
python3 --version
# 또는
python --version
```

→ `Python 3.10` 이상이 나오면 OK. 없으면 [python.org](https://www.python.org/downloads/)에서 설치.

### 2단계: 라이브러리 설치

`app` 폴더에서:

```bash
pip install -r requirements.txt
```

### 3단계: 실행

```bash
cd app
python main.py
```

또는

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 4단계: 접속

**같은 PC에서**: 브라우저로 http://localhost:8000

**같은 와이파이의 스마트폰에서**: PC의 IP 주소로 접속
- PC IP 확인: `ipconfig` (Windows) / `ifconfig` (Mac/Linux)
- 예: `http://192.168.0.10:8000`

### 초기 계정

| 아이디 | 비밀번호 |
|---|---|
| `admin` | `admin1234` |

---

## 📱 스마트폰 현장 사용법

**알바 출근/퇴근 기록**:
1. 스마트폰 브라우저로 접속
2. 하단 `👥 근태` 탭 클릭
3. 프로젝트·근무자 선택 → **출근** 버튼
4. 퇴근 시 해당 근무자 카드의 **퇴근** 버튼
   - 시간 · 금액 자동 계산

**영수증 사진 첨부**:
1. 하단 `💰 비용` 탭 → `+ 새 비용 등록`
2. 항목·금액 입력
3. **📷 영수증 사진** 클릭 → 카메라 촬영
4. 저장

---

## 💾 데이터 백업

데이터는 `app/data/app.db` 파일 한 개에 모두 저장됩니다.

**백업하려면**: `app/data/` 폴더 전체를 USB나 외장하드에 복사하세요.
- `app.db` — 데이터베이스
- `receipts/` — 영수증 사진들

복원 시: 같은 위치에 덮어쓰면 됩니다.

---

## 🌐 팀원 공유 방법

### 방법 A: 사장님 PC를 서버로 (무료, 사내 네트워크)

1. 사장님 PC에서 `python main.py` 실행 후 종료하지 않음
2. 팀원은 같은 와이파이에서 사장님 PC의 IP로 접속
   - 예: `http://192.168.0.10:8000`
3. 단점: 사장님 PC가 꺼지면 접속 불가

### 방법 B: 클라우드 호스팅 (월 5~10$, 24시간 접속)

[Railway.app](https://railway.app), [Fly.io](https://fly.io), [Render.com](https://render.com) 등 무료 티어로 시작 가능.
필요하시면 배포 가이드 별도로 안내드릴 수 있습니다.

---

## 📁 폴더 구조

```
app/
├── main.py                  메인 앱
├── database.py              DB 모델 (테이블 정의)
├── routes_*.py              각 기능별 라우터
├── requirements.txt         의존성 목록
├── templates/               HTML 화면
│   ├── base.html            공통 레이아웃 (네비)
│   ├── home.html            대시보드
│   ├── attendance.html      알바 근태
│   ├── expenses.html        비용 목록
│   ├── expense_new.html     비용 등록 (사진 첨부)
│   ├── projects.html        프로젝트 목록
│   ├── project_detail.html  프로젝트 상세
│   ├── items.html           단가표
│   ├── quotes.html          견적서 목록
│   ├── quote_new.html       견적서 작성
│   ├── quote_print.html     인쇄용 견적서
│   ├── workers.html         근무자 관리
│   └── vendors.html         거래처 관리
├── static/                  정적 파일
└── data/                    데이터 저장 (자동 생성)
    ├── app.db               SQLite DB
    └── receipts/            영수증 사진
```

---

## 🛠 문제 해결

**Q. 비밀번호를 잊었어요**
A. `app/data/app.db` 파일을 삭제하고 다시 실행하면 초기 계정으로 복구됩니다. (단, 모든 데이터 초기화됨!)

**Q. 폰에서 접속이 안 돼요**
A. PC와 폰이 같은 와이파이여야 합니다. 또한 Windows 방화벽에서 Python을 허용해야 할 수 있습니다.

**Q. 추가 기능이 필요해요**
A. 부가세 신고용 거래처별 매출 집계, PDF 직접 출력, 이메일 발송 등 추가 가능합니다.

---

## ✏️ 비밀번호 변경 (관리자)

코드로만 가능합니다 (추후 UI 추가 예정):

```python
from database import engine, User, hash_password
from sqlmodel import Session, select

with Session(engine) as s:
    user = s.exec(select(User).where(User.username == "admin")).first()
    user.password_hash = hash_password("새비밀번호")
    s.add(user)
    s.commit()
```
