import os
import io
import json
import uuid
import secrets
import threading
import logging
import time
import sqlite3
import requests
import pyotp
import qrcode
import base64
from functools import wraps
from urllib.parse import quote, urlparse
from flask import Flask, request, jsonify, render_template, Response, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

from asr_engine import STATIC_ENGINES, recognize_audio, ASRSegment, build_srt, build_vtt, build_txt, ms_to_srt_time

# ===== API Key encryption (Fernet) =====
_fernet = None

def _get_fernet():
    """Lazily init Fernet cipher using a key derived from the Flask secret key."""
    global _fernet
    if _fernet is not None:
        return _fernet
    import hashlib
    from cryptography.fernet import Fernet
    # Derive a 32-byte key from the app's secret key (which is persisted)
    raw = app.secret_key.encode('utf-8') if app.secret_key else b'default-secret-key-change-me'
    key = hashlib.sha256(raw).hexdigest()[:32]
    import base64
    fernet_key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    _fernet = Fernet(fernet_key)
    return _fernet

def encrypt_api_key(plain_key: str) -> str:
    """Encrypt an API key. Returns 'enc:...' prefixed string."""
    if not plain_key:
        return ''
    if plain_key.startswith('enc:'):
        return plain_key  # already encrypted
    try:
        token = _get_fernet().encrypt(plain_key.encode('utf-8')).decode('ascii')
        return 'enc:' + token
    except Exception:
        return plain_key  # fallback to plain if encryption fails

def decrypt_api_key(stored_key: str) -> str:
    """Decrypt an API key. Handles both old plain keys and new 'enc:...' keys."""
    if not stored_key:
        return ''
    if not stored_key.startswith('enc:'):
        return stored_key  # old plain key, return as-is (backward compat)
    try:
        return _get_fernet().decrypt(stored_key[4:].encode('ascii')).decode('utf-8')
    except Exception:
        return ''  # decryption failed (e.g. secret key changed)

def _is_encrypted(val: str) -> bool:
    return bool(val and val.startswith('enc:'))

# ywsj-audio-to-subtitle
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
            password TEXT NOT NULL,
            totp_secret TEXT
        );
        CREATE TABLE IF NOT EXISTS history (
            id TEXT PRIMARY KEY,
            filename TEXT,
            engine TEXT,
            segment_count INTEGER,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
    """)
    # Migration: 如果auth表没有totp_secret列，加上
    cols = [r[1] for r in conn.execute("PRAGMA table_info(auth)").fetchall()]
    if 'totp_secret' not in cols:
        conn.execute("ALTER TABLE auth ADD COLUMN totp_secret TEXT")
    conn.commit()
    conn.close()

init_db()

# ===== Auth =====
def load_auth():
    conn = get_db()
    row = conn.execute("SELECT username, password, totp_secret FROM auth WHERE id = 1").fetchone()
    conn.close()
    if row:
        return {'username': row['username'], 'password': row['password'], 'totp_secret': row['totp_secret'] if 'totp_secret' in row.keys() else None}
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

# ===== 2FA (TOTP) =====
_2fa_pending = {}  # {token: {username, expires, attempts}}
_2fa_lock = threading.Lock()
_2FA_EXPIRY = 180  # 3分钟
_2FA_MAX_ATTEMPTS = 5

def get_totp_secret():
    """获取当前用户的TOTP secret"""
    auth = load_auth()
    return auth.get('totp_secret') if auth else None

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_auth_enabled():
        return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        # 判断是2FA验证阶段还是密码阶段
        pending_token = request.form.get('pending_token', '')
        if pending_token:
            # 2FA验证阶段
            code = request.form.get('code', '').strip()
            with _2fa_lock:
                now = time.time()
                for k in list(_2fa_pending):
                    if _2fa_pending[k]['expires'] < now:
                        del _2fa_pending[k]
                pending = _2fa_pending.get(pending_token)

            if not pending:
                error = '验证已超时，请重新登录'
            else:
                attempts = pending.get('attempts', 0)
                if attempts >= _2FA_MAX_ATTEMPTS:
                    with _2fa_lock:
                        _2fa_pending.pop(pending_token, None)
                    error = '验证码错误次数过多，请重新登录'
                else:
                    secret = get_totp_secret()
                    totp = pyotp.TOTP(secret) if secret else None
                    if totp and totp.verify(code, valid_window=1):
                        # 2FA验证成功
                        with _2fa_lock:
                            _2fa_pending.pop(pending_token, None)
                        session.clear()
                        session['logged_in'] = True
                        session.permanent = True
                        return redirect(url_for('index'))
                    else:
                        with _2fa_lock:
                            pending['attempts'] = attempts + 1
                            _2fa_pending[pending_token] = pending
                        remaining = _2FA_MAX_ATTEMPTS - pending['attempts']
                        error = f'验证码错误，剩余尝试次数 {remaining} 次'
            return render_template('login.html', error=error, version=APP_VERSION,
                                   pending_token=pending_token, twofa_stage=True)

        # 密码阶段
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
                # 检查是否启用了2FA
                secret = auth.get('totp_secret')
                if secret:
                    # 需要2FA验证
                    pending_token = secrets.token_hex(32)
                    with _2fa_lock:
                        now = time.time()
                        for k in list(_2fa_pending):
                            if _2fa_pending[k]['expires'] < now:
                                del _2fa_pending[k]
                        _2fa_pending[pending_token] = {
                            'username': username,
                            'expires': now + _2FA_EXPIRY,
                            'attempts': 0,
                        }
                    return render_template('login.html', error=None, version=APP_VERSION,
                                           pending_token=pending_token, twofa_stage=True)
                else:
                    # 无需2FA，直接登录
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
    return render_template('login.html', error=error, version=APP_VERSION, twofa_stage=False)


# ===== 2FA管理API =====
@app.route('/api/2fa/status')
@login_required
def twofa_status():
    return jsonify({'enabled': bool(get_totp_secret())})

@app.route('/api/2fa/setup', methods=['POST'])
@login_required
def twofa_setup():
    """生成TOTP secret和二维码"""
    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=load_auth()['username'], issuer_name="音频转字幕")
    # 本地生成QR码
    qr = qrcode.QRCode(version=1, box_size=8, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    qr_data_uri = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    session['_pending_totp_secret'] = secret
    return jsonify({'secret': secret, 'qr_data_uri': qr_data_uri})

@app.route('/api/2fa/confirm', methods=['POST'])
@login_required
def twofa_confirm():
    """验证TOTP码并保存secret到数据库"""
    code = (request.json or {}).get('code', '').strip()
    secret = session.get('_pending_totp_secret')
    if not secret or not code:
        return jsonify({'ok': False, 'error': '请先获取二维码并输入验证码'}), 400
    totp = pyotp.TOTP(secret)
    if not totp.verify(code, valid_window=1):
        return jsonify({'ok': False, 'error': '验证码错误'}), 401
    conn = get_db()
    conn.execute("UPDATE auth SET totp_secret=? WHERE id=1", (secret,))
    conn.commit()
    conn.close()
    session.pop('_pending_totp_secret', None)
    return jsonify({'ok': True})

@app.route('/api/2fa/disable', methods=['POST'])
@login_required
def twofa_disable():
    """关闭2FA，需要验证当前密码"""
    password = (request.json or {}).get('password', '')
    auth = load_auth()
    if not auth or not check_password_hash(auth['password'], password):
        return jsonify({'ok': False, 'error': '密码错误'}), 403
    conn = get_db()
    conn.execute("UPDATE auth SET totp_secret=NULL WHERE id=1")
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

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
    return render_template('index.html', engines=STATIC_ENGINES, version=APP_VERSION)

@app.route('/api/engines')
@login_required
def engines():
    """返回可用引擎列表: 内置引擎 + 用户配置的接口"""
    result = {}
    # 内置引擎
    for k, v in STATIC_ENGINES.items():
        result[k] = {'label': v['label'], 'type': 'builtin', 'needs_key': False, 'languages': v.get('languages', '')}
    # 用户配置的接口
    cfg = load_config()
    for ep in cfg.get('endpoints', []):
        ep_id = f"ep:{ep['id']}"
        name = ep.get('name', ep.get('type', ''))
        result[ep_id] = {
            'label': name,
            'type': ep.get('type', 'openai'),
            'needs_key': True,
            'languages': _type_languages(ep.get('type', 'openai')),
        }
    return result


def _type_languages(t):
    return {'siliconflow': '中/英/日/韩/粤等', 'openai': '中/英/日/韩等(Whisper)', 'groq': '多语言(Whisper)'}.get(t, '多语言')


# ===== Endpoint (API接口) storage =====
def _get_config_path():
    return os.path.join(DATA_DIR, 'config.json')

def load_config():
    import json as _json
    try:
        with open(_get_config_path(), 'r') as f:
            return _json.load(f)
    except (OSError, IOError, ValueError):
        return {}

def save_config(cfg):
    import json as _json
    with open(_get_config_path(), 'w') as f:
        _json.dump(cfg, f, ensure_ascii=False, indent=2)

def _mask(key):
    if not key: return ''
    return key[:8] + '****' + key[-4:] if len(key) > 12 else '****'

TYPE_DEFAULTS = {
    'siliconflow': {'base_url': 'https://api.siliconflow.cn/v1', 'model': 'FunAudioLLM/SenseVoiceSmall'},
    'openai':      {'base_url': 'https://api.openai.com/v1',     'model': 'whisper-1'},
    'groq':        {'base_url': 'https://api.groq.com/openai/v1',  'model': 'whisper-large-v3'},
}

# ===== SSRF protection =====
import ipaddress

def _is_safe_url(url_str: str) -> bool:
    """Reject URLs pointing to internal/private/loopback addresses."""
    if not url_str:
        return False
    try:
        parsed = urlparse(url_str)
        if parsed.scheme not in ('http', 'https'):
            return False
        hostname = parsed.hostname or ''
        if not hostname:
            return False
        # Block obvious internal hostnames
        if hostname.lower() in ('localhost', 'ip6-localhost', 'ip6-loopback'):
            return False
        # Try to parse as IP address
        try:
            ip = ipaddress.ip_address(hostname)
            # Reject private, loopback, link-local, multicast, unspecified
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                return False
        except ValueError:
            # It's a domain name, not an IP — allow it
            # But block internal-looking TLDs
            if hostname.endswith('.internal') or hostname.endswith('.local') or hostname.endswith('.localhost'):
                return False
        # Block common metadata endpoints
        if hostname in ('169.254.169.254', 'fd00:ec2::254') or hostname.startswith('169.254.'):
            return False
        return True
    except Exception:
        return False

def _validate_base_url(base_url: str) -> str:
    """Validate base_url is safe. Returns error message or empty string if OK."""
    if not base_url:
        return 'Base URL 未填写'
    if not _is_safe_url(base_url):
        return 'Base URL 不安全（不允许内网/本地地址）'
    return ''

@app.route('/api/endpoints', methods=['GET'])
@login_required
def get_endpoints():
    cfg = load_config()
    eps = cfg.get('endpoints', [])
    return jsonify([{
        'id': ep['id'],
        'name': ep.get('name', ''),
        'type': ep.get('type', 'openai'),
        'api_key': _mask(decrypt_api_key(ep.get('api_key', ''))),
        'api_key_set': bool(ep.get('api_key')),
        'base_url': ep.get('base_url', TYPE_DEFAULTS.get(ep.get('type','openai'),{}).get('base_url','')),
        'model': ep.get('model', ''),
    } for ep in eps])

@app.route('/api/endpoints', methods=['POST'])
@login_required
def add_endpoint():
    data = request.json or {}
    ep_type = data.get('type', 'openai')
    if ep_type not in TYPE_DEFAULTS:
        return jsonify({'error': '未知接口类型'}), 400
    cfg = load_config()
    eps = cfg.get('endpoints', [])
    ep = {
        'id': str(uuid.uuid4())[:8],
        'name': data.get('name', '').strip() or f"{ep_type}-{len(eps)+1}",
        'type': ep_type,
        'api_key': encrypt_api_key(data.get('api_key', '').strip()),
        'base_url': data.get('base_url', '').strip() or TYPE_DEFAULTS[ep_type]['base_url'],
        'model': data.get('model', '').strip() or TYPE_DEFAULTS[ep_type]['model'],
    }
    if not ep['api_key']:
        return jsonify({'error': 'API Key 不能为空'}), 400
    eps.append(ep)
    cfg['endpoints'] = eps
    save_config(cfg)
    return jsonify({'ok': True, 'id': ep['id']})

@app.route('/api/endpoints/<ep_id>', methods=['PUT'])
@login_required
def update_endpoint(ep_id):
    data = request.json or {}
    cfg = load_config()
    eps = cfg.get('endpoints', [])
    for ep in eps:
        if ep['id'] == ep_id:
            if 'name' in data:
                ep['name'] = data['name'].strip() or ep['name']
            if 'api_key' in data and data['api_key'].strip():
                ep['api_key'] = encrypt_api_key(data['api_key'].strip())
            if 'base_url' in data and data['base_url'].strip():
                ep['base_url'] = data['base_url'].strip()
            if 'model' in data and data['model'].strip():
                ep['model'] = data['model'].strip()
            save_config(cfg)
            return jsonify({'ok': True})
    return jsonify({'error': '接口不存在'}), 404

@app.route('/api/endpoints/<ep_id>', methods=['DELETE'])
@login_required
def delete_endpoint(ep_id):
    cfg = load_config()
    eps = cfg.get('endpoints', [])
    cfg['endpoints'] = [ep for ep in eps if ep['id'] != ep_id]
    save_config(cfg)
    return jsonify({'ok': True})

@app.route('/api/endpoints/test', methods=['POST'])
@login_required
def test_endpoint():
    """测试接口连通性。可直接传参数测试(未保存),也可传id用已保存的。"""
    data = request.json or {}
    ep_id = data.get('id', '')

    if ep_id:  # 用已保存的接口测试
        cfg = load_config()
        ep = None
        for e in cfg.get('endpoints', []):
            if e['id'] == ep_id:
                ep = e; break
        if not ep:
            return jsonify({'ok': False, 'error': '接口不存在'}), 404
        api_key = decrypt_api_key(ep.get('api_key', ''))
        base_url = ep.get('base_url', '')
        model = ep.get('model', '')
        ep_name = ep.get('name', '')
    else:  # 用表单参数测试
        ep_type = data.get('type', 'openai')
        api_key = data.get('api_key', '')
        base_url = data.get('base_url', '') or TYPE_DEFAULTS.get(ep_type, {}).get('base_url', '')
        model = data.get('model', '') or TYPE_DEFAULTS.get(ep_type, {}).get('model', '')
        ep_name = data.get('name', ep_type)

    if not api_key:
        return jsonify({'ok': False, 'error': 'API Key 未填写'}), 400
    url_err = _validate_base_url(base_url)
    if url_err:
        return jsonify({'ok': False, 'error': url_err}), 400

    # 生成0.5秒静音音频
    import io as _io
    try:
        from pydub import AudioSegment
        silent = AudioSegment.silent(duration=500)
        buf = _io.BytesIO()
        silent.export(buf, format='mp3')
        test_audio = buf.getvalue()
    except Exception:
        return jsonify({'ok': False, 'error': '音频生成失败(ffmpeg未安装?)'}), 500

    try:
        url = f"{base_url.rstrip('/')}/audio/transcriptions"
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("test.mp3", test_audio, "audio/mp3")},
            data={"model": model, "response_format": "verbose_json"},
            timeout=30
        )
        if resp.status_code == 200:
            d = resp.json()
            text = d.get('text', '')
            return jsonify({'ok': True, 'message': f'「{ep_name}」连接成功 · 模型 {model} 可用'})
        else:
            try:
                err = resp.json()
                msg = err.get('message', err.get('error', {}).get('message', resp.text[:200]))
            except Exception:
                msg = resp.text[:200]
            return jsonify({'ok': False, 'error': f'HTTP {resp.status_code}: {msg}'})
    except requests.exceptions.Timeout:
        return jsonify({'ok': False, 'error': '请求超时(30s)'})
    except requests.exceptions.ConnectionError as e:
        return jsonify({'ok': False, 'error': f'连接失败: {str(e)[:100]}'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:200]})


@app.route('/api/transcribe', methods=['POST'])
@login_required
def start_transcribe():
    file = request.files.get('file')
    if not file:
        return jsonify({'error': '没有上传文件'}), 400
    filename = file.filename or 'audio.mp3'
    audio_bytes = file.read()
    engine = request.form.get('engine', 'bcut')

    if engine not in STATIC_ENGINES and not engine.startswith('ep:'):
        return jsonify({'error': '未知引擎'}), 400

    # 如果是用户配置的接口(ep:xxx)，读取对应endpoint配置
    ep_api_key = ''
    ep_base_url = ''
    ep_model = ''
    if engine.startswith('ep:'):
        ep_id = engine[3:]
        cfg = load_config()
        ep = None
        for e in cfg.get('endpoints', []):
            if e['id'] == ep_id:
                ep = e; break
        if not ep:
            return jsonify({'error': '接口不存在，请检查设置'}), 400
        if not ep.get('api_key'):
            return jsonify({'error': f'接口「{ep.get("name","")}」未配置 API Key'}), 400
        ep_api_key = decrypt_api_key(ep.get('api_key', ''))
        ep_base_url = ep.get('base_url', '')
        ep_model = ep.get('model', '')
        # SSRF protection: validate base_url before making outbound request
        url_err = _validate_base_url(ep_base_url)
        if url_err:
            return jsonify({'error': url_err}), 400

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

            segments = recognize_audio(audio_bytes, engine, filename, callback,
                                       api_key=ep_api_key,
                                       base_url=ep_base_url,
                                       model=ep_model)

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
    app.run(host='0.0.0.0', port=5200, debug=os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true'))
