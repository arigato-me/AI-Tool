#!/usr/bin/env python3
"""CLI: N audio (hoặc video có audio — luôn lấy stream a:0) -> 1 audio nối tiếp — dùng cho
mode="mix" (xem pipeline_runner.py::_run_mix_pipeline). Luôn chọn a:0 nên nhận thẳng cả input
video (item type="reuse" trỏ vào 1 file video đã tải), khỏi cần bước tách -vn riêng."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SAMPLE_RATE = 44100


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def build_concat_audio_command(inputs: list[str], output: Path, sample_rate: int) -> list[str]:
    cmd = ["ffmpeg", "-y"]
    for inp in inputs:
        cmd += ["-i", inp]
    per_input = [
        f"[{i}:a:0]aformat=sample_rates={sample_rate}:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]"
        for i in range(len(inputs))
    ]
    concat = "".join(f"[a{i}]" for i in range(len(inputs))) + f"concat=n={len(inputs)}:v=0:a=1[outa]"
    filter_complex = ";".join(per_input + [concat])
    cmd += ["-filter_complex", filter_complex, "-map", "[outa]", "-c:a", "pcm_s16le", str(output)]
    return cmd


def run_concat_audio(inputs: list[str], output: str, sample_rate: int = DEFAULT_SAMPLE_RATE) -> dict:
    input_paths = [Path(p) for p in inputs]
    if len(input_paths) < 1:
        raise RuntimeError("concat-audio cần ít nhất 1 input")
    for p in input_paths:
        if not p.resolve().is_file():
            raise RuntimeError(f"file audio không tồn tại: {p}")

    output_p = Path(output).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    cmd = build_concat_audio_command([str(p) for p in input_paths], output_p, sample_rate)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise RuntimeError(f"ffmpeg concat-audio thất bại:\n{tail}")

    return {
        "ok": True, "inputs": [str(p.resolve()) for p in input_paths], "output": str(output_p),
        "sample_rate": sample_rate, "duration_s": probe_duration(output_p),
        "elapsed_s": round(time.time() - start, 2),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Concat-audio CLI: nối N audio (hoặc video, luôn lấy a:0)")
    parser.add_argument("-i", "--input", dest="inputs", action="append", required=True, help="Audio/video input (lặp lại -i cho nhiều file)")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_concat_audio(args.inputs, args.output, args.sample_rate)
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
