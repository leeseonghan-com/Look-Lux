"""영수증 OCR 유틸리티 — Tesseract 기반.

설치되어 있지 않거나 실패 시 None 반환 (graceful fallback).
"""
import re
from io import BytesIO
from typing import Optional


def is_ocr_available() -> bool:
    """Tesseract 사용 가능 여부 (모듈 + 바이너리 둘 다 확인)"""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def extract_receipt_data(image_bytes: bytes) -> dict:
    """영수증 이미지에서 텍스트를 추출하고 금액·날짜·상호명을 분석.

    반환: {
        "ok": bool,
        "raw_text": str,
        "amount": int (추정 총액),
        "date": str (YYYY-MM-DD 형식),
        "vendor": str (추정 상호),
        "error": str (실패 시),
    }
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return {"ok": False, "error": "OCR 라이브러리 미설치", "raw_text": "",
                "amount": 0, "date": "", "vendor": ""}

    try:
        img = Image.open(BytesIO(image_bytes))
        # 한글 + 영문 인식
        try:
            text = pytesseract.image_to_string(img, lang="kor+eng")
        except pytesseract.TesseractError:
            # 한글 데이터 없는 경우 영어만
            text = pytesseract.image_to_string(img, lang="eng")
        except Exception:
            text = pytesseract.image_to_string(img)
    except Exception as e:
        return {"ok": False, "error": str(e), "raw_text": "",
                "amount": 0, "date": "", "vendor": ""}

    if not text or not text.strip():
        return {"ok": False, "error": "텍스트를 인식할 수 없습니다.", "raw_text": "",
                "amount": 0, "date": "", "vendor": ""}

    return {
        "ok": True,
        "raw_text": text,
        "amount": _extract_amount(text),
        "date": _extract_date(text),
        "vendor": _extract_vendor(text),
        "error": "",
    }


def _extract_amount(text: str) -> int:
    """영수증 텍스트에서 가장 큰 금액(=총액) 추정. 정확도 강화 버전.

    전략 (우선순위 점수 순):
    1. [최우선 150점] "합계금액/총합계/결제금액/받을금액/청구금액/승인금액" 옆 숫자
    2. [높음 120점] "합계/총액/Total/Amount" 옆 숫자
    3. [중간 100점] 영수증 하단 1/3 구간의 "원/₩" 부착 숫자
    4. [낮음 60점] 콤마 포함 숫자 (3자리 이상)
    5. [참고 30점] 일반 큰 숫자 (전화번호·계좌·날짜·시간 제외)
    """
    # ★ 최우선 키워드 — 영수증의 "최종 결제액"을 명확히 가리킴
    top_keywords = [
        r"합\s*계\s*금\s*액", r"총\s*합\s*계", r"결\s*제\s*금\s*액",
        r"받\s*을\s*금\s*액", r"청\s*구\s*금\s*액", r"승\s*인\s*금\s*액",
        r"GRAND\s*TOTAL", r"TOTAL\s*AMOUNT", r"받\s*은\s*금\s*액",
        r"결\s*제\s*총\s*액",
    ]
    # 높음 키워드
    high_keywords = [
        r"합\s*계", r"총\s*액", r"총\s*금\s*액", r"판\s*매\s*금\s*액",
        r"\bTOTAL\b", r"\bAMOUNT\b", r"\bSUBTOTAL\b", r"\bDUE\b", r"\bSUM\b",
    ]
    # 제외 키워드 (이 키워드 옆 숫자는 무시 — 부가세, 공급가, 세금 등은 총액 아님)
    skip_keywords = [
        r"부\s*가\s*세", r"공\s*급\s*가", r"V\s*A\s*T", r"세\s*액",
        r"\bTAX\b", r"\bNET\b", r"과\s*세", r"비\s*과\s*세",
        r"면\s*세", r"잔\s*돈", r"거\s*스\s*름",
    ]

    candidates = []  # (score, num)

    lines = text.split("\n")
    total_lines = max(len(lines), 1)

    # 같은 라인 또는 다음 라인에 숫자가 있을 수 있음 (멀티라인 영수증 대응)
    for i, line in enumerate(lines):
        # 제외 키워드 매칭 — 이 라인은 후보에서 제외
        is_skip_line = any(re.search(kw, line, re.IGNORECASE) for kw in skip_keywords)
        if is_skip_line:
            continue

        line_score_bonus = int((i / total_lines) * 30)  # 하단일수록 +30까지

        # 최우선 키워드
        for kw in top_keywords:
            for m in re.finditer(kw + r"[\s\:\-원W₩\(\)]*([\d,]{2,})", line, re.IGNORECASE):
                num = _parse_number(m.group(1))
                if 100 <= num <= 1_000_000_000:
                    candidates.append((150 + line_score_bonus, num))
            # 같은 라인의 라스트 숫자 (오른쪽 정렬된 금액)
            if re.search(kw, line, re.IGNORECASE):
                nums_in_line = re.findall(r"([\d]{1,3}(?:,[\d]{3})+|[\d]{4,10})", line)
                for n in nums_in_line:
                    num = _parse_number(n)
                    if 100 <= num <= 1_000_000_000:
                        candidates.append((140 + line_score_bonus, num))

        # 높음 키워드
        for kw in high_keywords:
            for m in re.finditer(kw + r"[\s\:\-원W₩\(\)]*([\d,]{2,})", line, re.IGNORECASE):
                num = _parse_number(m.group(1))
                if 100 <= num <= 1_000_000_000:
                    candidates.append((120 + line_score_bonus, num))

    # 영수증 하단 1/3 구간의 큰 콤마 숫자 (대부분 총액)
    bottom_third_start = int(total_lines * 0.66)
    for i in range(bottom_third_start, total_lines):
        line = lines[i]
        if any(re.search(kw, line, re.IGNORECASE) for kw in skip_keywords):
            continue
        for m in re.finditer(r"([\d]{1,3}(?:,[\d]{3})+)\s*[원W₩]?", line):
            num = _parse_number(m.group(1))
            if 1000 <= num <= 1_000_000_000:
                candidates.append((100, num))

    # "원" 직전 큰 숫자
    for m in re.finditer(r"([\d]{1,3}(?:,[\d]{3})+|[\d]{4,9})\s*원", text):
        num = _parse_number(m.group(1))
        if 1000 <= num <= 1_000_000_000:
            candidates.append((80, num))

    # 일반 콤마 숫자
    for m in re.finditer(r"([\d]{1,3}(?:,[\d]{3})+)", text):
        num = _parse_number(m.group(1))
        if 1000 <= num <= 1_000_000_000:
            # 컨텍스트 확인 — 전화번호/계좌 형식 제외
            ctx_start = max(0, m.start() - 10)
            ctx_end = min(len(text), m.end() + 10)
            ctx = text[ctx_start:ctx_end]
            if re.search(r"\d{3}-\d{4}-\d{4}|\d{2,3}-\d{6,8}", ctx):  # 전화번호
                continue
            if re.search(r"카\s*드\s*번\s*호|계\s*좌\s*번\s*호", ctx):
                continue
            candidates.append((60, num))

    if not candidates:
        return 0

    # 점수 가장 높은 후보들 중 가장 큰 금액 채택 (총액이 부분합보다 큼)
    candidates.sort(reverse=True)
    max_score = candidates[0][0]
    top = [num for sc, num in candidates if sc >= max_score - 10]  # ±10점 이내 동급
    return max(top) if top else 0


def _extract_date(text: str) -> str:
    """영수증 텍스트에서 날짜 추출 (YYYY-MM-DD 반환).

    다양한 한국 영수증 날짜 포맷 지원:
    - 2026-01-15, 2026.01.15, 2026/01/15
    - 2026년 01월 15일
    - 26-01-15 (YY 형식)
    - 26/01/15, 26.01.15
    """
    patterns = [
        # YYYY-MM-DD 계열 (가장 일반적)
        r"(20\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})",
        # YYYY MM DD 공백 구분
        r"(20\d{2})\s+(\d{1,2})\s+(\d{1,2})",
        # YY-MM-DD 계열
        r"(\d{2})\s*[-./]\s*(\d{1,2})\s*[-./]\s*(\d{1,2})",
    ]
    today_year = 2026  # 현재 연도 기준 가까운 연도 우선
    candidates = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            y, mo, d = m.group(1), m.group(2), m.group(3)
            if len(y) == 2:
                y_i = int(y)
                # 50 이하면 20xx, 그 이상이면 19xx (드물게)
                y = ("20" if y_i <= 50 else "19") + y
            try:
                y_i = int(y); m_i = int(mo); d_i = int(d)
                if 2020 <= y_i <= 2099 and 1 <= m_i <= 12 and 1 <= d_i <= 31:
                    # 현재 연도와 가까울수록 우선
                    score = 100 - abs(y_i - today_year)
                    candidates.append((score, f"{y_i:04d}-{m_i:02d}-{d_i:02d}"))
            except ValueError:
                continue
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return ""


def _extract_vendor(text: str) -> str:
    """영수증 상단에서 상호 추정 - 정확도 강화

    전략:
    1. "상호:" / "상호명:" / "업체명:" / "매장명:" 키워드 옆 텍스트 → 가장 확실
    2. 영수증 첫 5줄 중 한글이 많은 줄 (브랜드명 보통 최상단)
    3. 양식 단어 제외 (영수증·세금계산서·신용카드 등)
    """
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not lines:
        return ""

    # 1차: 명시적 키워드 — 가장 정확
    for line in lines[:15]:
        m = re.search(r"(?:상\s*호\s*명?|업\s*체\s*명|매\s*장\s*명|점\s*포\s*명)\s*[\:\-]\s*([^\n\:]+)", line)
        if m:
            name = m.group(1).strip()
            name = re.sub(r"[\d\-]{8,}", "", name).strip()
            if 2 <= len(name) <= 30:
                return name

    # 제외할 양식 단어
    skip_words = ["영수증", "세금계산서", "신용카드", "현금영수증", "거래내역",
                  "매출전표", "영업소", "사업자", "TEL", "FAX", "Tel", "Fax",
                  "주소", "Address", "결제", "주문", "RECEIPT", "ORDER",
                  "감사합니다", "방문해주셔서"]

    # 2차: 첫 5줄 중 한글이 많고 양식 단어가 없는 줄
    candidates = []
    for i, ln in enumerate(lines[:5]):
        if any(sw in ln for sw in skip_words):
            continue
        if not (2 <= len(ln) <= 30):
            continue
        digit_count = sum(1 for c in ln if c.isdigit())
        if digit_count > len(ln) * 0.4:  # 숫자 비율이 높으면 사업자번호 등
            continue
        korean_count = sum(1 for c in ln if '\uac00' <= c <= '\ud7a3')
        if korean_count >= 2:
            # 상위 줄일수록 가산점
            score = korean_count + (5 - i) * 2
            candidates.append((score, ln))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return lines[0][:20] if lines else ""


def _parse_number(s: str) -> int:
    """콤마/공백 제거 후 정수 변환"""
    try:
        return int(s.replace(",", "").replace(" ", "").strip())
    except (ValueError, AttributeError):
        return 0
