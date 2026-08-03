#!/usr/bin/env python3
"""CLI (nhánh review): trộn track TTS + 1 file nhạc nền mức cố định xuyên suốt thành 1 file
audio final. Khác `mix_dialogue_cli.py` (đọc mẫu theo từng timestamp segment, cần numpy) —
ở đây chỉ cộng phẳng 1 track nhạc nền lặp lại suốt video nên dùng thẳng ffmpeg filter
(`amix`), không cần numpy/soundfile, giữ đúng style stdlib-only của edit_cli.py/json_to_srt.py."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_MUSIC_LEVEL = 0.15
# Trần dB tuyệt đối cho nhạc nền, ÁP DỤNG SAU `volume=music_level` (không thay thế) — bug thật
# gặp: giảm music_level tương đối vẫn có thể ra to nếu file nhạc gốc master sẵn to (loud
# chorus/drop), vì volume= chỉ nhân theo tỉ lệ, không biết trần tuyệt đối là bao nhiêu. `alimiter`
# chặn cứng peak nhạc ở mức này bất kể music_level đang để bao nhiêu.
DEFAULT_MUSIC_MAX_DB = -20.0
# TTS trước đây pass-through 100% (0dB, giữ nguyên loudness thô do TTS engine xuất ra) — đo thật
# 1 job (`Rick&morty_3b7157a8`): TTS ra -21.29 LUFS, khá nhỏ so với chuẩn thoại phổ biến
# (~-16 LUFS streaming/dialogue). Dùng `loudnorm` đưa về mức chuẩn NHẤT QUÁN giữa các job thay vì
# phụ thuộc TTS engine hôm đó xuất to/nhỏ — TP (true peak ceiling) đi kèm để không bao giờ clip dù
# job nào đó TTS gốc đã to sẵn.
DEFAULT_TTS_TARGET_LUFS = -16.0
DEFAULT_TTS_TP_DB = -1.5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mix-music CLI: TTS track + nhạc nền -> 1 track audio final (nhánh review)"
    )
    parser.add_argument("--tts", type=Path, required=True, help="Track TTS (reup-tts-gpu-node segments)")
    parser.add_argument("--music", type=Path, required=True, help="File nhạc nền (preset có sẵn hoặc người dùng upload)")
    parser.add_argument("-o", "--output", type=Path, required=True, help="File audio final (dùng làm -a cho edit_cli.py)")
    parser.add_argument(
        "--music-level", type=float, default=DEFAULT_MUSIC_LEVEL,
        help=f"Mức nhạc nền cố định xuyên suốt (0-1, mặc định {DEFAULT_MUSIC_LEVEL})",
    )
    parser.add_argument(
        "--music-max-db", type=float, default=DEFAULT_MUSIC_MAX_DB,
        help=f"Trần peak tuyệt đối cho nhạc nền sau khi nhân music-level, dBFS (mặc định {DEFAULT_MUSIC_MAX_DB})",
    )
    parser.add_argument(
        "--tts-target-lufs", type=float, default=DEFAULT_TTS_TARGET_LUFS,
        help=f"Mức loudness chuẩn cho TTS, LUFS (mặc định {DEFAULT_TTS_TARGET_LUFS})",
    )
    parser.add_argument(
        "--tts-tp-db", type=float, default=DEFAULT_TTS_TP_DB,
        help=f"Trần true-peak cho TTS sau chuẩn hoá, dBTP (mặc định {DEFAULT_TTS_TP_DB})",
    )
    return parser.parse_args()


def run_mix_music(
    tts: str, music: str, output: str,
    music_level: float = DEFAULT_MUSIC_LEVEL,
    music_max_db: float = DEFAULT_MUSIC_MAX_DB,
    tts_target_lufs: float = DEFAULT_TTS_TARGET_LUFS,
    tts_tp_db: float = DEFAULT_TTS_TP_DB,
) -> dict:
    """Trộn TTS + nhạc nền mức cố định xuyên suốt (KHÔNG duck theo speech — nhạc nền chỉ áp
    dụng cho nhánh review, vốn đã mute hoàn toàn audio gốc nên không còn tiếng nền tự nhiên
    nào khác để lo lấn TTS ngoài chính nhạc mới thêm vào; nhánh dialogue vẫn giữ instrumental
    gốc, không dùng CLI này). Nhạc lặp vô hạn (`-stream_loop -1`) nếu ngắn hơn track TTS,
    output cắt đúng theo độ dài TTS (`duration=first`). `normalize=0` bắt buộc — mặc định
    `amix` tự chia đều volume theo số input (n input -> mỗi input còn 1/n), sẽ vô tình kéo
    TTS nhỏ lại đúng thứ vừa fix ở mix_dialogue_cli.py; tắt normalize để cả 2 nhánh TTS/nhạc giữ
    đúng gain đã set qua loudnorm/volume+alimiter bên dưới, amix không tự ý chỉnh lại nữa."""
    tts_p, music_p = Path(tts), Path(music)
    for label, path in (("tts", tts_p), ("music", music_p)):
        if not path.resolve().is_file():
            raise RuntimeError(f"file {label} không tồn tại: {path}")

    output_p = Path(output).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)

    music_max_linear = 10 ** (music_max_db / 20)
    start = time.time()
    cmd = [
        "ffmpeg", "-y",
        "-i", str(tts_p),
        "-stream_loop", "-1", "-i", str(music_p),
        "-filter_complex",
        f"[0:a]loudnorm=I={tts_target_lufs}:TP={tts_tp_db}:LRA=11[tts];"
        # level=disabled BẮT BUỘC — alimiter mặc định level=enabled tự khuếch đại lại tín hiệu
        # về gần 0dB sau khi limit (auto-normalize), triệt tiêu luôn trần vừa đặt (bug thật gặp:
        # limit=0.1 (-20dB) nhưng volumedetect vẫn đo ra 0dB do auto-level bù lại).
        f"[1:a]volume={music_level},alimiter=limit={music_max_linear}:level=disabled[music];"
        f"[tts][music]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]",
        "-map", "[aout]",
        str(output_p),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise RuntimeError(f"ffmpeg thất bại:\n{tail}")

    return {
        "ok": True,
        "tts": str(tts_p.resolve()),
        "music": str(music_p.resolve()),
        "output": str(output_p),
        "music_level": music_level,
        "music_max_db": music_max_db,
        "tts_target_lufs": tts_target_lufs,
        "tts_tp_db": tts_tp_db,
        "elapsed_s": round(time.time() - start, 2),
    }


def main() -> int:
    args = parse_args()
    try:
        result = run_mix_music(
            str(args.tts), str(args.music), str(args.output), args.music_level,
            args.music_max_db, args.tts_target_lufs, args.tts_tp_db,
        )
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
