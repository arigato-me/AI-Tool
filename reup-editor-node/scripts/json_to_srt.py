#!/usr/bin/env python3
"""CLI: transcript JSON (segments start/end/text) -> file .srt."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Ngưỡng cảnh báo (không chặn) — segment quá dài từng gây mov_text (soft sub) lỗi/cắt
# video sớm (xem docker_transcribe MAX_SEGMENT_SECONDS, chặn gốc ở VAD). Giữ cảnh báo ở
# đây làm lưới an toàn thứ 2: nếu vẫn lọt qua (vd input không qua docker_transcribe, hoặc
# field --field text_original dài hơn dự kiến), báo ngay ra stderr thay vì âm thầm sinh ra
# .srt lỗi mất 30 phút mới phát hiện được. Đây là mức cảnh báo trên TOÀN BỘ segment gốc,
# không phải trên từng cue hiển thị — 1 segment dài vẫn được tách thành nhiều cue ngắn ở
# bên dưới (MAX_LINE_CHARS) nên bản thân việc "dài" không còn tự động = lỗi hiển thị, chỉ
# còn là tín hiệu VAD/dịch có thể đang gộp quá nhiều câu vào 1 segment.
MAX_CUE_CHARS = 400
MAX_CUE_SECONDS = 25.0

# Mỗi segment transcript (1 cụm VAD-speech) có thể chứa nhiều câu — burn nguyên khối lên
# màn hình sẽ tràn nhiều dòng, không giống phụ đề video ngắn thông thường (1 dòng, nhảy
# theo câu). Tách text thành các dòng ngắn, chia đều thời lượng segment theo tỉ lệ số ký
# tự mỗi dòng (dòng dài hơn hiện lâu hơn — xấp xỉ tốc độ nói đều).
MAX_LINE_CHARS = 42


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="JSON transcript -> SRT")
    parser.add_argument("-i", "--input", type=Path, required=True, help="File JSON transcript đầu vào")
    parser.add_argument("-o", "--output", type=Path, required=True, help="File .srt đầu ra")
    parser.add_argument(
        "--field",
        default="text",
        help="Field text dùng làm phụ đề (mặc định: text; dùng text_original nếu muốn sub gốc)",
    )
    parser.add_argument(
        "--max-line-chars",
        type=int,
        default=MAX_LINE_CHARS,
        help=f"Số ký tự tối đa/dòng phụ đề trước khi tách sang cue kế tiếp (mặc định {MAX_LINE_CHARS})",
    )
    parser.add_argument(
        "--windows", type=Path, default=None,
        help="File *_windows.json (segments_cli.py) — vị trí TTS THẬT sau cascading placement, "
             "dùng để sinh sub KHỚP audio thay vì timestamp gốc (tuỳ chọn, không có vẫn chạy được)",
    )
    return parser.parse_args()


CLAUSE_SPLIT_RE = re.compile(r"(?<=[,;])\s+")


def _word_wrap(text: str, max_chars: int) -> list[str]:
    """Word-wrap thuần theo ký tự (không cắt giữa từ) — fallback cuối khi 1 mệnh đề vẫn
    dài hơn max_chars mà không còn dấu phẩy nào để ngắt."""
    words = text.split()
    if not words:
        return []
    result = [words[0]]
    for word in words[1:]:
        candidate = f"{result[-1]} {word}"
        if len(candidate) <= max_chars:
            result[-1] = candidate
        else:
            result.append(word)
    return result


def split_into_lines(text: str, max_chars: int) -> list[str]:
    """Chia text thành các dòng <= max_chars. 1 segment giờ thường đã = 1 câu (tách ở
    translate_cli.py theo dấu . ! ?), nhưng 1 câu đơn lẻ vẫn có thể dài hơn max_chars —
    ưu tiên ngắt tại ranh giới dấu phẩy/chấm phẩy (giữ trọn mệnh đề có nghĩa) trước khi
    word-wrap thuần theo ký tự, tránh cắt giữa mệnh đề kiểu "...gặp" / "nhau...".
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    clauses = [c.strip() for c in CLAUSE_SPLIT_RE.split(text) if c.strip()]
    lines: list[str] = []
    for clause in clauses:
        if not lines:
            lines.append(clause)
            continue
        candidate = f"{lines[-1]} {clause}"
        if len(candidate) <= max_chars:
            lines[-1] = candidate
        else:
            lines.append(clause)

    final_lines: list[str] = []
    for line in lines:
        if len(line) <= max_chars:
            final_lines.append(line)
        else:
            final_lines.extend(_word_wrap(line, max_chars))
    return final_lines


def distribute_cues(start: float, end: float, segment_lines: list[str]) -> list[tuple[float, float, str]]:
    """Chia thời lượng [start, end] của 1 segment cho từng dòng theo tỉ lệ độ dài ký tự —
    dòng dài hơn hiện lâu hơn, xấp xỉ tốc độ nói đều trong cùng 1 segment."""
    if not segment_lines:
        return []
    total_chars = sum(len(line) for line in segment_lines) or 1
    cues = []
    t = start
    for i, line in enumerate(segment_lines):
        if i == len(segment_lines) - 1:
            t_end = end
        else:
            share = len(line) / total_chars
            t_end = min(end, t + (end - start) * share)
        cues.append((t, t_end, line))
        t = t_end
    return cues


def format_timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def load_srt_timings(segments: list[dict], windows_path: "Path | None") -> dict[int, tuple[float, float]]:
    """id -> (start, end) dùng để sinh SRT — mặc định lấy thẳng từ transcript (như trước giờ),
    NHƯNG ưu tiên vị trí TTS THẬT trong *_windows.json (segments_cli.py) nếu có. Phát hiện thật:
    khi TTS bị "cascading placement" đẩy lùi 1 đoạn dài (đã cố tránh audio đè lên nhau, xem
    reup-tts-gpu-node/scripts/segments_cli.py), sub vẫn hiện đúng giờ GỐC trong khi audio đã trễ
    tới vài giây (video Douyin test 2026-08-01: lệch 3.92s ở cuối video, 174/210 segment trôi) —
    cùng lớp bug đã fix cho mix-dialogue (load_mix_windows() bên mix_dialogue_cli.py) nhưng chưa
    từng áp dụng cho SRT. windows.json thiếu/lỗi/không truyền vào = fallback y hệt hành vi cũ
    (subtitle-mode không có TTS nên không bao giờ có file này, không đổi gì)."""
    timings = {s["id"]: (s["start"], s["end"]) for s in segments if "id" in s}
    if windows_path is None or not windows_path.is_file():
        return timings
    try:
        windows_data = json.loads(windows_path.read_text(encoding="utf-8"))
        for w in windows_data.get("segments", []):
            if isinstance(w, dict) and "id" in w and "start" in w and "end" in w:
                timings[w["id"]] = (w["start"], w["end"])
    except (json.JSONDecodeError, OSError):
        pass
    return timings


def run_srt(
    input_path: str, output_path: str, field: str = "text", max_line_chars: int = MAX_LINE_CHARS,
    windows: str | None = None,
) -> dict:
    """Transcript JSON -> file .srt (tách dòng ngắn, chia đều thời lượng). Tách khỏi
    main()/argparse để CLI và worker dùng chung — logic tách dòng/cue giữ nguyên 100%."""
    input_p = Path(input_path).resolve()
    if not input_p.is_file():
        raise RuntimeError(f"file input không tồn tại: {input_p}")

    data = json.loads(input_p.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    if not segments:
        raise RuntimeError(f"file input không có 'segments': {input_p}")

    windows_p = Path(windows).resolve() if windows else None
    timings = load_srt_timings(segments, windows_p)

    out_lines = []
    oversized = 0
    cue_no = 0
    for i, seg in enumerate(segments, start=1):
        text = seg.get(field) or seg.get("text", "")
        seg_start, seg_end = timings.get(seg.get("id"), (seg["start"], seg["end"]))
        duration = seg_end - seg_start
        if len(text) > MAX_CUE_CHARS or duration > MAX_CUE_SECONDS:
            oversized += 1
            print(
                f"Cảnh báo: segment #{i} [{seg_start:.2f}-{seg_end:.2f}] dài {duration:.1f}s, "
                f"{len(text)} ký tự — vượt ngưỡng an toàn ({MAX_CUE_SECONDS}s/{MAX_CUE_CHARS} ký tự). "
                f"Nghi VAD/dịch đang gộp quá nhiều câu vào 1 segment.",
                file=sys.stderr,
            )

        seg_lines = split_into_lines(text, max_line_chars)
        for cue_start, cue_end, line in distribute_cues(seg_start, seg_end, seg_lines):
            cue_no += 1
            out_lines.append(str(cue_no))
            out_lines.append(f"{format_timestamp(cue_start)} --> {format_timestamp(cue_end)}")
            out_lines.append(line)
            out_lines.append("")

    output_p = Path(output_path).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)
    output_p.write_text("\n".join(out_lines), encoding="utf-8")

    return {
        "ok": True,
        "input": str(input_p),
        "output": str(output_p),
        "segments": len(segments),
        "cues": cue_no,
        "oversized_segments": oversized,
    }


def main() -> int:
    args = parse_args()
    try:
        result = run_srt(str(args.input), str(args.output), args.field, args.max_line_chars,
                          str(args.windows) if args.windows else None)
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
