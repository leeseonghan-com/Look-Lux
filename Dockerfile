FROM python:3.11-slim

# 한글 폰트 + 시간대 + OCR (Tesseract)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-nanum \
    tzdata \
    tesseract-ocr \
    tesseract-ocr-kor \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Seoul

WORKDIR /app

# 의존성
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스 (data 폴더는 volume에 마운트되므로 제외)
COPY app/ /app/

# 영구 저장될 디렉토리 (Fly volume과 매핑됨)
RUN mkdir -p /data/db /data/receipts /data/company
ENV DATA_DIR=/data

EXPOSE 8000

# main.py가 같은 디렉토리에 있으므로 직접 실행
# Railway/Render는 $PORT를 동적으로 주입, 없으면 8000 사용
# shell form (exec form 아님) — 환경변수 확장이 동작해야 함
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
