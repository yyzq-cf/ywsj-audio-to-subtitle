"""
ASR engines - standalone implementation using free cloud ASR services.
Adapted from VideoCaptioner project (https://github.com/WEIFENG2333/VideoCaptioner)

Engines:
  - bcut (B站必剪)   : 免费，无第三方依赖，调用B站官方API
  - jianying (剪映)   : 免费，但签名依赖第三方服务 asrtools-update.bkfeng.top
"""

import datetime
import hashlib
import hmac
import json
import os
import time
import uuid
import logging
import io
import tempfile
import zlib
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any, Union

import requests
from pydub import AudioSegment

logger = logging.getLogger(__name__)

VERSION = "2.0.0"


class ASRSegment:
    def __init__(self, text: str, start_ms: int, end_ms: int):
        self.text = text
        self.start_ms = start_ms
        self.end_ms = end_ms

    def to_dict(self):
        return {'text': self.text, 'start': self.start_ms, 'end': self.end_ms}

    @property
    def start_str(self):
        return ms_to_srt_time(self.start_ms)

    @property
    def end_str(self):
        return ms_to_srt_time(self.end_ms)


def ms_to_srt_time(ms):
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    milli = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def audio_to_wav_bytes(audio_bytes: bytes, filename: str = '') -> bytes:
    """Convert any audio/video to WAV bytes using pydub/ffmpeg."""
    # 视频格式：先用ffmpeg提取音频
    video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.m4v', '.wmv')
    if filename.lower().endswith(video_exts):
        try:
            import subprocess
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp_in:
                tmp_in.write(audio_bytes)
                tmp_in_path = tmp_in.name
            tmp_out_path = tmp_in_path + '.wav'
            subprocess.run(['ffmpeg', '-i', tmp_in_path, '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', tmp_out_path, '-y'],
                           capture_output=True, timeout=120)
            with open(tmp_out_path, 'rb') as f:
                wav_bytes = f.read()
            os.unlink(tmp_in_path)
            os.unlink(tmp_out_path)
            return wav_bytes
        except Exception as e:
            logger.warning(f"Video to audio extraction failed: {e}")
            # fall through to pydub

    # 音频格式：用pydub转换
    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
    except Exception:
        return audio_bytes
    buf = io.BytesIO()
    audio.export(buf, format='wav')
    return buf.getvalue()


# ===== Bcut (B站必剪) ASR - 无第三方依赖 =====

API_BASE_URL = "https://member.bilibili.com/x/bcut/rubick-interface"
API_REQ_UPLOAD = API_BASE_URL + "/resource/create"
API_COMMIT_UPLOAD = API_BASE_URL + "/resource/create/complete"
API_CREATE_TASK = API_BASE_URL + "/task"
API_QUERY_RESULT = API_BASE_URL + "/task/result"


class BcutASR:
    """Bilibili Bcut ASR - 直接调用B站官方API，无需任何第三方服务。"""

    headers = {
        "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
        "Content-Type": "application/json",
        "Referer": "https://www.bilibili.com",
        "Origin": "https://www.bilibili.com",
    }

    def __init__(self, audio_bytes: bytes, filename: str = 'audio'):
        self.filename = filename
        self.file_binary = audio_to_wav_bytes(audio_bytes, self.filename)
        self.task_id: Optional[str] = None
        self._in_boss_key: Optional[str] = None
        self._resource_id: Optional[str] = None
        self._upload_id: Optional[str] = None
        self._upload_urls: List[str] = []
        self._per_size: int = 0
        self._clips: int = 0
        self._etags: List[str] = []
        self._download_url: Optional[str] = None
        # B站WAF需要cookie，用session先访问首页获取buvid3
        self.session = requests.Session()
        self._init_cookies()

    def _init_cookies(self):
        """访问B站首页获取WAF cookie（buvid3），避免query时412。"""
        try:
            self.session.get("https://www.bilibili.com", headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            }, timeout=10)
        except Exception:
            pass  # cookie获取失败不阻塞，query会自动重试

    def _upload(self):
        """请求上传凭证并上传音频文件。"""
        if not self.file_binary:
            raise ValueError("No audio data to upload")

        model_id = "8"

        payload = json.dumps({
            "type": 2,
            "name": "audio.mp3",
            "size": len(self.file_binary),
            "ResourceFileType": "mp3",
            "model_id": model_id,
        })
        resp = self.session.post(API_REQ_UPLOAD, data=payload, headers=self.headers, timeout=15)
        resp.raise_for_status()
        resp = resp.json()

        if resp.get("code") != 0:
            raise Exception(f"Bcut upload request failed: {resp.get('message', resp)}")

        data = resp["data"]
        self._in_boss_key = data["in_boss_key"]
        self._resource_id = data["resource_id"]
        self._upload_id = data["upload_id"]
        self._upload_urls = data["upload_urls"]
        self._per_size = int(data["per_size"])
        self._clips = len(data["upload_urls"])

        # 上传分片
        for clip in range(self._clips):
            start = clip * self._per_size
            end = (clip + 1) * self._per_size
            r = self.session.put(
                self._upload_urls[clip],
                data=self.file_binary[start:end],
                headers=self.headers,
                timeout=120,
            )
            r.raise_for_status()
            etag = r.headers.get("Etag")
            if etag is not None:
                self._etags.append(etag)

        # 提交上传
        commit_data = json.dumps({
            "InBossKey": self._in_boss_key,
            "ResourceId": self._resource_id,
            "Etags": ",".join(self._etags) if self._etags else "",
            "UploadId": self._upload_id,
            "model_id": model_id,
        })
        resp = self.session.post(API_COMMIT_UPLOAD, data=commit_data, headers=self.headers, timeout=15)
        resp.raise_for_status()
        resp = resp.json()
        if resp.get("code") != 0:
            raise Exception(f"Bcut commit upload failed: {resp.get('message', resp)}")
        self._download_url = resp["data"]["download_url"]

    def _create_task(self) -> str:
        """创建识别任务。"""
        resp = self.session.post(
            API_CREATE_TASK,
            json={"resource": self._download_url, "model_id": "8"},
            headers=self.headers,
            timeout=15,
        )
        resp.raise_for_status()
        resp = resp.json()
        if resp.get("code") != 0:
            raise Exception(f"Bcut create task failed: {resp.get('message', resp)}")
        self.task_id = resp["data"]["task_id"]
        return self.task_id or ""

    def _query_result(self, task_id: Optional[str] = None) -> dict:
        """轮询识别结果。B站WAF可能返回412，自动重试。"""
        tid = task_id or self.task_id
        for _ in range(300):
            resp = self.session.get(
                API_QUERY_RESULT,
                params={"model_id": 7, "task_id": tid},
                headers=self.headers,
                timeout=15,
            )
            # B站WAF可能返回412 Precondition Failed，等几秒重试
            if resp.status_code == 412:
                time.sleep(3)
                continue
            resp.raise_for_status()
            resp = resp.json()
            if resp.get("code") != 0:
                raise Exception(f"Bcut query failed: {resp.get('message', resp)}")
            data = resp.get("data", {})
            state = data.get("state", -1)
            # state: 0=待处理 1=处理中 2=失败 3=部分成功 4=完成
            if state in (2, 3):
                err = data.get("err", "未知错误")
                if state == 3:
                    # 部分成功时也可能没有result，继续等
                    if data.get("result"):
                        return json.loads(data["result"])
                raise Exception(f"Bcut ASR task failed (state={state}): {err}")
            if state == 4:
                if not data.get("result"):
                    return {"utterances": []}  # 没有识别到内容
                return json.loads(data["result"])
            time.sleep(1)
        raise Exception("Bcut ASR recognition timeout (300s)")

    def recognize(self, callback=None) -> List[ASRSegment]:
        if callback: callback(10, '请求上传凭证...')
        self._upload()
        if callback: callback(40, '提交识别任务...')
        self._create_task()
        if callback: callback(60, '识别中，请稍候...')
        result = self._query_result()
        if callback: callback(90, '解析结果...')
        return self._parse(result)

    def _parse(self, result: dict) -> List[ASRSegment]:
        segments = []
        utterances = result.get("utterances", [])
        for u in utterances:
            text = u.get("transcript", "").strip()
            start = u.get("start_time", 0)
            end = u.get("end_time", 0)
            if text:
                segments.append(ASRSegment(text, int(start), int(end)))
        return segments


# ===== AWS Signature helpers (for JianYing) =====

def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _aws_signature(secret_key: str, request_parameters: str, headers: Dict[str, str],
                   method: str = "GET", region: str = "cn", service: str = "vod") -> str:
    canonical_uri = "/"
    canonical_querystring = request_parameters
    canonical_headers = "\n".join([f"{k}:{v}" for k, v in headers.items()]) + "\n"
    signed_headers = ";".join(headers.keys())
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = f"{method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    amzdate = headers["x-amz-date"]
    datestamp = amzdate.split("T")[0]
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = f"{algorithm}\n{amzdate}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    signing_key = _get_signature_key(secret_key, datestamp, region, service)
    return hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


# ===== JianYing (CapCut) ASR - 依赖第三方签名服务 =====

class JianYingASR:
    """JianYing free ASR - uses remote sign service at asrtools-update.bkfeng.top."""

    def __init__(self, audio_bytes: bytes, filename: str = 'audio'):
        self.file_binary = audio_to_wav_bytes(audio_bytes)
        self.filename = filename
        self.crc32_hex = format(zlib.crc32(self.file_binary) & 0xFFFFFFFF, "08x")

        self.session_token = None
        self.secret_key = None
        self.access_key = None
        self.store_uri = None
        self.auth = None
        self.upload_id = None
        self.session_key = None
        self.upload_hosts = None
        self.tdid = self._get_tid()
        self._sign_cache = {}

    def _get_tid(self) -> str:
        i = str(datetime.datetime.now().year)[3]
        fr = 390 + int(i)
        ed = "3278516897751" if int(i) % 2 != 0 else f"{uuid.getnode():013d}"
        return f"{fr}{ed}"

    def _generate_sign(self, url_path: str) -> Tuple[str, str]:
        """Get sign from remote service (cached per second)."""
        current_time = str(int(time.time()))
        cache_key = f"{url_path}:{current_time}"
        if cache_key in self._sign_cache:
            return self._sign_cache[cache_key]
        data = {
            "url": url_path,
            "current_time": current_time,
            "pf": "4",
            "appvr": "6.6.0",
            "tdid": self.tdid,
        }
        headers = {
            "User-Agent": f"VideoCaptioner/{VERSION}",
            "tdid": self.tdid,
            "t": current_time,
        }
        resp = requests.post("https://asrtools-update.bkfeng.top/sign", json=data, headers=headers, timeout=15)
        resp.raise_for_status()
        sign = resp.json().get("sign")
        if not sign:
            raise ValueError("Failed to get sign from remote service")
        result = (sign.lower(), current_time)
        self._sign_cache[cache_key] = result
        return result

    def _build_headers(self, device_time: str, sign: str) -> Dict[str, str]:
        return {
            "User-Agent": "Cronet/TTNetVersion:d4572e53 2024-06-12 QuicVersion:4bf243e0 2023-04-17",
            "appvr": "6.6.0",
            "device-time": str(device_time),
            "pf": "4",
            "sign": sign,
            "sign-ver": "1",
            "tdid": self.tdid,
        }

    def _upload_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Authorization": self.auth,
            "Content-CRC32": self.crc32_hex,
        }

    def _upload_sign(self):
        url = "https://lv-pc-api-sinfonlinec.ulikecam.com/lv/v1/upload_sign"
        sign, dt = self._generate_sign("/lv/v1/upload_sign")
        headers = self._build_headers(dt, sign)
        resp = requests.post(url, data=json.dumps({"biz": "pc-recognition"}), headers=headers, timeout=30)
        resp.raise_for_status()
        d = resp.json()["data"]
        self.access_key = d["access_key_id"]
        self.secret_key = d["secret_access_key"]
        self.session_token = d["session_token"]

    def _upload_auth(self):
        file_size = len(self.file_binary)
        req_params = f"Action=ApplyUploadInner&FileSize={file_size}&FileType=object&IsInner=1&SpaceName=lv-mac-recognition&Version=2020-11-19&s=5y0udbjapi"
        t = datetime.datetime.now(datetime.timezone.utc)
        amz_date = t.strftime("%Y%m%dT%H%M%SZ")
        headers = {"x-amz-date": amz_date, "x-amz-security-token": self.session_token}
        sig = _aws_signature(self.secret_key, req_params, headers)
        auth = f"AWS4-HMAC-SHA256 Credential={self.access_key}/{amz_date[:8]}/cn/vod/aws4_request, SignedHeaders=x-amz-date;x-amz-security-token, Signature={sig}"
        headers["authorization"] = auth
        resp = requests.get(f"https://vod.bytedanceapi.com/?{req_params}", headers=headers, timeout=30)
        resp.raise_for_status()
        d = resp.json()["Result"]["UploadAddress"]
        self.store_uri = d["StoreInfos"][0]["StoreUri"]
        self.auth = d["StoreInfos"][0]["Auth"]
        self.upload_id = d["StoreInfos"][0]["UploadID"]
        self.session_key = d["SessionKey"]
        self.upload_hosts = d["UploadHosts"][0]

    def _upload_file(self):
        url = f"https://{self.upload_hosts}/{self.store_uri}?partNumber=1&uploadID={self.upload_id}"
        resp = requests.put(url, data=self.file_binary, headers=self._upload_headers(), timeout=120)
        resp.raise_for_status()
        assert resp.json()["success"] == 0, f"Upload failed: {resp.text}"

    def _upload_check(self):
        url = f"https://{self.upload_hosts}/{self.store_uri}?uploadID={self.upload_id}"
        resp = requests.post(url, data=f"1:{self.crc32_hex}", headers=self._upload_headers(), timeout=30)
        resp.raise_for_status()

    def _upload_commit(self):
        url = f"https://{self.upload_hosts}/{self.store_uri}?uploadID={self.upload_id}&partNumber=1&x-amz-security-token={self.session_token}"
        requests.put(url, data=self.file_binary, headers=self._upload_headers(), timeout=60)
        return self.store_uri

    def _submit(self) -> str:
        url = "https://lv-pc-api-sinfonlinec.ulikecam.com/lv/v1/audio_subtitle/submit"
        payload = {
            "adjust_endtime": 200, "audio": self.store_uri, "caption_type": 2,
            "client_request_id": str(uuid.uuid4()), "max_lines": 1,
            "songs_info": [{"end_time": 6000000, "id": "", "start_time": 0}],
            "words_per_line": 16,
        }
        sign, dt = self._generate_sign("/lv/v1/audio_subtitle/submit")
        resp = requests.post(url, json=payload, headers=self._build_headers(dt, sign), timeout=30)
        resp.raise_for_status()
        d = resp.json()
        if d.get("ret") != "0":
            raise Exception(f"JianYing submit failed: {d.get('errmsg', '')}")
        return d["data"]["id"]

    def _query(self, query_id: str) -> dict:
        url = "https://lv-pc-api-sinfonlinec.ulikecam.com/lv/v1/audio_subtitle/query"
        payload = {"id": query_id, "pack_options": {"need_attribute": True}}
        for _ in range(120):
            sign, dt = self._generate_sign("/lv/v1/audio_subtitle/query")
            resp = requests.post(url, json=payload, headers=self._build_headers(dt, sign), timeout=30)
            resp.raise_for_status()
            d = resp.json()
            if d.get("ret") != "0":
                raise Exception(f"JianYing query failed: {d.get('errmsg', '')}")
            result = d.get("data", {})
            if result.get("code") == 2:  # Complete
                return result
            time.sleep(3)
        raise Exception("JianYing recognition timeout")

    def recognize(self, callback=None) -> List[ASRSegment]:
        if callback: callback(10, '获取上传凭证...')
        self._upload_sign()
        if callback: callback(20, '上传音频到剪映服务器...')
        self._upload_auth()
        self._upload_file()
        self._upload_check()
        self._upload_commit()
        if callback: callback(50, '提交识别任务...')
        query_id = self._submit()
        if callback: callback(60, '识别中...')
        result = self._query(query_id)
        if callback: callback(90, '解析结果...')
        return self._parse(result)

    def _parse(self, result: dict) -> List[ASRSegment]:
        segments = []
        utterances = result.get("utterances", [])
        for u in utterances:
            text = u.get("text", "").strip()
            start = u.get("start_time", 0)
            end = u.get("end_time", 0)
            if text:
                segments.append(ASRSegment(text, start, end))
        return segments


# ===== SiliconFlow / OpenAI-compatible Whisper ASR =====

class OpenAICompatibleASR:
    """OpenAI-compatible Whisper ASR - supports SiliconFlow, OpenAI, Groq, etc."""

    def __init__(self, audio_bytes: bytes, filename: str = 'audio',
                 api_key: str = '', base_url: str = '', model: str = ''):
        self.filename = filename
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model
        # 视频格式先提取音频
        video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.m4v', '.wmv')
        if filename.lower().endswith(video_exts):
            self.audio_bytes = audio_to_wav_bytes(audio_bytes, filename)
        else:
            self.audio_bytes = audio_bytes

    def recognize(self, callback=None) -> List[ASRSegment]:
        if not self.api_key:
            raise ValueError("API Key 未配置，请在设置页面填写")
        if not self.base_url:
            raise ValueError("API Base URL 未配置，请在设置页面填写")
        if not self.model:
            raise ValueError("模型名称未配置，请在设置页面填写")

        if callback: callback(15, f'上传音频到 {self.base_url.split("//")[1].split("/")[0]}...')
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        # 根据实际音频格式设置文件名和Content-Type
        video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.m4v', '.wmv')
        if self.filename.lower().endswith(video_exts):
            # 视频已提取为WAV
            send_filename = 'audio.wav'
            send_content_type = 'audio/wav'
        elif self.filename.lower().endswith('.mp3'):
            send_filename = self.filename
            send_content_type = 'audio/mp3'
        elif self.filename.lower().endswith('.wav'):
            send_filename = self.filename
            send_content_type = 'audio/wav'
        elif self.filename.lower().endswith('.m4a'):
            send_filename = self.filename
            send_content_type = 'audio/mp4'
        elif self.filename.lower().endswith('.ogg'):
            send_filename = self.filename
            send_content_type = 'audio/ogg'
        elif self.filename.lower().endswith('.flac'):
            send_filename = self.filename
            send_content_type = 'audio/flac'
        else:
            send_filename = self.filename or 'audio.mp3'
            send_content_type = 'audio/mpeg'

        # 获取音频时长
        try:
            audio = AudioSegment.from_file(io.BytesIO(self.audio_bytes))
            duration = len(audio) / 1000.0
        except Exception:
            duration = 60

        if callback: callback(30, f'识别中（音频 {duration:.1f}s）...')

        resp = requests.post(
            url,
            headers=headers,
            files={"file": (send_filename, self.audio_bytes, send_content_type)},
            data={
                "model": self.model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "segment",
            },
            timeout=300
        )

        if callback: callback(80, '解析识别结果...')

        if resp.status_code != 200:
            error_msg = resp.text[:300]
            try:
                error_data = resp.json()
                error_msg = error_data.get('message', error_msg)
            except Exception:
                pass
            raise Exception(f"API错误 (HTTP {resp.status_code}): {error_msg}")

        data = resp.json()
        if callback: callback(95, '生成字幕...')
        return self._parse(data, duration)

    def _parse(self, data: dict, duration: float) -> List[ASRSegment]:
        segments = []
        api_segments = data.get("segments", [])
        if api_segments:
            for s in api_segments:
                text = s.get("text", "").strip()
                start = int(float(s.get("start", 0)) * 1000)
                end = int(float(s.get("end", 0)) * 1000)
                if text:
                    segments.append(ASRSegment(text, start, end))
        if not segments:
            text = data.get("text", "").strip()
            if text:
                segments = self._split_text_to_segments(text, duration)
        return segments

    def _split_text_to_segments(self, text: str, duration: float) -> List[ASRSegment]:
        """纯文本无时间戳时，按语义断句+按字数比例分配时间。"""
        import re

        # 第一步：按句号/问号/感叹号/换行 断句
        sentences = [p.strip() for p in re.split(r'[。！？!?\n]+', text) if p.strip()]

        # 如果整段没有句号(像说话连贯的场景)，按逗号+语义断句
        if len(sentences) <= 1:
            sentences = [p.strip() for p in re.split(r'[，,。！？!?\n]+', text) if p.strip()]

        # 第二步：每条字幕限制最多约20字，超长的再按逗号/空格切
        parts = []
        for sent in sentences:
            if len(sent) <= 20:
                parts.append(sent)
            else:
                # 按逗号/顿号再切
                sub = [s.strip() for s in re.split(r'[，,、；;]+', sent) if s.strip()]
                if len(sub) <= 1:
                    # 考虑按空格/连字符切(中英混合场景: "ywsj-kms" "Windows Office")
                    if (re.search(r'[a-zA-Z]', sent) and (' ' in sent or '-' in sent)):
                        # 按空格切，连字符保留在词内
                        words = re.split(r'(?<=[\u4e00-\u9fff，。！？])\s+|\s+(?=[\u4e00-\u9fff])|(?<=\w)\s+(?=\w)', sent)
                        words = [w.strip() for w in words if w.strip()]
                        if len(words) <= 1:
                            words = sent.split(' ')
                        buf = ''
                        for s in words:
                            sep = ' ' if buf and not buf[-1] in '，。！？' else ''
                            if len(buf) + len(s) + (1 if sep else 0) <= 20:
                                buf = buf + sep + s if buf else s
                            else:
                                if buf: parts.append(buf)
                                buf = s
                        if buf: parts.append(buf)
                    else:
                        # 纯中文无标点，按字数硬切(每20字)
                        for i in range(0, len(sent), 18):
                            parts.append(sent[i:i+18])
                else:
                    # 把逗号切出来的短句合并到不超过18字
                    buf = ''
                    for s in sub:
                        if len(buf) + len(s) + 1 <= 20:
                            buf = buf + s if not buf else buf + '，' + s
                        else:
                            if buf: parts.append(buf)
                            buf = s
                    if buf: parts.append(buf)

        if not parts:
            parts = [text]

        # 第三步：按字数比例分配时间（不是均匀分）
        total_chars = sum(len(p) for p in parts)
        if total_chars == 0:
            total_chars = 1
        total_ms = int(duration * 1000)
        segments = []
        current_ms = 0
        for i, part in enumerate(parts):
            char_ratio = len(part) / total_chars
            seg_ms = int(total_ms * char_ratio)
            start = current_ms
            if i == len(parts) - 1:
                end = total_ms  # 最后一条吃满
            else:
                end = start + seg_ms
            current_ms = end
            segments.append(ASRSegment(part, start, end))
        return segments


# ===== Engine registry (内置引擎，用户接口通过 /api/engines 动态返回) =====

STATIC_ENGINES = {
    'bcut': {
        'label': '必剪 (B站) - 免费·仅中英文',
        'needs_key': False,
        'languages': '中文、英文',
    },
}


def recognize_audio(audio_bytes: bytes, engine: str, filename: str = 'audio',
                    callback=None, api_key: str = '', base_url: str = '', model: str = '') -> List[ASRSegment]:
    if engine == 'bcut':
        asr = BcutASR(audio_bytes, filename)
        return asr.recognize(callback)
    elif engine.startswith('ep:'):
        # 用户自定义接口，统一用 OpenAICompatibleASR
        if not api_key:
            raise ValueError("该接口未配置 API Key")
        if not base_url:
            raise ValueError("该接口未配置 Base URL")
        if not model:
            raise ValueError("该接口未配置模型名称")
        asr = OpenAICompatibleASR(audio_bytes, filename, api_key=api_key, base_url=base_url, model=model)
        return asr.recognize(callback)
    raise ValueError(f"Unknown engine: {engine}")


# ===== Subtitle builders =====

def build_srt(segments: List[ASRSegment]) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{seg.start_str} --> {seg.end_str}")
        lines.append(seg.text)
        lines.append('')
    return '\n'.join(lines)


def build_vtt(segments: List[ASRSegment]) -> str:
    lines = ['WEBVTT', '']
    for i, seg in enumerate(segments, 1):
        start = seg.start_str.replace(',', '.')
        end = seg.end_str.replace(',', '.')
        lines.append(f"{start} --> {end}")
        lines.append(seg.text)
        lines.append('')
    return '\n'.join(lines)


def build_txt(segments: List[ASRSegment]) -> str:
    return '\n'.join(seg.text for seg in segments)
