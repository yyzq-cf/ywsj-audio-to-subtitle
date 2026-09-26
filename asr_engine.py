"""
ASR engines - standalone implementation using JianYing (ByteDance) free ASR.
Adapted from VideoCaptioner project (https://github.com/WEIFENG2333/VideoCaptioner)
Sign generation uses remote service at asrtools-update.bkfeng.top
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
from typing import List, Optional, Tuple, Dict, Any, Union

import requests
from pydub import AudioSegment

logger = logging.getLogger(__name__)

VERSION = "1.0.0"


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


def audio_to_wav_bytes(audio_bytes: bytes) -> bytes:
    """Convert any audio to WAV bytes using pydub."""
    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
    except Exception:
        return audio_bytes
    buf = io.BytesIO()
    audio.export(buf, format='wav')
    return buf.getvalue()


# ===== AWS Signature helpers =====

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


# ===== JianYing (CapCut) ASR =====

class JianYingASR:
    """JianYing free ASR - uses remote sign service."""

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


# ===== Engine registry =====

ENGINES = {
    'jianying': {
        'label': '剪映 (字节跳动) - 免费',
        'needs_key': False,
    },
}


def recognize_audio(audio_bytes: bytes, engine: str, filename: str = 'audio',
                    callback=None) -> List[ASRSegment]:
    if engine == 'jianying':
        asr = JianYingASR(audio_bytes, filename)
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
