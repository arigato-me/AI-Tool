#!/usr/bin/env python3
"""CLI: 1 ảnh tĩnh -> 1 clip video giữ nguyên ảnh trong `duration` giây — dùng cho mode="mix"
khi video_items có item type="image" (xem pipeline_runner.py::_resolve_mix_item). Clip sinh ra
là input bình thường cho concat-video (chuẩn hoá lại theo item đầu tiên trong nhóm như mọi input
khác, không cần khớp resolution video khác ngay ở bước này)."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_FPS = 30


def probe_image_dims(image: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(image)],
        capture_output=True, text=True,
    )
    w, h = out.stdout.strip().split("x")
    # libx264 cần width/height chẵn — làm tròn xuống, ảnh lệch 1px không đáng kể.
    return int(w) - int(w) % 2, int(h) - int(h) % 2


def build_image_to_video_command(image: str, output: Path, w: int, h: int, duration: float, fps: int, crf: int) -> list[str]:
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}"
    return [
        "ffmpeg", "-y", "-loop", "1", "-i", image, "-t", str(duration),
        "-vf", vf, "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", "-an", str(output),
    ]


def run_image_to_video(image: str, output: str, duration: float, fps: int = DEFAULT_FPS, crf: int = 20) -> dict:
    image_p = Path(image)
    if not image_p.resolve().is_file():
        raise RuntimeError(f"file ảnh không tồn tại: {image_p}")
    if duration <= 0:
        raise RuntimeError(f"duration phải > 0 (nhận {duration})")

    w, h = probe_image_dims(image_p)
    if w <= 0 or h <= 0:
        raise RuntimeError(f"không đọc được kích thước ảnh: {image_p}")

    output_p = Path(output).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    cmd = build_image_to_video_command(str(image_p), output_p, w, h, duration, fps, crf)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise RuntimeError(f"ffmpeg image-to-video thất bại:\n{tail}")

    return {
        "ok": True, "image": str(image_p.resolve()), "output": str(output_p),
        "width": w, "height": h, "fps": fps, "duration": duration,
        "elapsed_s": round(time.time() - start, 2),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Image-to-video CLI: 1 ảnh tĩnh -> 1 clip video")
    parser.add_argument("-i", "--image", required=True)
    parser.add_argument("-o", "--output", required=True)
    parser.add_argument("-d", "--duration", type=float, required=True)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    parser.add_argument("--crf", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_image_to_video(args.image, args.output, args.duration, args.fps, args.crf)
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
