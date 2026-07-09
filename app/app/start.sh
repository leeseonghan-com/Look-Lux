#!/bin/bash
echo "========================================"
echo " 정산관리 시스템 시작"
echo "========================================"
cd "$(dirname "$0")"
pip3 install -r requirements.txt --quiet
echo ""
echo "브라우저에서 http://localhost:8000 접속"
echo ""
python3 main.py
