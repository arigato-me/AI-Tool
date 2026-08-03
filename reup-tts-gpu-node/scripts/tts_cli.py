#!/usr/bin/env python3
"""CLI: đọc file text → tổng hợp giọng nói → lưu file wav."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from vieneu import Vieneu

STYLES = ("tu_nhien", "tin_tuc", "doc_truyen")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VieNeu-TTS CLI: file text → file wav"
    )
    parser.add_argument(
        "-i", "--input",
        type=Path,
        help="File text đầu vào (UTF-8)",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        help="File wav đầu ra",
    )
    parser.add_argument(
        "--voice",
        default=os.environ.get("VIENEU_VOICE"),
        help="Tên built-in voice (mặc định: env VIENEU_VOICE hoặc voice đầu tiên)",
    )
    parser.add_argument(
        "--style",
        default=os.environ.get("VIENEU_STYLE", "tu_nhien"),
        choices=STYLES,
        help="Kiểu đọc: tu_nhien | tin_tuc | doc_truyen",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="In danh sách built-in voices rồi thoát",
    )
    parser.add_argument(
        "--ref-audio",
        type=Path,
        default=None,
        help="File audio mẫu (3-5s) để clone giọng — bỏ qua --voice khi được set",
    )
    return parser.parse_args()


def resolve_voice(tts: Vieneu, voice: str | None) -> str:
    voices = tts.list_preset_voices()
    if not voices:
        raise RuntimeError("Không có built-in voice nào.")

    if voice:
        for label, voice_id in voices:
            if voice in (label, voice_id):
                return voice_id
        available = ", ".join(f"{label} ({vid})" for label, vid in voices)
        raise ValueError(f"Voice '{voice}' không tồn tại. Có sẵn: {available}")

    return voices[0][1]


def list_voices_dict(tts: Vieneu | None = None) -> dict:
    tts_engine = tts if tts is not None else Vieneu()
    voices = tts_engine.list_preset_voices()
    return {"ok": True, "count": len(voices), "voices": [{"label": l, "id": v} for l, v in voices]}


def run_tts(
    input_path: str, output_path: str,
    voice: str | None = None, style: str = "tu_nhien",
    tts: Vieneu | None = None, ref_audio: str | None = None,
) -> dict:
    """1 file text -> 1 file wav. Tách khỏi main()/argparse để CLI và worker dùng chung —
    `tts` cho phép worker truyền vào 1 instance Vieneu đã load sẵn (giữ resident xuyên suốt
    nhiều job) thay vì tạo mới mỗi lần gọi (CLI một-lần vẫn tự tạo nếu không truyền).
    `ref_audio` (file mẫu 3-5s) bật clone giọng — engine v3turbo hỗ trợ sẵn qua
    `infer(text, ref_audio=...)`, bỏ qua hẳn `resolve_voice()`/preset khi được set."""
    input_p = Path(input_path).resolve()
    output_p = Path(output_path).resolve()

    if not input_p.is_file():
        raise RuntimeError(f"file input không tồn tại: {input_p}")

    text = input_p.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"file input rỗng: {input_p}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    tts_engine = tts if tts is not None else Vieneu()
    if ref_audio:
        ref_p = Path(ref_audio).resolve()
        if not ref_p.is_file():
            raise RuntimeError(f"file ref_audio không tồn tại: {ref_p}")
        voice_id = f"clone:{ref_p.name}"
        audio = tts_engine.infer(text, ref_audio=str(ref_p), style=style)
    else:
        voice_id = resolve_voice(tts_engine, voice)
        audio = tts_engine.infer(text, voice=voice_id, style=style)
    tts_engine.save(audio, str(output_p))
    synthesis_time_s = round(time.time() - start, 2)
    audio_duration_s = round(len(audio) / tts_engine.sample_rate, 2)

    return {
        "ok": True,
        "input": str(input_p),
        "output": str(output_p),
        "voice": voice_id,
        "style": style,
        "synthesis_time_s": synthesis_time_s,
        "audio_duration_s": audio_duration_s,
        "text_chars": len(text),
    }


def main() -> int:
    args = parse_args()
    if args.list_voices:
        result = list_voices_dict()
        for v in result["voices"]:
            print(f"{v['label']}\t{v['id']}")
        print(json.dumps({"ok": True, "count": result["count"]}, ensure_ascii=False))
        return 0

    if not args.input or not args.output:
        print("Lỗi: cần --input/-i và --output/-o", file=sys.stderr)
        return 1
    try:
        result = run_tts(str(args.input), str(args.output), args.voice, args.style, ref_audio=str(args.ref_audio) if args.ref_audio else None)
    except (RuntimeError, ValueError) as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
