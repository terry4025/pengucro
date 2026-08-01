# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.'), ('app_icon.png', '.')],
    hiddenimports=[
        # Engines are selected by name at runtime in engines/registry.py, so
        # static analysis cannot see them.
        'engines.zeroworld_shin_engine',
        'engines.doomescape_engine',
        'engines.zeroworld_gu_engine',
        'engines.jigubyeol_engine',
        'engines.keyescape_engine',
        'engines.naver_engine',
        'playwright.async_api',
        'win32crypt',
        # Modules added in the 5.32/5.4 reworks. They are imported normally, so
        # PyInstaller finds them on its own; listed here so a future import
        # being made lazy cannot silently drop them from the bundle.
        'ui.loading_overlay',
        'ui.repaint',
        'ui.scrollable',
        'pengucro.logging_setup',
        'engines.browser_session',
        'engines.server_clock',
        'engines.naver_api',
    ],
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
    name='방탈출펭크로v5.43',
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
