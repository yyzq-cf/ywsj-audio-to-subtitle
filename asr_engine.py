"""
ASR engines - using VideoCaptioner package for JianYing (ByteDance) ASR.
BCut (Bilibili) may be blocked by regional WAF.
"""

import logging
import io
from typing import List

from pydub import AudioSegment

logger = logging.getLogger(__name__)


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
    """Convert any audio to WAV bytes."""
    try:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))
    except Exception:
        return audio_bytes
    buf = io.BytesIO()
    audio.export(buf, format='wav')
    return buf.getvalue()


def recognize_jianying(audio_bytes: bytes, callback=None) -> List[ASRSegment]:
    """Recognize using JianYing (ByteDance) ASR via VideoCaptioner."""
    import tempfile, os
    from videocaptioner.core.asr.jianying import JianYingASR

    # Write to temp file
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        f.write(audio_to_wav_bytes(audio_bytes))
        tmp_path = f.name

    try:
        if callback:
            callback(10, '上传音频到剪映服务器...')

        asr = JianYingASR(tmp_path, use_cache=False)
        result = asr.run()

        if callback:
            callback(90, '解析结果...')

        segments = []
        for seg in result.segments:
            text = seg.text.strip()
            if text:
                segments.append(ASRSegment(text, int(seg.start_time), int(seg.end_time)))
        return segments
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass


def recognize_bcut(audio_bytes: bytes, callback=None) -> List[ASRSegment]:
    """Recognize using BCut (Bilibili) ASR via VideoCaptioner."""
    import tempfile, os
    from videocaptioner.core.asr.bcut import BcutASR

    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        f.write(audio_to_wav_bytes(audio_bytes))
        tmp_path = f.name

    try:
        if callback:
            callback(10, '上传音频到必剪服务器...')

        asr = BcutASR(tmp_path, use_cache=False)
        result = asr.run()

        if callback:
            callback(90, '解析结果...')

        segments = []
        for seg in result.segments:
            text = seg.text.strip()
            if text:
                segments.append(ASRSegment(text, int(seg.start_time), int(seg.end_time)))
        return segments
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass


ENGINES = {
    'jianying': {
        'label': '剪映 (字节)',
        'needs_key': False,
    },
    'bcut': {
        'label': '必剪 (B站)',
        'needs_key': False,
    },
}

_func_map = {
    'jianying': recognize_jianying,
    'bcut': recognize_bcut,
}


def recognize_audio(audio_bytes: bytes, engine: str, filename: str = 'audio',
                    callback=None) -> List[ASRSegment]:
    if engine not in _func_map:
        raise ValueError(f"Unknown engine: {engine}")
    return _func_map[engine](audio_bytes, callback)


def build_srt(segments):
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{seg.start_str} --> {seg.end_str}")
        lines.append(seg.text)
        lines.append('')
    return '\n'.join(lines)


def build_vtt(segments):
    lines = ['WEBVTT', '']
    for i, seg in enumerate(segments, 1):
        start = seg.start_str.replace(',', '.')
        end = seg.end_str.replace(',', '.')
        lines.append(f"{start} --> {end}")
        lines.append(seg.text)
        lines.append('')
    return '\n'.join(lines)


def build_txt(segments):
    return '\n'.join(seg.text for seg in segments)
