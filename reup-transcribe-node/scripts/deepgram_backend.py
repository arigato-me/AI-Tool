"""Backend Deepgram — logic từ bản one-shot cũ (transcribe_cli.py), giữ làm fallback/baseline
để so sánh độ chính xác với pipeline local. Chọn qua env TRANSCRIBE_BACKEND=deepgram."""
from __future__ import annotations

import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from audio_utils import extract_audio

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


def call_deepgram(wav_bytes: bytes, api_key: str, model: str, language: str) -> dict:
    params = urllib.parse.urlencode({
        "model": model,
        "language": language,
        "punctuate": "true",
        "utterances": "true",
        "smart_format": "true",
    })
    req = urllib.request.Request(
        f"{DEEPGRAM_URL}?{params}",
        data=wav_bytes,
        method="POST",
        headers={
            "Authorization": f"Token {api_key}",
            "Content-Type": "audio/wav",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json_loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Deepgram API lỗi HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Không kết nối được Deepgram: {e.reason}") from e


def json_loads(raw: bytes) -> dict:
    import json
    return json.loads(raw.decode("utf-8"))


def build_segments(response: dict) -> list[dict]:
    utterances = response.get("results", {}).get("utterances")
    if not utterances:
        raise RuntimeError(
            "Deepgram không trả về 'utterances' — kiểm tra API key/gói dịch vụ, "
            "hoặc audio quá ngắn/im lặng."
        )
    segments = []
    for idx, utt in enumerate(utterances):
        text = (utt.get("transcript") or "").strip()
        if not text:
            continue
        segments.append({
            "id": idx,
            "start": round(float(utt["start"]), 2),
            "end": round(float(utt["end"]), 2),
            "text": text,
        })
    return segments


def transcribe(input_path: Path, api_key: str, model: str, language: str) -> dict:
    """Trả {"language","duration_s","segments","elapsed_s"} — không tự ghi file."""
    start = time.time()
    fd, tmp_wav_str = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    tmp_wav = Path(tmp_wav_str)
    try:
        extract_audio(input_path, tmp_wav)
        wav_bytes = tmp_wav.read_bytes()
        response = call_deepgram(wav_bytes, api_key, model, language)
    finally:
        tmp_wav.unlink(missing_ok=True)

    segments = build_segments(response)
    duration_s = response.get("metadata", {}).get("duration")
    if duration_s is None:
        duration_s = segments[-1]["end"] if segments else 0.0

    return {
        "language": language,
        "duration_s": duration_s,
        "segments": segments,
        "elapsed_s": round(time.time() - start, 2),
    }
