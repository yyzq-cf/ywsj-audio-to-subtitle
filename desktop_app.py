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
    app.run(host='127.0.0.1', port=port, debug=False, use_reloader=False)

def main():
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
        app.template_folder = os.path.join(base_path, 'templates')
        app.static_folder = os.path.join(base_path, 'static')
    
    port = find_free_port()
    
    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()
    
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
