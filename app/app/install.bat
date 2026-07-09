@echo off
chcp 65001 > nul
title 정산관리 시스템 - 자동 설치

REM ====================================================================
REM 정산관리 시스템 - Windows 자동 설치 스크립트
REM ====================================================================
REM 이 스크립트는 다음을 자동으로 수행합니다:
REM   1. Python 설치 확인 (없으면 안내)
REM   2. 가상환경 생성
REM   3. 필요한 라이브러리 자동 설치
REM   4. 데이터 폴더 자동 생성
REM   5. 시작 바로가기 생성
REM ====================================================================

echo.
echo  ============================================================
echo    정산관리 시스템 - Windows 자동 설치
echo  ============================================================
echo.
echo  이 프로그램은 다음 작업을 자동으로 수행합니다:
echo    1. Python 환경 확인 ^& 설정
echo    2. 필요한 라이브러리 자동 설치 (FastAPI, openpyxl 등)
echo    3. 데이터 폴더 생성
echo    4. 시작/종료 바로가기 생성
echo.
echo  설치 시간: 약 2-3분 소요
echo.
pause

REM === Python 설치 확인 ===
echo.
echo [1/5] Python 설치 확인 중...
where python > nul 2>&1
if errorlevel 1 (
    echo.
    echo  X Python이 설치되어 있지 않습니다.
    echo.
    echo    아래 사이트에서 Python 3.11 이상을 먼저 설치해주세요:
    echo    https://www.python.org/downloads/
    echo.
    echo    설치 시 "Add Python to PATH" 체크박스를 반드시 선택하세요!
    echo.
    echo    설치 후 이 install.bat을 다시 실행하면 됩니다.
    echo.
    pause
    exit /b 1
)
python --version
echo   OK - Python 정상

REM === 가상환경 생성 ===
echo.
echo [2/5] 가상환경 생성 중... (1-2분 소요)
if not exist ".venv" (
    python -m venv .venv
    if errorlevel 1 (
        echo  X 가상환경 생성 실패. python -m pip install --upgrade pip 후 재시도하세요.
        pause
        exit /b 1
    )
)
echo   OK - 가상환경 준비 완료

REM === pip 업그레이드 ===
echo.
echo [3/5] pip 최신 버전 업데이트 중...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
echo   OK

REM === 라이브러리 설치 ===
echo.
echo [4/5] 필수 라이브러리 설치 중... (2-3분 소요)
echo    - FastAPI, uvicorn, SQLModel
echo    - openpyxl (엑셀 백업)
echo    - Pillow (이미지 처리)
echo.
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo  X 라이브러리 설치 실패
    pause
    exit /b 1
)
echo   OK - 모든 라이브러리 설치 완료

REM === 데이터 폴더 생성 ===
echo.
echo [5/5] 데이터 폴더 생성 중...
if not exist "data" mkdir data
if not exist "data\receipts" mkdir data\receipts
if not exist "data\company" mkdir data\company
echo   OK - 데이터 폴더 준비 완료

REM === 바탕화면 바로가기 생성 ===
echo.
echo 바로가기 생성 중...
set "CURDIR=%CD%"
set "DESKTOP=%USERPROFILE%\Desktop"
powershell -Command "$WS = New-Object -ComObject WScript.Shell; $S = $WS.CreateShortcut('%DESKTOP%\정산관리 시작.lnk'); $S.TargetPath = '%CURDIR%\start.bat'; $S.WorkingDirectory = '%CURDIR%'; $S.IconLocation = '%CURDIR%\icon.ico'; $S.Save()" > nul 2>&1
powershell -Command "$WS = New-Object -ComObject WScript.Shell; $S = $WS.CreateShortcut('%DESKTOP%\정산관리 종료.lnk'); $S.TargetPath = '%CURDIR%\stop.bat'; $S.WorkingDirectory = '%CURDIR%'; $S.Save()" > nul 2>&1
echo   OK - 바탕화면에 [정산관리 시작] 바로가기 생성됨

echo.
echo  ============================================================
echo    설치 완료!
echo  ============================================================
echo.
echo    다음 단계:
echo.
echo    1. 바탕화면의 [정산관리 시작] 아이콘을 더블클릭
echo    2. 브라우저에서 http://localhost:8000 접속
echo    3. 초기 로그인: admin / admin1234
echo    4. 첫 로그인 후 비밀번호 변경 필수!
echo.
echo    외부 접속을 원하시면 [외부접속_설정가이드.md]를 참고하세요.
echo.
pause
