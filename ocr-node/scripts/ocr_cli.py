#!/usr/bin/env python3
"""CLI dev/test: 1 file (pdf/docx/pptx/xlsx/ảnh) -> transcript JSON (segments timestamp giả).
Runtime THẬT là worker.py tiêu thụ job queue (xem entrypoint.sh `worker`) — CLI này chỉ để test
tay 1 file không cần Redis/queue, đúng vai trò "entrypoint test/dev" của mọi node khác trong
repo (yt-dlp/translate/transcribe đều có `cli` tương tự)."""
from __future__ import annotations

import argparse
import json
import os
import sys

from document_extractor import run_extract
from ocr_engine import OCREngine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OCR/extract CLI (dev/test) — không dùng khoá GPU chung")
    parser.add_argument("-i", "--input", required=True, help="File pdf/docx/pptx/xlsx/ảnh")
    parser.add_argument("-o", "--output", required=True, help="File JSON output")
    parser.add_argument("--source-lang", default="vi", choices=["vi", "en", "fr"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    engine = OCREngine(
        lang=args.source_lang,
        vietocr_model_name=os.environ.get("VIETOCR_MODEL_NAME", "vgg_transformer"),
        vietocr_device=os.environ.get("VIETOCR_DEVICE", "cuda:0"),
    )

    def ocr_page(image_path: str, lang: str) -> list[dict]:
        return engine.run(image_path, lang=lang)

    try:
        result = run_extract(args.input, args.output, args.source_lang, ocr_page)
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
