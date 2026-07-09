@echo off
chcp 65001 > nul
title 정산관리 시스템 - 종료

echo.
echo  정산관리 시스템을 종료합니다...
echo.

REM 포트 8000을 사용하는 프로세스 종료
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    taskkill /F /PID %%a > nul 2>&1
)

REM uvicorn / python 관련 프로세스 종료
taskkill /F /IM uvicorn.exe > nul 2>&1
wmic process where "commandline like '%%uvicorn%%main:app%%'" delete > nul 2>&1

echo  OK - 정산관리 시스템이 종료되었습니다.
echo.
timeout /t 2 > nul
exit
