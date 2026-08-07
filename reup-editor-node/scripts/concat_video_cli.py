#!/usr/bin/env python3
"""CLI: N video (khác resolution/fps/codec tuỳ nguồn) -> 1 video nối tiếp, đã chuẩn hoá
(scale/pad/fps theo video ĐẦU) — dùng cho mode="mix" (xem pipeline_runner.py::_run_mix_pipeline).
Không map audio (bước mux cuối trong nhánh mix đọc audio từ 1 file concat-audio riêng)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from edit_cli import probe_video_dims

DEFAULT_FPS = 30


def probe_fps(video: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True,
    )
    raw = out.stdout.strip()
    try:
        num, den = raw.split("/")
        fps = round(int(num) / int(den))
        return fps if fps > 0 else DEFAULT_FPS
    except (ValueError, ZeroDivisionError):
        return DEFAULT_FPS


def build_concat_video_command(inputs: list[str], output: Path, w: int, h: int, fps: int, crf: int) -> list[str]:
    cmd = ["ffmpeg", "-y"]
    for inp in inputs:
        cmd += ["-i", inp]
    per_input = [
        f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]"
        for i in range(len(inputs))
    ]
    concat = "".join(f"[v{i}]" for i in range(len(inputs))) + f"concat=n={len(inputs)}:v=1:a=0[outv]"
    filter_complex = ";".join(per_input + [concat])
    cmd += ["-filter_complex", filter_complex, "-map", "[outv]",
            "-c:v", "libx264", "-crf", str(crf), "-preset", "medium", "-an", str(output)]
    return cmd


def run_concat_video(inputs: list[str], output: str, fps: int | None = None, crf: int = 20) -> dict:
    input_paths = [Path(p) for p in inputs]
    if len(input_paths) < 1:
        raise RuntimeError("concat-video cần ít nhất 1 input")
    for p in input_paths:
        if not p.resolve().is_file():
            raise RuntimeError(f"file video không tồn tại: {p}")

    w, h = probe_video_dims(input_paths[0])
    fps = fps or probe_fps(input_paths[0])

    output_p = Path(output).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    cmd = build_concat_video_command([str(p) for p in input_paths], output_p, w, h, fps, crf)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise RuntimeError(f"ffmpeg concat-video thất bại:\n{tail}")

    return {
        "ok": True, "inputs": [str(p.resolve()) for p in input_paths], "output": str(output_p),
        "width": w, "height": h, "fps": fps, "elapsed_s": round(time.time() - start, 2),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Concat-video CLI: nối N video (chuẩn hoá theo video đầu)")
    parser.add_argument("-i", "--input", dest="inputs", action="append", required=True, help="Video input (lặp lại -i cho nhiều video)")
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("--fps", type=int, default=None)
    parser.add_argument("--crf", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_concat_video(args.inputs, args.output, args.fps, args.crf)
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
