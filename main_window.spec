# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['ui\\main_window.py', 'app.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.'), ('app_icon.png', '.')],
    hiddenimports=['engines.zeroworld_shin_engine', 'engines.doomescape_engine', 'engines.zeroworld_gu_engine', 'engines.jigubyeol_engine', 'engines.keyescape_engine', 'engines.naver_engine', 'playwright.async_api', 'win32crypt'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pydantic', 'pydantic_core'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='main_window',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
