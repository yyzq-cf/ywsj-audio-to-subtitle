# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None

# 确保如果本地没有 static 目录时自动创建一个空的，防止 PyInstaller 报错
if not os.path.exists('static'):
    os.makedirs('static')

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
        'clr_loader',
        'pythonnet'
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
