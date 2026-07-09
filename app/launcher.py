"""
정산관리 시스템 - 데스크톱 앱 런처
- 백그라운드에서 uvicorn 서버 시작
- 시스템 기본 브라우저에서 자동 오픈
- 시스템 트레이/메뉴바 (선택적)
"""
import os
import sys
import threading
import time
import webbrowser
import socket
from pathlib import Path

# PyInstaller로 패키징되었을 때 작업 디렉토리
if getattr(sys, 'frozen', False):
    # PyInstaller 환경
    BASE_DIR = Path(sys._MEIPASS)
    # 데이터는 사용자 홈에 저장 (앱 번들 안에 쓰지 않도록)
    if sys.platform == 'darwin':
        DATA_DIR = Path.home() / "Library" / "Application Support" / "정산관리"
    elif sys.platform == 'win32':
        DATA_DIR = Path(os.environ.get('APPDATA', Path.home())) / "정산관리"
    else:
        DATA_DIR = Path.home() / ".정산관리"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 작업 디렉토리를 데이터 디렉토리로 변경 (data/ 폴더가 거기에 생성됨)
    os.chdir(DATA_DIR)
    # 앱 내부 리소스 경로
    APP_DIR = BASE_DIR / "app"
    sys.path.insert(0, str(APP_DIR))
else:
    # 개발 환경 (정상 Python 실행)
    APP_DIR = Path(__file__).resolve().parent
    DATA_DIR = APP_DIR.parent
    os.chdir(DATA_DIR)
    sys.path.insert(0, str(APP_DIR))


def find_free_port(default=8765, max_tries=20):
    """사용 가능한 포트 탐색 (8765 우선, 다른 인스턴스 충돌 회피)"""
    for offset in range(max_tries):
        port = default + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return default


def wait_for_server(url, timeout=15):
    """서버가 준비될 때까지 대기"""
    import urllib.request
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def run_server(port):
    """uvicorn 서버 실행 (메인 스레드 차단)"""
    import uvicorn
    # main 모듈 import 후 app 실행
    from main import app
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)


def main():
    port = find_free_port()
    url = f"http://127.0.0.1:{port}/"

    # 서버를 백그라운드 데몬 스레드로 시작
    server_thread = threading.Thread(target=run_server, args=(port,), daemon=True)
    server_thread.start()

    # 서버가 응답할 때까지 대기 후 브라우저 오픈
    if wait_for_server(url + "login"):
        print(f"✓ 정산관리 시스템이 시작되었습니다")
        print(f"  주소: {url}")
        print(f"  데이터 저장 위치: {DATA_DIR}")
        print(f"")
        print(f"  종료하려면 이 창을 닫거나 Ctrl+C를 누르세요")
        webbrowser.open(url)
    else:
        print("⚠️ 서버 시작 실패")
        sys.exit(1)

    # 메인 스레드 유지 (서버가 데몬이라 메인이 끝나면 종료됨)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n종료 중...")
        sys.exit(0)


if __name__ == "__main__":
    main()
