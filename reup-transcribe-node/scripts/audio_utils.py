"""ffmpeg helper dùng chung cho cả 2 backend (pipeline local và Deepgram)."""
from __future__ import annotations

import subprocess
from pathlib import Path


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
