"""Tier 1 (MarkItDown/PyMuPDF, CPU, không đụng GPU) + Tier 2 (OCR ảnh/trang scan, GPU qua
`ocr_fn` — xem worker.py) — gộp 1 file input (pdf/docx/pptx/xlsx/ảnh) thành `segments` dạng
transcript JSON GIỐNG HỆT schema reup-transcribe-node (`{language, duration_s, segments:
[{id,start,end,text}]}`) để reup-translate-node/reup-tts-gpu-node dùng thẳng, không cần sửa 2
node đó (xem CLAUDE.md "Reup pipeline" nhánh sách).

`start`/`end` ở đây CHỈ là placeholder cấu trúc, KHÔNG mô phỏng tốc độ đọc thật — run_translate()
(reup-translate-node) bắt buộc mọi segment phải có 2 field numeric này (KeyError nếu thiếu), dùng
ở `call_deepseek()` (gợi ý pacing, ảnh hưởng nhẹ) và `split_segment_by_sentences()` (chia lại
theo câu, phân bổ tỷ lệ ký tự). Cố tình KHÔNG dùng để đồng bộ audio thật như nhánh video (đoạn
văn ở đây vốn đã trọn vẹn theo trang/đoạn, không có khái niệm "bị VAD cắt ngang giữa câu").

QUAN TRỌNG: `run_translate(tag_speakers_enabled=True)` (nhánh sách) SKIP hẳn
`merge_dangling_sentence_segments()` — heuristic đó gộp segment dựa vào ngưỡng
`end-start >= 18.5s`, vốn để vá lỗi VAD cắt audio thật; áp nhầm lên timestamp giả ở đây sẽ gộp
2 đoạn văn không liên quan chỉ vì đoạn trước dài hơn ~259 ký tự (259/CHARS_PER_SECOND). KHÔNG
được gọi run_translate() cho nhánh sách mà thiếu `tag_speakers_enabled=True`."""
from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Callable

# (image_path, lang) -> [{"text","confidence","bbox"}, ...] — worker.py truyền vào bản có giữ
# khoá GPU quanh đúng lệnh gọi; ocr_cli.py (dev/test) truyền bản KHÔNG khoá (chạy standalone,
# không có Redis).
OcrFn = Callable[[str, str], list[dict]]

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
MARKITDOWN_EXTS = {".docx", ".pptx", ".xlsx"}
CHARS_PER_SECOND = 14.0  # ước lượng tốc độ đọc — chỉ dùng làm gợi ý độ dài câu, không phải audio thật
MIN_SEGMENT_DURATION_S = 1.0
SCAN_CHAR_THRESHOLD = 20  # < ký tự trên trang PDF => nghi trang scan/ảnh, không tin text layer
RENDER_ZOOM = 2.0  # zoom lúc render trang PDF ra ảnh (~144 DPI so với 72 DPI mặc định PyMuPDF)
# — đủ nét cho OCR mà không quá nặng VRAM/thời gian.


def _split_paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return paras or ([text.strip()] if text.strip() else [])


def _extract_docx_like(path: Path) -> list[str]:
    """docx/pptx/xlsx qua MarkItDown — KHÔNG cài `markitdown[all]`/`markitdown-ocr` (dùng LLM
    Vision cloud, trái nguyên tắc OCR 100% local của node này). `convert_local()` (không phải
    `convert()`) để khoá cứng chỉ đọc file local, không tự fetch URI/network."""
    from markitdown import MarkItDown

    result = MarkItDown().convert_local(str(path))
    return _split_paragraphs(result.text_content)


def _extract_pdf(path: Path, source_lang: str, ocr_fn: OcrFn) -> list[str]:
    import fitz  # PyMuPDF

    paragraphs: list[str] = []
    doc = fitz.open(str(path))
    try:
        for page in doc:
            text_layer = page.get_text().strip()
            if len(text_layer) >= SCAN_CHAR_THRESHOLD:
                # get_text("blocks") thay vì get_text() trơn + _split_paragraphs(): get_text()
                # trơn chèn "\n" ở MỌI dòng hiển thị (kể cả dòng bị word-wrap giữa 1 đoạn văn dài,
                # PDF không có khái niệm đoạn văn) — không phân biệt được "xuống dòng do tràn khổ
                # trang" với "đoạn văn/dòng thoại mới", khiến nhiều dòng KHÔNG liên quan bị gộp
                # thành 1 "segment" nếu giữa chúng không có blank-line rõ ràng. Bug thật gặp lúc
                # test truyện có thoại: dịch báo "Deepseek trả về 5 câu, cần 1" (model tự tách 5
                # dòng bị gộp làm 1 input) + gán vai chỉ ra được 1 speaker cho cả narrator lẫn 2
                # nhân vật thoại. get_text("blocks") nhóm ĐÚNG theo block layout PyMuPDF tự phát
                # hiện (đoạn văn/dòng thoại riêng biệt), các dòng word-wrap TRONG CÙNG 1 block
                # được nối lại bằng khoảng trắng thay vì giữ "\n" thô.
                for block in page.get_text("blocks"):
                    block_text = block[4].strip()
                    if not block_text:
                        continue
                    joined = " ".join(line.strip() for line in block_text.splitlines() if line.strip())
                    if joined:
                        paragraphs.append(joined)
                continue
            # Trang gần như trống chữ (scan thuần hoặc ảnh) — render ra ảnh rồi OCR nguyên trang.
            pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM))
            with tempfile.NamedTemporaryFile(suffix=".png", delete=True) as tmp:
                pix.save(tmp.name)
                lines = ocr_fn(tmp.name, source_lang)
            page_text = " ".join(line["text"] for line in lines if line.get("text", "").strip())
            if page_text.strip():
                paragraphs.append(page_text.strip())
    finally:
        doc.close()
    return paragraphs


def _extract_image(path: Path, source_lang: str, ocr_fn: OcrFn) -> list[str]:
    """Nối MỌI dòng OCR detect được trên ảnh thành 1 đoạn văn duy nhất (khoảng trắng nối dòng) —
    KHÔNG trả 1 segment/dòng như trước đây.

    Bug thật gặp lúc test job 2b3ad20e (ảnh chụp 1 trang sách dày chữ): mỗi dòng OCR (ranh giới
    layout in ấn — dòng bị ngắt vì tràn khổ trang, KHÔNG phải ranh giới câu) từng thành 1
    "segment" riêng, dịch từng dòng độc lập ra bản dịch cụt lủng, không kết thúc bằng dấu câu
    (câu thật bị cắt làm 2-3 "segment" không liên quan). Nối lại thành 1 khối văn bản liền mạch,
    để `split_segment_by_sentences()` (translate_cli.py, chạy SAU khi dịch) tự tách lại đúng
    theo ranh giới câu thật (`.`/`!`/`?`/`…`) — cùng cách `_extract_pdf()` xử lý trang scan ở
    trên, đúng lý do người dùng muốn output nhánh sách giống nhánh video (segment luôn kết thúc
    bằng dấu câu)."""
    lines = ocr_fn(str(path), source_lang)
    page_text = " ".join(line["text"].strip() for line in lines if line.get("text", "").strip())
    return [page_text] if page_text.strip() else []


def extract_paragraphs(input_path: str, source_lang: str, ocr_fn: OcrFn) -> list[str]:
    path = Path(input_path)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _extract_pdf(path, source_lang, ocr_fn)
    if ext in MARKITDOWN_EXTS:
        return _extract_docx_like(path)
    if ext in IMAGE_EXTS:
        return _extract_image(path, source_lang, ocr_fn)
    raise RuntimeError(f"định dạng không hỗ trợ: {ext!r} (chỉ nhận .pdf/.docx/.pptx/.xlsx/ảnh)")


def _build_segments(paragraphs: list[str]) -> tuple[list[dict], float]:
    segments = []
    t = 0.0
    for i, text in enumerate(paragraphs, start=1):
        dur = max(MIN_SEGMENT_DURATION_S, len(text) / CHARS_PER_SECOND)
        segments.append({"id": i, "start": round(t, 2), "end": round(t + dur, 2), "text": text})
        t += dur
    return segments, round(t, 2)


def run_extract(
    input_path: str, output_path: str, source_lang: str, ocr_fn: OcrFn,
    pipeline_id: str | None = None, video_name: str | None = None,
) -> dict:
    start = time.time()
    input_p = Path(input_path).resolve()
    if not input_p.is_file():
        raise RuntimeError(f"file input không tồn tại: {input_p}")

    paragraphs = extract_paragraphs(str(input_p), source_lang, ocr_fn)
    if not paragraphs:
        raise RuntimeError(
            f"không trích được đoạn văn bản nào từ {input_p} — file rỗng, không đọc được, "
            f"hoặc OCR không nhận ra chữ nào"
        )

    segments, duration_s = _build_segments(paragraphs)

    output_p = Path(output_path).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)
    output_p.write_text(
        json.dumps({"language": source_lang, "duration_s": duration_s, "segments": segments},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "input": str(input_p),
        "output": str(output_p),
        "segments": len(segments),
        "duration_s": duration_s,
        "elapsed_s": round(time.time() - start, 2),
    }
