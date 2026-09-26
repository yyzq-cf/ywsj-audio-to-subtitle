"""
ASR engines - free speech recognition using Bijian (Bilibili) and JianYing (ByteDance).
Adapted from VideoCaptioner project.
"""

import json
import time
import zlib
import hashlib
import hmac
import uuid
import logging
import io
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs

import requests
from pydub import AudioSegment

logger = logging.getLogger(__name__)


class ASRSegment:
    """One subtitle segment."""
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
    """Convert milliseconds to SRT time string HH:MM:SS,mmm"""
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    milli = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def audio_to_mp3_bytes(audio_input: bytes, filename: str = 'audio') -> bytes:
    """Convert any audio format to MP3 bytes using pydub."""
    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_input))
    except Exception:
        # Try as mp3 directly
        return audio_input
    buf = io.BytesIO()
    audio.export(buf, format='mp3', bitrate='128k')
    return buf.getvalue()


# ===== Bijian (Bilibili BCut) ASR =====

class BcutASR:
    """Bilibili BCut free ASR API."""

    API_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"

    def __init__(self, audio_bytes: bytes, filename: str = 'audio'):
        self.audio_bytes = audio_to_mp3_bytes(audio_bytes, filename)
        self.filename = filename
        self.session = requests.Session()
        self.headers = {
            "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
            "Content-Type": "application/json",
        }
        self._in_boss_key = None
        self._resource_id = None
        self._upload_id = None
        self._upload_urls = []
        self._per_size = None
        self._clips = None
        self._etags = []
        self._download_url = None
        self.task_id = None

    def _upload(self):
        """Upload audio file to Bilibili servers."""
        payload = json.dumps({
            "type": 2,
            "name": f"{self.filename}.mp3",
            "size": len(self.audio_bytes),
            "ResourceFileType": "mp3",
            "model_id": "8",
        })
        resp = self.session.post(f"{self.API_BASE}/resource/create",
                                 data=payload, headers=self.headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()["data"]

        self._in_boss_key = data["in_boss_key"]
        self._resource_id = data["resource_id"]
        self._upload_id = data["upload_id"]
        self._upload_urls = data["upload_urls"]
        self._per_size = data["per_size"]
        self._clips = len(data["upload_urls"])

        # Upload parts
        for clip in range(self._clips):
            start = clip * self._per_size
            end = (clip + 1) * self._per_size
            r = self.session.put(
                self._upload_urls[clip],
                data=self.audio_bytes[start:end],
                headers={"User-Agent": self.headers["User-Agent"]},
                timeout=60
            )
            r.raise_for_status()
            etag = r.headers.get("Etag")
            if etag:
                self._etags.append(etag)

        # Commit upload
        commit_data = json.dumps({
            "InBossKey": self._in_boss_key,
            "ResourceId": self._resource_id,
            "Etags": ",".join(self._etags) if self._etags else "",
            "UploadId": self._upload_id,
            "model_id": "8",
        })
        resp = self.session.post(f"{self.API_BASE}/resource/create/complete",
                                 data=commit_data, headers=self.headers, timeout=30)
        resp.raise_for_status()
        self._download_url = resp.json()["data"]["download_url"]

    def _create_task(self):
        """Create ASR recognition task."""
        payload = json.dumps({
            "resource": self._download_url,
            "model_id": "8",
            "filters": ["narrate"],
        })
        resp = self.session.post(f"{self.API_BASE}/task",
                                 data=payload, headers=self.headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"BCut task creation failed: {data.get('message', '')}")
        self.task_id = data["data"]["task_id"]

    def _query_result(self) -> dict:
        """Poll for ASR result."""
        for _ in range(120):
            resp = self.session.get(
                f"{self.API_BASE}/task/result",
                params={"task_id": self.task_id},
                headers=self.headers, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise Exception(f"BCut query failed: {data.get('message', '')}")
            state = data["data"].get("state", -1)
            if state == 0:  # Ready
                continue
            if state == 1:  # Running
                time.sleep(3)
                continue
            if state == 2:  # Complete
                return data["data"]
            if state == 3:  # Failed
                raise Exception("BCut recognition failed")
            time.sleep(3)
        raise Exception("BCut recognition timeout")

    def recognize(self, callback=None) -> List[ASRSegment]:
        """Run full recognition pipeline."""
        if callback:
            callback(10, '上传音频到必剪服务器...')
        self._upload()

        if callback:
            callback(40, '创建识别任务...')
        self._create_task()

        if callback:
            callback(60, '识别中...')
        result = self._query_result()

        if callback:
            callback(90, '解析结果...')
        return self._parse_result(result)

    def _parse_result(self, result: dict) -> List[ASRSegment]:
        """Parse BCut ASR result into segments."""
        segments = []
        body = result.get("body", {})
        sentences = body.get("sentences", [])

        for sent in sentences:
            text = sent.get("text", "").strip()
            start_ms = sent.get("start", 0)
            end_ms = sent.get("end", 0)
            if text:
                segments.append(ASRSegment(text, start_ms, end_ms))
        return segments


# ===== JianYing (CapCut) ASR =====

class JianYingASR:
    """JianYing (CapCut) free ASR API by ByteDance."""

    def __init__(self, audio_bytes: bytes, filename: str = 'audio'):
        self.audio_bytes = audio_to_mp3_bytes(audio_bytes, filename)
        self.filename = filename
        self.session = requests.Session()
        self.session_token = None
        self.secret_key = None
        self.access_key = None
        self.store_uri = None
        self.auth = None
        self.upload_id = None
        self.session_key = None
        self.upload_hosts = None
        self.tdid = str(uuid.uuid4())

    def _generate_sign(self, url_path: str, pf: str = "4", appvr: str = "6.6.0"):
        """Generate HMAC signature for JianYing API."""
        device_time = str(int(time.time()))
        sign_str = f"{device_time}&{self.tdid}&{pf}&{appvr}&{url_path}"
        secret = "ee6e5c4373594d66a2a5a2e5b8a5b5d7"
        sign = hmac.new(secret.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
        return sign, device_time

    def _build_headers(self, device_time: str, sign: str):
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JianYing/6.6.0",
            "Content-Type": "application/json",
            "X-Tt-Tdid": self.tdid,
            "X-Device-Time": device_time,
            "X-Sign": sign,
            "X-Argus": "",
        }

    def _upload_sign(self):
        """Get upload credentials."""
        url = "https://lv-pc-api-sinfonlinec.ulikecam.com/lv/v1/audio_subtitle/upload_sign"
        payload = {"category": 4}
        sign, device_time = self._generate_sign("/lv/v1/audio_subtitle/upload_sign")
        headers = self._build_headers(device_time, sign)
        resp = self.session.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != "0":
            raise Exception(f"JianYing upload_sign failed: {data.get('errmsg', '')}")
        d = data["data"]
        self.session_token = d.get("session_token", "")
        self.access_key = d.get("access_key", "")
        self.secret_key = d.get("secret_key", "")
        self.store_uri = d.get("store_uri", "")
        self.upload_id = d.get("upload_id", "")
        self.session_key = d.get("session_key", "")
        self.upload_hosts = d.get("upload_hosts", [])

    def _upload_file(self):
        """Upload audio file to JianYing servers."""
        if not self.upload_hosts:
            raise Exception("No upload host available")

        host = self.upload_hosts[0]
        # Use direct PUT upload
        upload_url = f"https://{host}/upload/v1/{self.store_uri}"
        headers = {
            "Authorization": self.session_token,
            "Content-Type": "audio/mpeg",
        }
        resp = self.session.put(upload_url, data=self.audio_bytes, headers=headers, timeout=120)
        resp.raise_for_status()

    def _upload_commit(self):
        """Commit upload."""
        url = "https://lv-pc-api-sinfonlinec.ulikecam.com/lv/v1/audio_subtitle/upload_commit"
        payload = {
            "store_uri": self.store_uri,
            "session_key": self.session_key,
            "upload_id": self.upload_id,
        }
        sign, device_time = self._generate_sign("/lv/v1/audio_subtitle/upload_commit")
        headers = self._build_headers(device_time, sign)
        resp = self.session.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != "0":
            raise Exception(f"JianYing upload_commit failed: {data.get('errmsg', '')}")

    def _submit(self) -> str:
        """Submit recognition task."""
        url = "https://lv-pc-api-sinfonlinec.ulikecam.com/lv/v1/audio_subtitle/submit"
        payload = {
            "adjust_endtime": 200,
            "audio": self.store_uri,
            "caption_type": 2,
            "client_request_id": str(uuid.uuid4()),
            "max_lines": 1,
            "songs_info": [{"end_time": 6000000, "id": "", "start_time": 0}],
            "words_per_line": 16,
        }
        sign, device_time = self._generate_sign("/lv/v1/audio_subtitle/submit")
        headers = self._build_headers(device_time, sign)
        resp = self.session.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ret") != "0":
            raise Exception(f"JianYing submit failed: {data.get('errmsg', '')}")
        return data["data"]["id"]

    def _query(self, query_id: str) -> dict:
        """Query recognition result."""
        for _ in range(120):
            url = "https://lv-pc-api-sinfonlinec.ulikecam.com/lv/v1/audio_subtitle/query"
            payload = {"id": query_id, "pack_options": {"need_attribute": True}}
            sign, device_time = self._generate_sign("/lv/v1/audio_subtitle/query")
            headers = self._build_headers(device_time, sign)
            resp = self.session.post(url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("ret") != "0":
                raise Exception(f"JianYing query failed: {data.get('errmsg', '')}")
            result = data.get("data", {})
            state = result.get("state", 0)
            if state == 2:  # Complete
                return result
            time.sleep(3)
        raise Exception("JianYing recognition timeout")

    def recognize(self, callback=None) -> List[ASRSegment]:
        """Run full recognition pipeline."""
        if callback:
            callback(10, '获取上传凭证...')
        self._upload_sign()

        if callback:
            callback(30, '上传音频到剪映服务器...')
        self._upload_file()
        self._upload_commit()

        if callback:
            callback(50, '提交识别任务...')
        query_id = self._submit()

        if callback:
            callback(60, '识别中...')
        result = self._query(query_id)

        if callback:
            callback(90, '解析结果...')
        return self._parse_result(result)

    def _parse_result(self, result: dict) -> List[ASRSegment]:
        """Parse JianYing ASR result into segments."""
        segments = []
        # JianYing returns result in different formats depending on version
        utterances = result.get("utterances", [])
        if not utterances:
            # Try alternative format
            body = result.get("body", {})
            utterances = body.get("utterances", [])

        for utt in utterances:
            text = utt.get("text", "").strip()
            start_ms = utt.get("start_time", 0)
            end_ms = utt.get("end_time", 0)
            if not text:
                continue
            if start_ms == 0 and end_ms == 0:
                start_ms = utt.get("start", 0)
                end_ms = utt.get("end", 0)
            if text:
                segments.append(ASRSegment(text, start_ms, end_ms))

        # If no segments from utterances, try paragraphs
        if not segments:
            paragraphs = result.get("paragraphs", [])
            for para in paragraphs:
                text = para.get("text", "").strip()
                start_ms = para.get("start_time", 0)
                end_ms = para.get("end_time", 0)
                if text:
                    segments.append(ASRSegment(text, start_ms, end_ms))

        return segments


# ===== Engine registry =====

ENGINES = {
    'bcut': {
        'label': '必剪 (B站)',
        'class': BcutASR,
        'needs_key': False,
    },
    'jianying': {
        'label': '剪映 (字节)',
        'class': JianYingASR,
        'needs_key': False,
    },
}


def recognize_audio(audio_bytes: bytes, engine: str, filename: str = 'audio',
                    callback=None) -> List[ASRSegment]:
    """Recognize audio using specified engine."""
    if engine not in ENGINES:
        raise ValueError(f"Unknown engine: {engine}")
    engine_cls = ENGINES[engine]['class']
    asr = engine_cls(audio_bytes, filename)
    return asr.recognize(callback)


# ===== Subtitle builders =====

def build_srt(segments: List[ASRSegment]) -> str:
    """Build SRT subtitle content."""
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{seg.start_str} --> {seg.end_str}")
        lines.append(seg.text)
        lines.append('')
    return '\n'.join(lines)


def build_vtt(segments: List[ASRSegment]) -> str:
    """Build VTT subtitle content."""
    lines = ['WEBVTT', '']
    for i, seg in enumerate(segments, 1):
        start = seg.start_str.replace(',', '.')
        end = seg.end_str.replace(',', '.')
        lines.append(f"{start} --> {end}")
        lines.append(seg.text)
        lines.append('')
    return '\n'.join(lines)


def build_txt(segments: List[ASRSegment]) -> str:
    """Build plain text content."""
    return '\n'.join(seg.text for seg in segments)
