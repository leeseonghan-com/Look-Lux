@echo off
chcp 65001 > nul
title 정산관리 시스템 - 실행 중

REM ====================================================================
REM 정산관리 시스템 시작 스크립트
REM   - 가상환경 활성화
REM   - 데이터 폴더 확인
REM   - 서버 시작 (포트 8000)
REM   - 자동으로 브라우저 열기
REM ====================================================================

cd /d "%~dp0"

REM 가상환경 확인
if not exist ".venv\Scripts\activate.bat" (
    echo.
    echo  X 가상환경이 없습니다. 먼저 install.bat을 실행하세요.
    echo.
    pause
    exit /b 1
)

REM 데이터 폴더 자동 생성
if not exist "data" mkdir data
if not exist "data\receipts" mkdir data\receipts
if not exist "data\company" mkdir data\company

echo.
echo  ============================================================
echo    정산관리 시스템이 시작됩니다
echo  ============================================================
echo.

REM 가상환경 활성화
call .venv\Scripts\activate.bat

REM IP 주소 확인 후 출력 (사내 네트워크 다른 PC에서 접속용)
echo  접속 주소:
echo.
echo    이 PC에서:       http://localhost:8000
for /f "tokens=2 delims=:" %%i in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "IP=%%i"
    setlocal enabledelayedexpansion
    set "IP=!IP: =!"
    echo    같은 와이파이:   http://!IP!:8000
    endlocal
)
echo.
echo    모바일에서도 위 주소로 접속 가능 (같은 와이파이 필수)
echo.
echo  ============================================================
echo    창을 닫으면 서버가 종료됩니다. 종료하지 마세요!
echo  ============================================================
echo.

REM 5초 후 자동으로 브라우저 열기
start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8000"

REM 환경변수 설정 — 데이터 위치
set "DATA_DIR=%CD%\data"
set "PORT=8000"

REM uvicorn 실행 (로그 출력)
python -m uvicorn main:app --host 0.0.0.0 --port 8000

pause
