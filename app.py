import os
import io
import json
import uuid
import secrets
import threading
import logging
import time
import sqlite3
from functools import wraps
from urllib.parse import quote
from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

from asr_engine import ENGINES, recognize_audio, ASRSegment, build_srt, build_vtt, build_txt, ms_to_srt_time

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24h

APP_VERSION = os.environ.get('APP_VERSION', 'dev')
DATA_DIR = os.environ.get('DATA_DIR', '/data')
os.makedirs(DATA_DIR, exist_ok=True)

# ===== Persisted secret key =====
def _load_secret_key():
    key_file = os.path.join(DATA_DIR, '.flask_secret_key')
    try:
        with open(key_file, 'r') as f:
            return f.read().strip()
    except (OSError, IOError):
        key = secrets.token_hex(32)
        with open(key_file, 'w') as f:
            f.write(key)
        os.chmod(key_file, 0o600)
        return key

app.secret_key = _load_secret_key()

# ===== Brute force protection =====
login_attempts = {}

def check_login_allowed(ip):
    allowed = True
    wait = 0
    entry = login_attempts.get(ip)
    if entry and entry.get('locked_until', 0) > time.time():
        allowed = False
        wait = int(entry['locked_until'] - time.time())
    return allowed, wait

def record_failed_login(ip):
    entry = login_attempts.get(ip, {'count': 0, 'locked_until': 0})
    entry['count'] += 1
    if entry['count'] >= 5:
        entry['locked_until'] = time.time() + 900
        entry['count'] = 0
    login_attempts[ip] = entry

def clear_failed_login(ip):
    login_attempts.pop(ip, None)

# ===== SQLite database =====
DB_FILE = os.path.join(DATA_DIR, "app.db")

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS auth (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            username TEXT NOT NULL,
            password TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history (
            id TEXT PRIMARY KEY,
            filename TEXT,
            engine TEXT,
            segment_count INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
    """)
    conn.commit()
    conn.close()

init_db()

# ===== Auth =====
def load_auth():
    conn = get_db()
    row = conn.execute("SELECT username, password FROM auth WHERE id = 1").fetchone()
    conn.close()
    if row:
        return {'username': row['username'], 'password': row['password']}
    return None

def save_auth(username, password):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO auth (id, username, password) VALUES (1, ?, ?)",
        (username, generate_password_hash(password))
    )
    conn.commit()
    conn.close()

def is_auth_enabled():
    return load_auth() is not None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_auth_enabled():
            if request.path.startswith('/api/'):
                return jsonify({'error': '未初始化', 'setup_required': True}), 401
            return redirect(url_for('setup'))
        if not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': '未登录', 'auth_required': True}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ===== Routes =====

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if is_auth_enabled():
        return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if not username or not password:
            error = '用户名和密码不能为空'
        elif len(password) < 4:
            error = '密码至少4位'
        elif password != confirm:
            error = '两次输入的密码不一致'
        else:
            save_auth(username, password)
            session.clear()
            session['logged_in'] = True
            session.permanent = True
            return redirect(url_for('index'))
    return render_template('setup.html', error=error, version=APP_VERSION)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_auth_enabled():
        return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        ip = request.remote_addr or '0.0.0.0'
        allowed, wait = check_login_allowed(ip)
        if not allowed:
            error = f'登录尝试过多，已锁定，请 {wait} 秒后重试'
        else:
            username = request.form.get('username', '')
            password = request.form.get('password', '')
            auth = load_auth()
            if auth and username == auth['username'] and check_password_hash(auth['password'], password):
                clear_failed_login(ip)
                session.clear()
                session['logged_in'] = True
                session.permanent = True
                return redirect(url_for('index'))
            record_failed_login(ip)
            remaining = 5 - login_attempts.get(ip, {}).get('count', 0)
            if remaining > 0:
                error = f'用户名或密码错误，剩余尝试次数 {remaining} 次'
            else:
                error = '登录失败次数过多，已锁定 15 分钟'
    return render_template('login.html', error=error, version=APP_VERSION)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/api/auth/status')
def auth_status():
    return jsonify({
        'auth_enabled': is_auth_enabled(),
        'logged_in': bool(session.get('logged_in')),
        'setup_required': not is_auth_enabled(),
    })

@app.route('/api/auth/change-password', methods=['POST'])
@login_required
def change_password():
    data = request.get_json()
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')
    if not new_password or len(new_password) < 4:
        return jsonify({'error': '新密码至少4位'}), 400
    auth = load_auth()
    if not auth:
        return jsonify({'error': '认证未启用'}), 400
    if not check_password_hash(auth['password'], old_password):
        return jsonify({'error': '旧密码错误'}), 403
    save_auth(auth['username'], new_password)
    return jsonify({'ok': True})

# ===== In-memory task store =====
tasks = {}

@app.route('/')
@login_required
def index():
    return render_template('index.html', engines=ENGINES, version=APP_VERSION)

@app.route('/api/engines')
@login_required
def engines():
    return jsonify({k: {'label': v['label'], 'needs_key': v.get('needs_key', False)} for k, v in ENGINES.items()})

@app.route('/api/transcribe', methods=['POST'])
@login_required
def start_transcribe():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': '没有上传文件'}), 400
    filename = file.filename or 'audio.mp3'
    audio_bytes = file.read()
    engine = request.form.get('engine', 'bcut')

    if engine not in ENGINES:
        return jsonify({'error': '未知引擎'}), 400

    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {
        'status': 'pending', 'progress': 0, 'message': '初始化...',
        'result': None, 'error': None, 'logs': [],
        'filename': filename, 'engine': engine, 'segments': None,
    }

    def run_task():
        task = tasks[task_id]
        def callback(progress, message):
            task['progress'] = progress
            task['message'] = message
            task['status'] = 'processing'
            import datetime
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            task['logs'].append(f'[{ts}] {message}')
            if len(task['logs']) > 200:
                task['logs'] = task['logs'][-200:]

        try:
            task['status'] = 'processing'
            import datetime
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            task['logs'].append(f'[{ts}] 开始识别: {filename} ({"%.1f" % (len(audio_bytes)/1024/1024)}MB)')

            segments = recognize_audio(audio_bytes, engine, filename, callback)

            task['segments'] = [seg.to_dict() for seg in segments]
            task['status'] = 'done'
            task['progress'] = 100
            task['message'] = f'识别完成，共 {len(segments)} 条字幕'
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            task['logs'].append(f'[{ts}] 识别完成! 共 {len(segments)} 条字幕')

            # Save to history
            try:
                conn = get_db()
                conn.execute(
                    "INSERT INTO history (id, filename, engine, segment_count) VALUES (?, ?, ?, ?)",
                    (task_id, filename, engine, len(segments))
                )
                conn.commit()
                conn.close()
            except:
                pass

        except Exception as e:
            task['status'] = 'error'
            task['error'] = str(e)
            task['message'] = f'识别失败: {str(e)}'
            import datetime
            ts = datetime.datetime.now().strftime('%H:%M:%S')
            task['logs'].append(f'[{ts}] 错误: {str(e)}')
            logger.error(f'Transcribe task {task_id} failed: {e}')

    t = threading.Thread(target=run_task)
    t.daemon = True
    t.start()
    return jsonify({'task_id': task_id})

@app.route('/api/progress/<task_id>')
@login_required
def progress(task_id):
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    resp = {
        'status': task['status'],
        'progress': task['progress'],
        'message': task['message'],
        'logs': task.get('logs', []),
        'error': task.get('error'),
    }
    if task['status'] == 'done' and task.get('segments'):
        resp['segments'] = task['segments']
        resp['total'] = len(task['segments'])
    return jsonify(resp)

@app.route('/api/edit/<task_id>', methods=['POST'])
@login_required
def edit_segment(task_id):
    """Edit a subtitle segment text."""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    data = request.get_json()
    index = data.get('index')
    text = data.get('text')
    if index is None or text is None:
        return jsonify({'error': '缺少参数'}), 400
    segments = task.get('segments', [])
    if 0 <= index < len(segments):
        segments[index]['text'] = text
        return jsonify({'ok': True})
    return jsonify({'error': '索引超出范围'}), 400

@app.route('/api/edit-time/<task_id>', methods=['POST'])
@login_required
def edit_time(task_id):
    """Edit a subtitle segment time."""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    data = request.get_json()
    index = data.get('index')
    start = data.get('start')
    end = data.get('end')
    if index is None:
        return jsonify({'error': '缺少参数'}), 400
    segments = task.get('segments', [])
    if 0 <= index < len(segments):
        if start is not None:
            segments[index]['start'] = int(start)
        if end is not None:
            segments[index]['end'] = int(end)
        return jsonify({'ok': True})
    return jsonify({'error': '索引超出范围'}), 400

@app.route('/api/add-segment/<task_id>', methods=['POST'])
@login_required
def add_segment(task_id):
    """Add a new subtitle segment."""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    data = request.get_json()
    text = data.get('text', '')
    start = data.get('start', 0)
    end = data.get('end', 0)
    segments = task.get('segments', [])
    segments.append({'text': text, 'start': int(start), 'end': int(end)})
    return jsonify({'ok': True, 'index': len(segments) - 1})

@app.route('/api/delete-segment/<task_id>', methods=['POST'])
@login_required
def delete_segment(task_id):
    """Delete a subtitle segment."""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    data = request.get_json()
    index = data.get('index')
    if index is None:
        return jsonify({'error': '缺少参数'}), 400
    segments = task.get('segments', [])
    if 0 <= index < len(segments):
        segments.pop(index)
        return jsonify({'ok': True})
    return jsonify({'error': '索引超出范围'}), 400

@app.route('/api/download/<task_id>')
@login_required
def download(task_id):
    """Download subtitle file."""
    task = tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '识别尚未完成'}), 400

    segments_data = task.get('segments', [])
    segs = [ASRSegment(s['text'], s['start'], s['end']) for s in segments_data]

    fmt = request.args.get('format', 'srt')
    if fmt == 'vtt':
        result = build_vtt(segs)
    elif fmt == 'txt':
        result = build_txt(segs)
    else:
        result = build_srt(segs)

    base_name = os.path.splitext(task['filename'])[0]
    out_filename = f'{base_name}.{fmt}'
    encoded = quote(out_filename)
    ascii_name = encoded.replace('%', 'X')[:50]
    cd = 'attachment; filename="' + ascii_name + '"; filename*=UTF-8' + chr(39) + chr(39) + encoded
    return Response(result, mimetype='application/octet-stream', headers={'Content-Disposition': cd})

@app.route('/api/cleanup', methods=['POST'])
@login_required
def cleanup():
    cleared = len(tasks)
    tasks.clear()
    return jsonify({'ok': True, 'message': f'已清理 {cleared} 个任务缓存'})

@app.route('/api/history')
@login_required
def history():
    conn = get_db()
    rows = conn.execute("SELECT id, filename, engine, segment_count, created_at FROM history ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5200, debug=True)
