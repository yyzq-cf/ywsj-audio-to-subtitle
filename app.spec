# -*- mode: python ; coding: utf-8 -*-

import sys
import os

block_cipher = None

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=[
        'flask', 
        'pydub', 
        'requests', 
        'webview', 
        'jinja2', 
        'werkzeug', 
        'engineio.async_drivers.threading',
        'click',
        'itsdangerous',
        'blinker'
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_folder.get_data() if hasattr(a, 'zipped_folder') else a.pure, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='AudioToSubtitle',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_window_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

if sys.platform == 'darwin':
    app = BUNDLE(
        exe,
        name='AudioToSubtitle.app',
        icon=None,
        bundle_identifier='com.ywsj.audiotosubtitle',
    )
