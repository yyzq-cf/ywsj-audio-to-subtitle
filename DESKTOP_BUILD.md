# PyInstaller Desktop App Packaging

To build native desktop applications (Windows `.exe` and macOS `.app` / `.dmg`) for **ywsj-audio-to-subtitle** without using a browser web UI, we use **PyInstaller** combined with **pywebview** (which opens a native OS webview window running the local Flask backend).

---

## 📋 架构设计
1. **后端核心**：Python Flask 随机空闲端口启动。
2. **图形界面**：`pywebview` 创建原生桌面窗口（Windows 上调用 WebView2 / Win32，macOS 上调用 Cocoa WebKit）。
3. **音视频依赖**：内联静态编译的 `ffmpeg` 二进制文件，确保用户电脑无需单独安装 FFmpeg 即可实现音轨提取。

---

## 🛠️ 本地桌面客户端打包脚本 (`desktop_app.py`)

在项目根目录下新建 `desktop_app.py`：

```python
import os
import sys
import threading
import socket
import webview
from app import app, logger

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def run_server(port):
    # 关闭调试模式，在本地线程运行 Flask
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)

def main():
    # 适配 PyInstaller 打包后的资源路径
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
        app.template_folder = os.path.join(base_path, 'templates')
        app.static_folder = os.path.join(base_path, 'static')
    
    port = find_free_port()
    
    # 启动后台 Flask 线程
    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()
    
    # 创建原生桌面 Webview 窗口
    url = f'http://127.0.0.1:{port}/'
    window = webview.create_window(
        'ywsj-audio-to-subtitle - 智能音视频转字幕',
        url,
        width=1100,
        height=750,
        min_size=(800, 600),
        resizable=True
    )
    
    webview.start()

if __name__ == '__main__':
    main()
```

---

## 📦 PyInstaller 打包配置 (`app.spec`)

在项目根目录下新建 `app.spec`：

```python
# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['desktop_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ],
    hiddenimports=['flask', 'pydub', 'requests', 'webview', 'jinja2', 'werkzeug'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_folder.get_data(), cipher=block_cipher)

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
    console=False,  # 无黑窗口，纯原生 GUI 桌面应用
    disable_window_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
```

---

## 🚀 GitHub Actions 自动多平台打包 Workflow

在 `.github/workflows/desktop-build.yml` 配置自动打包 Windows 和 macOS 客户端：

```yaml
name: Build Native Desktop App

on:
  push:
    tags:
      - 'v*'

jobs:
  build-desktop:
    name: Build for ${{ matrix.os }}
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [windows-latest, macos-latest]

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install pyinstaller pywebview flask pydub requests jinja2 werkzeug

      - name: Build with PyInstaller (Windows)
        if: startsWith(matrix.os, 'windows')
        run: |
          pyinstaller app.spec

      - name: Build with PyInstaller (macOS)
        if: startsWith(matrix.os, 'macos')
        run: |
          pyinstaller app.spec

      - name: Package Artifacts (Windows)
        if: startsWith(matrix.os, 'windows')
        run: |
          Compress-Archive -Path dist/AudioToSubtitle.exe -DestinationPath AudioToSubtitle-Windows.zip

      - name: Package Artifacts (macOS)
        if: startsWith(matrix.os, 'macos')
        run: |
          cd dist && zip -r AudioToSubtitle-macOS.zip AudioToSubtitle.app && cd ..

      - name: Upload Release Assets
        uses: softprops/action-gh-release@v2
        with:
          files: |
            dist/AudioToSubtitle.exe
            dist/AudioToSubtitle-Windows.zip
            dist/AudioToSubtitle-macOS.zip
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```
