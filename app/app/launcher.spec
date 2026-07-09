# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec - 정산관리 시스템
빌드 방법:
  macOS:   pyinstaller launcher.spec
  Windows: pyinstaller launcher.spec
"""
import sys
from pathlib import Path

block_cipher = None

APP_NAME = "정산관리"
APP_VERSION = "1.0.0"

# 패키징할 파일/폴더
datas = [
    ('templates', 'app/templates'),
    ('static', 'app/static'),
    ('main.py', 'app'),
    ('database.py', 'app'),
    ('template_utils.py', 'app'),
    ('routes_attendance.py', 'app'),
    ('routes_expense.py', 'app'),
    ('routes_project.py', 'app'),
    ('routes_item.py', 'app'),
    ('routes_quote.py', 'app'),
    ('routes_worker.py', 'app'),
    ('routes_vendor.py', 'app'),
    ('routes_settings.py', 'app'),
    ('routes_equipment.py', 'app'),
    ('routes_mobile.py', 'app'),
]

# 명시적으로 포함할 모듈 (PyInstaller가 못 찾을 수 있는 것들)
hiddenimports = [
    'uvicorn',
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'fastapi',
    'starlette',
    'starlette.middleware.sessions',
    'sqlmodel',
    'sqlalchemy',
    'sqlalchemy.dialects.sqlite',
    'jinja2',
    'multipart',
    'python_multipart',
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageFont',
]

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 실행 파일 생성
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI 모드 (콘솔 창 안 뜸)
    disable_windowed_traceback=False,
    argv_emulation=True if sys.platform == 'darwin' else False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.icns' if sys.platform == 'darwin' else ('icon.ico' if sys.platform == 'win32' else None),
)

# 폴더 구조로 패키징 (가벼움)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

# macOS .app 번들 생성
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name=f'{APP_NAME}.app',
        icon='icon.icns',
        bundle_identifier='com.jeongsan.management',
        version=APP_VERSION,
        info_plist={
            'CFBundleName': APP_NAME,
            'CFBundleDisplayName': APP_NAME,
            'CFBundleShortVersionString': APP_VERSION,
            'CFBundleVersion': APP_VERSION,
            'NSHighResolutionCapable': True,
            'LSApplicationCategoryType': 'public.app-category.business',
            'NSHumanReadableCopyright': 'Copyright © 2026',
        },
    )
