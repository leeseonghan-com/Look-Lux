@echo off
chcp 65001 > nul
title 정산관리 - Windows 시작 시 자동 실행 등록

REM ====================================================================
REM 정산관리 시스템을 Windows 시작 시 자동 실행되도록 등록
REM (작업 스케줄러 사용 — 관리자 권한 필요)
REM ====================================================================

echo.
echo  ============================================================
echo    Windows 자동 시작 등록
echo  ============================================================
echo.
echo  이 PC를 켜면 정산관리 시스템이 자동으로 실행되도록 설정합니다.
echo.
echo  요구사항: 관리자 권한 필요
echo  로그인 후 자동 실행 / 로그인 없이도 자동 실행 옵션 제공
echo.
pause

cd /d "%~dp0"
set "STARTSCRIPT=%CD%\start.bat"

echo.
echo 작업 스케줄러에 등록 중...

schtasks /Create /TN "정산관리시스템" /SC ONLOGON /TR "\"%STARTSCRIPT%\"" /RL HIGHEST /F

if errorlevel 1 (
    echo.
    echo  X 등록 실패. 이 파일을 [관리자 권한으로 실행]해주세요.
    echo    파일 우클릭 → "관리자 권한으로 실행"
    pause
    exit /b 1
)

echo.
echo  OK - 자동 시작 등록 완료
echo.
echo  이제 Windows에 로그인할 때마다 정산관리가 자동으로 시작됩니다.
echo.
echo  자동 시작 해제를 원하면 autostart_unregister.bat을 실행하세요.
echo.
pause
