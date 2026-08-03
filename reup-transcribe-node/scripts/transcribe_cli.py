#!/usr/bin/env python3
"""CLI: video/audio -> trích audio bằng ffmpeg -> gọi Deepgram -> transcript JSON có timestamp."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transcribe CLI: file video/audio -> transcript JSON (Deepgram)"
    )
    parser.add_argument("-i", "--input", type=Path, required=True, help="File video/audio đầu vào")
    parser.add_argument("-o", "--output", type=Path, required=True, help="File JSON transcript đầu ra")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("DEEPGRAM_API_KEY"),
        help="Deepgram API key (mặc định: env DEEPGRAM_API_KEY)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEPGRAM_MODEL", "nova-2"),
        help="Deepgram model (mặc định: env DEEPGRAM_MODEL hoặc nova-2)",
    )
    parser.add_argument(
        "--language",
        default=os.environ.get("DEEPGRAM_LANGUAGE", "vi"),
        help="Ngôn ngữ audio gốc (mặc định: env DEEPGRAM_LANGUAGE hoặc vi)",
    )
    return parser.parse_args()


def extract_audio(input_path: Path, tmp_wav: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(tmp_wav),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg trích audio thất bại:\n{tail}")


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
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Deepgram API lỗi HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Không kết nối được Deepgram: {e.reason}") from e


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


def main() -> int:
    args = parse_args()

    if not args.api_key:
        print("Lỗi: thiếu Deepgram API key (--api-key hoặc env DEEPGRAM_API_KEY)", file=sys.stderr)
        return 1

    input_path = args.input.resolve()
    if not input_path.is_file():
        print(f"Lỗi: file input không tồn tại: {input_path}", file=sys.stderr)
        return 1

    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    fd, tmp_wav_str = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    tmp_wav = Path(tmp_wav_str)
    try:
        extract_audio(input_path, tmp_wav)
        wav_bytes = tmp_wav.read_bytes()
        response = call_deepgram(wav_bytes, args.api_key, args.model, args.language)
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    finally:
        tmp_wav.unlink(missing_ok=True)

    try:
        segments = build_segments(response)
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1

    duration_s = response.get("metadata", {}).get("duration")
    if duration_s is None:
        duration_s = segments[-1]["end"] if segments else 0.0

    output_path.write_text(
        json.dumps({
            "language": args.language,
            "duration_s": duration_s,
            "segments": segments,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result = {
        "ok": True,
        "input": str(input_path),
        "output": str(output_path),
        "segments": len(segments),
        "duration_s": duration_s,
        "elapsed_s": round(time.time() - start, 2),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
