"""
ASR engines - standalone implementation using free cloud ASR services.

Engines:
  - bcut (B站必剪)  : 免费，无第三方依赖，调用B站官方API
  - OpenAI-compatible: 硅基流动/Groq/OpenAI Whisper接口(用户自定义)
"""

import datetime
import json
import os
import time
import logging
import io
import tempfile
from pathlib import Path
from typing import List, Optional, Dict, Any, Union

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
        # 统一转小写，避免大写扩展名(如.MP3)被API拒绝
        lower_name = self.filename.lower()
        video_exts = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.flv', '.ts', '.m4v', '.wmv')
        if lower_name.endswith(video_exts):
            # 视频已提取为WAV
            send_filename = 'audio.wav'
            send_content_type = 'audio/wav'
        elif lower_name.endswith('.mp3'):
            send_filename = lower_name
            send_content_type = 'audio/mp3'
        elif lower_name.endswith('.wav'):
            send_filename = lower_name
            send_content_type = 'audio/wav'
        elif lower_name.endswith('.m4a'):
            send_filename = lower_name
            send_content_type = 'audio/mp4'
        elif lower_name.endswith('.ogg'):
            send_filename = lower_name
            send_content_type = 'audio/ogg'
        elif lower_name.endswith('.flac'):
            send_filename = lower_name
            send_content_type = 'audio/flac'
        elif lower_name.endswith('.opus'):
            send_filename = lower_name
            send_content_type = 'audio/opus'
        elif lower_name.endswith('.mpeg') or lower_name.endswith('.mpga'):
            send_filename = lower_name
            send_content_type = 'audio/mpeg'
        else:
            send_filename = lower_name or 'audio.mp3'
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
