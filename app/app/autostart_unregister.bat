@echo off
chcp 65001 > nul
title 정산관리 - 자동 시작 해제

echo.
echo  정산관리 자동 시작을 해제합니다...
echo.

schtasks /Delete /TN "정산관리시스템" /F

if errorlevel 1 (
    echo  X 해제 실패 (또는 등록되지 않음). 관리자 권한이 필요할 수 있습니다.
) else (
    echo  OK - 자동 시작 해제 완료
)

echo.
pause
