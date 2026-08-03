#!/usr/bin/env python3
"""CLI: video gốc + audio TTS mới (+ sub tùy chọn) -> video final (FFmpeg)."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Font để burn sub + đo chữ (Pillow) phải là CÙNG 1 font, nếu không độ rộng đo được sẽ lệch
# với độ rộng libass thực render. DejaVu Sans là font duy nhất có sẵn trong image (xem
# Dockerfile — không cài thêm gói font nào); panel chỉnh style chỉ bật/tắt Bold (không đổi
# họ font) nên cần cả 2 file Regular/Bold để đo chữ đúng theo đúng weight đang chọn — cả 2
# đều có sẵn trong `fonts-dejavu-core` (đã xác nhận trên container thật).
SUB_FONT_PATH_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SUB_FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SUB_FONT_NAME = "DejaVu Sans"


def _sub_font_path(bold: bool) -> str:
    return SUB_FONT_PATH_BOLD if bold else SUB_FONT_PATH_REGULAR


SUB_FONT_SIZE_RATIO = 0.045  # tỉ lệ theo chiều cao video
# Lề quanh chữ trong nền — nhỏ, chỉ đủ "thở" quanh chữ, không phải khung đệm lớn (từng để
# 20/14 kết hợp cách tính box_h qua tỉ lệ dòng ước lượng khiến nền trông thừa hẳn so với chữ).
SUB_BOX_PAD_X = 10
SUB_BOX_PAD_Y = 6
# Test frame thật (video Douyin/TikTok có caption Trung hardcode sẵn) lộ ra: MarginV=30 (bám sát
# đáy) đặt sub Việt ĐÈ LÊN đúng vùng caption Trung có sẵn — bản cũ (dải đen full-footer ih*0.12,
# ~130px ở video 1080p) từng che khuất được cả 2 nên không lộ, giờ box nhỏ lại mới thấy chồng
# chữ thật. Tỉ lệ 0.14 (thay vì px cố định) để đặt sub Việt luôn nằm TRÊN, cách rõ vùng ih*0.12
# đó — không có toạ độ caption Trung thật để đo (mỗi video nguồn khác nhau), đây là ước lượng
# theo đúng zone bản cũ từng che kín, không phải số đo chính xác cho mọi video.
SUB_MARGIN_V_RATIO = 0.14
SUB_MAX_LINE_WIDTH_RATIO = 0.92  # đo rộng hơn tỉ lệ này (theo video_w) thì tự xuống dòng

# Style mặc định — khớp y hệt giá trị hardcode đã verify kỹ (drawbox trắng đục 55%, viền đen
# 1px, chữ đậm trắng) trước khi panel chỉnh style ra đời, để job không truyền sub_style vẫn
# ra đúng video y hệt trước đây.
DEFAULT_SUB_STYLE = {
    "bold": True,
    "text_color": "#FFFFFF",
    "outline_color": "#000000",
    "outline_width": 1,
    "background_enabled": True,
    "background_color": "#FFFFFF",
    "background_opacity": 55,
}

# Con trỏ "style người dùng đã lưu làm mặc định" — cùng pattern _default.json của
# music_library.py (con trỏ đơn, không phải nhiều preset). Chỉ `api` cần đọc/ghi (reup-ui đọc
# lúc mount form để tiền điền, ghi lúc bấm "Lưu làm mặc định") — job burn thật (worker) KHÔNG
# cần biết default này, vì reup-ui luôn gửi nguyên giá trị style hiện tại trong payload submit,
# không dựa vào worker tự tra cứu (giống hệt cách nhạc nền mặc định hoạt động).
STYLE_DIR = Path(os.environ.get("STYLE_DIR", "/style"))
STYLE_DEFAULT_FILE = "sub_style_default.json"


def get_default_sub_style() -> dict | None:
    path = STYLE_DIR / STYLE_DEFAULT_FILE
    if not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(saved, dict):
        return None
    return merge_sub_style(saved)


def set_default_sub_style(sub_style: dict) -> dict:
    merged = merge_sub_style(sub_style)
    STYLE_DIR.mkdir(parents=True, exist_ok=True)
    (STYLE_DIR / STYLE_DEFAULT_FILE).write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    return merged


def clear_default_sub_style() -> None:
    path = STYLE_DIR / STYLE_DEFAULT_FILE
    if path.is_file():
        path.unlink()

# Demo chỉ hiển thị đúng khối chữ sub (crop sát), không mô phỏng nguyên khung video — cỡ chữ
# cố định đủ lớn để xem rõ trên form, không tính theo tỉ lệ video thật (không cần, vì không
# còn vẽ khung video giả nữa).
PREVIEW_FONT_SIZE = 44
PREVIEW_MAX_TEXT_W = 900  # đủ rộng để câu mẫu mặc định ra đúng 1 dòng (đo thật: 753px ở 44px
# Bold) — wrap_text() vẫn tự xuống dòng nếu câu mẫu dài hơn, không giới hạn cứng thành nhiều dòng
PREVIEW_PAD = 24  # lề quanh khối chữ+nền, tính từ mép ảnh demo


def _hex_to_ass_color(hex_str: str) -> str:
    """#RRGGBB (web/CSS) -> &H00BBGGRR (ASS, thứ tự BGR, không alpha — alpha ASS style field
    này không dùng được cho box nền do giới hạn libass đã gặp, chỉ dùng cho chữ/viền)."""
    h = hex_str.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def _hex_to_ffmpeg_color(hex_str: str, opacity_pct: float) -> str:
    """#RRGGBB + độ đục 0-100 -> cú pháp màu ffmpeg (`drawbox`/`color` filter), vd `0xFFFFFF@0.55`."""
    h = hex_str.lstrip("#")
    return f"0x{h}@{max(0.0, min(100.0, opacity_pct)) / 100:.2f}"


SRT_BLOCK_RE = re.compile(
    r"\d+\s*\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)",
    re.S,
)


def _srt_ts_to_s(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _ass_ts(t: float) -> str:
    cs = round(t * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def parse_srt(path: Path) -> list[dict]:
    segments = []
    for match in SRT_BLOCK_RE.finditer(path.read_text(encoding="utf-8")):
        start_s, end_s, body = match.groups()
        line = " ".join(l.strip() for l in body.strip().splitlines() if l.strip())
        if line:
            segments.append({"start": _srt_ts_to_s(start_s), "end": _srt_ts_to_s(end_s), "text": line})
    return segments


def probe_video_dims(video: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(video)],
        capture_output=True, text=True,
    )
    w, h = out.stdout.strip().split("x")
    return int(w), int(h)


def wrap_text(text: str, font: "ImageFont.FreeTypeFont", max_w: float) -> list[str]:
    """Tự xuống dòng theo độ rộng thật (Pillow), KHÔNG để libass tự wrap — script generate
    ra dùng WrapStyle=2 (chỉ ngắt dòng đúng chỗ mình chèn \\N), nên số dòng/độ rộng ở đây
    phải là sự thật tuyệt đối, không phải ước lượng."""
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if not cur or font.getbbox(trial)[2] <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [text]


def _text_block_size(font: "ImageFont.FreeTypeFont", lines: list[str], stroke_width: int) -> tuple[int, int, int]:
    """Đo CHÍNH XÁC kích thước khối chữ (kể cả viền) bằng Pillow `multiline_textbbox` — thay vì
    ước lượng qua `font_size * tỉ lệ dòng` (cách cũ luôn dư khoảng trắng ngoài ink thật của chữ,
    khiến nền bao quanh trông thừa/rộng hơn chữ rõ rệt). Trả (width, height, spacing) — spacing
    là khoảng cách giữa các dòng đã dùng để đo, phải truyền lại y hệt lúc vẽ để khớp 100%."""
    spacing = round(font.size * 0.15)  # chỉ có tác dụng khi 2+ dòng — khoảng hở giữa các dòng
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bbox = dummy.multiline_textbbox(
        (0, 0), "\n".join(lines), font=font, stroke_width=stroke_width, align="center", spacing=spacing,
    )
    return bbox[2] - bbox[0], bbox[3] - bbox[1], spacing


ASS_HEADER_TMPL = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary_colour},&H000000FF,{outline_colour},&H00000000,{bold},0,0,0,100,100,0,0,1,{outline_width},0,2,20,20,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass_and_boxes(
    segments: list[dict], video_w: int, video_h: int, ass_path: Path, sub_style: dict,
) -> str:
    """Sinh file .ass (PlayRes = kích thước video THẬT, tự wrap sẵn bằng \\N — bỏ hẳn việc
    dựa vào ffmpeg auto-convert .srt->.ass, vì bản mặc định dùng PlayRes 384x288 làm chữ bị
    phóng to sai tỉ lệ khi scale lên video thật, gây tràn dòng — phát hiện khi soi frame thật)
    + trả về chuỗi filter drawbox (1 cái/segment, enable=between, khớp CHÍNH XÁC số dòng đã
    wrap ở trên, không phải áng chừng)."""
    font_size = max(round(video_h * SUB_FONT_SIZE_RATIO), 16)
    font = ImageFont.truetype(_sub_font_path(sub_style["bold"]), font_size)
    max_line_w = video_w * SUB_MAX_LINE_WIDTH_RATIO
    margin_v = round(video_h * SUB_MARGIN_V_RATIO)

    header = ASS_HEADER_TMPL.format(
        w=video_w, h=video_h, font=SUB_FONT_NAME, size=font_size, marginv=margin_v,
        primary_colour=_hex_to_ass_color(sub_style["text_color"]),
        outline_colour=_hex_to_ass_color(sub_style["outline_color"]),
        bold=-1 if sub_style["bold"] else 0,
        outline_width=sub_style["outline_width"],
    )
    events = []
    box_filters = []
    box_color = _hex_to_ffmpeg_color(sub_style["background_color"], sub_style["background_opacity"])
    for seg in segments:
        lines = wrap_text(seg["text"].replace("{", "(").replace("}", ")"), font, max_line_w)
        ass_text = f"{chr(92)}N".join(lines)
        events.append(f"Dialogue: 0,{_ass_ts(seg['start'])},{_ass_ts(seg['end'])},Default,,0,0,0,,{ass_text}")

        if not sub_style["background_enabled"]:
            continue
        text_w, text_h, _ = _text_block_size(font, lines, sub_style["outline_width"])
        box_w = min(round(text_w + SUB_BOX_PAD_X * 2), video_w)
        box_h = round(text_h + SUB_BOX_PAD_Y * 2)
        y = video_h - margin_v - box_h
        enable = f"between(t,{seg['start']:.3f},{seg['end']:.3f})"
        box_filters.append(f"drawbox=x=(iw-{box_w})/2:y={y}:w={box_w}:h={box_h}:color={box_color}:t=fill:enable='{enable}'")

    ass_path.write_text(header + "\n".join(e for e in events if e), encoding="utf-8")
    return ",".join(box_filters)


def render_preview_png(sub_style: dict, sample_text: str = "Đây là ví dụ phụ đề tiếng Việt.") -> bytes:
    """Demo CHỈ đúng phần chữ sub (không mô phỏng nguyên khung video — người dùng chỉ cần xem
    rõ style chữ, nhét vào khung video thu nhỏ làm chữ tí hon không đọc được). Crop sát khối
    chữ + nền, cỡ chữ cố định đủ lớn để xem rõ trên form. Dùng lại wrap_text() nên vẫn tự xuống
    dòng khi câu dài, không gọi ffmpeg (render PIL trực tiếp, đủ nhanh để gọi lại mỗi lần chỉnh)."""
    font = ImageFont.truetype(_sub_font_path(sub_style["bold"]), PREVIEW_FONT_SIZE)
    lines = wrap_text(sample_text, font, PREVIEW_MAX_TEXT_W)
    text = "\n".join(lines)
    text_w, text_h, spacing = _text_block_size(font, lines, sub_style["outline_width"])

    box_w = round(text_w + SUB_BOX_PAD_X * 2)
    box_h = round(text_h + SUB_BOX_PAD_Y * 2)
    canvas_w = box_w + PREVIEW_PAD * 2
    canvas_h = box_h + PREVIEW_PAD * 2
    box_x, box_y = PREVIEW_PAD, PREVIEW_PAD

    # Nền trung tính tối — chỉ để thấy tương phản của nền sub khi bật/tắt, không mô phỏng frame
    # video thật (video thật xem trực tiếp sau khi chạy pipeline, demo này chỉ xem STYLE chữ).
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (38, 38, 44, 255))

    if sub_style["background_enabled"]:
        alpha = round(255 * max(0.0, min(100.0, sub_style["background_opacity"])) / 100)
        bg_rgb = tuple(int(sub_style["background_color"].lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle(
            [box_x, box_y, box_x + box_w, box_y + box_h], fill=(*bg_rgb, alpha),
        )
        canvas = Image.alpha_composite(canvas, overlay)

    # Vẽ đúng tại vị trí đã đo (anchor mặc định "la" — khớp 100% với _text_block_size ở trên,
    # không suy luận offset qua công thức font_size*tỉ lệ như trước) — bù lại bbox[0]/[1] (có
    # thể âm do viền vẽ lấn ra ngoài) để khối chữ THẬT nằm khít đúng ô đã trừ padding.
    dummy = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bbox = dummy.multiline_textbbox(
        (0, 0), text, font=font, stroke_width=sub_style["outline_width"], align="center", spacing=spacing,
    )
    target_x, target_y = box_x + SUB_BOX_PAD_X, box_y + SUB_BOX_PAD_Y
    draw = ImageDraw.Draw(canvas)
    draw.multiline_text(
        (target_x - bbox[0], target_y - bbox[1]), text, font=font, fill=sub_style["text_color"],
        stroke_width=sub_style["outline_width"], stroke_fill=sub_style["outline_color"],
        align="center", spacing=spacing,
    )

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Edit CLI: video + audio mới (+ srt) -> video final"
    )
    parser.add_argument("-v", "--video", type=Path, required=True, help="Video gốc (chỉ lấy hình)")
    parser.add_argument("-a", "--audio", type=Path, required=True, help="Audio mới (TTS), thay hoàn toàn audio gốc")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Video final đầu ra")
    parser.add_argument("-s", "--subtitles", type=Path, help="File .srt (tùy chọn)")
    parser.add_argument(
        "--subtitle-mode",
        choices=("none", "soft", "burn"),
        default="none",
        help="none: bỏ qua sub | soft: mux sub rời (chọn bật/tắt được) | burn: in cứng vào hình (re-encode)",
    )
    parser.add_argument("--crf", type=int, default=20, help="CRF khi burn sub / re-encode (mặc định: 20)")
    parser.add_argument("--audio-bitrate", default="192k", help="Bitrate audio đầu ra (mặc định: 192k)")
    return parser.parse_args()


def escape_subtitles_path(path: Path) -> str:
    # ffmpeg filter arg: dấu ':' và '\' cần escape, path bọc trong nháy đơn
    escaped = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"'{escaped}'"


def merge_sub_style(sub_style: dict | None) -> dict:
    """Merge (không thay hẳn) với default — caller (CLI thủ công, curl tay, payload cũ) có thể
    chỉ gửi 1-2 field muốn đổi, thiếu field nào thì rơi về đúng giá trị mặc định đã verify,
    không crash KeyError (phát hiện lúc test tay: gửi thiếu field crash ngay trong build_ass_and_boxes)."""
    return {**DEFAULT_SUB_STYLE, **(sub_style or {})}


def build_command(
    video: Path, audio: Path, output: Path,
    subtitles: Path | None, subtitle_mode: str, crf: int, audio_bitrate: str,
    sub_style: dict | None = None,
) -> list[str]:
    sub_style = merge_sub_style(sub_style)
    cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(audio)]

    if subtitles and subtitle_mode == "soft":
        cmd += ["-i", str(subtitles)]
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-map", "2:s:0"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate, "-c:s", "mov_text"]
    elif subtitles and subtitle_mode == "burn":
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        # Video ngắn nguồn (Douyin/TikTok) thường đã có sẵn phụ đề tiếng Trung hardcode ở đúng
        # vùng dưới màn hình — không có nền, sub mới burn đè lên bị chồng chữ, không đọc được
        # (phát hiện khi soi frame thật, không thấy được nếu chỉ check ffprobe/duration). Từng
        # thử BorderStyle=3 (ASS "opaque box") + BackColour alpha (vd &H50000000) để có nền bán
        # trong suốt — nhưng verify bằng test đơn sắc (nền đỏ đặc) phát hiện: libass bản ffmpeg
        # này KHÔNG hỗ trợ alpha cho BorderStyle=3 ở bất kỳ cách nào (force_style, Style: line
        # trong .ass thật, cả tag inline \4a) — luôn ra đen đặc 100% bất kể giá trị alpha, là
        # giới hạn cứng của thư viện chứ không phải lỗi cú pháp. Chuyển sang `drawbox` (filter
        # riêng của ffmpeg, có alpha thật qua cú pháp color@opacity). Bản đầu dùng 1 dải cố định
        # che hết footer (đen, ih*0.12) — sau phản hồi thực tế: cần nền TRẮNG đục mờ (không phải
        # đen) và chỉ ôm sát đúng dòng sub tiếng Việt (dải cũ che luôn cả caption tiếng Trung có
        # sẵn bên dưới, không đúng ý). Đợt sửa đầu (per-segment drawbox nhưng vẫn burn thẳng file
        # .srt gốc qua force_style) verify bằng frame thật phát hiện bug khác: ffmpeg auto-convert
        # .srt->.ass mặc định dùng PlayResX/Y=384x288 rồi scale lên kích thước video thật khi
        # render — Fontsize mình set (tính theo pixel thật) bị phóng to sai tỉ lệ theo đó, chữ
        # tràn 2-3 dòng ngoài dự tính, box tính theo pixel thật lệch hẳn vị trí/kích thước thật.
        # Fix: tự sinh file .ass riêng (PlayResX/Y = kích thước video THẬT, WrapStyle=2 tắt hẳn
        # auto-wrap của libass, tự xuống dòng bằng Pillow trong build_ass_and_boxes()) — Fontsize
        # từ đó là pixel thật 1:1, box tính theo đúng số dòng đã wrap, không còn đoán mò.
        subtitles_p = Path(subtitles)
        video_w, video_h = probe_video_dims(video)
        segments = parse_srt(subtitles_p)
        if segments:
            ass_path = output.parent / f"{output.stem}_burn.ass"
            boxes = build_ass_and_boxes(segments, video_w, video_h, ass_path, sub_style)
            vf = f"{boxes},subtitles={escape_subtitles_path(ass_path)}" if boxes else f"subtitles={escape_subtitles_path(ass_path)}"
        else:
            vf = f"subtitles={escape_subtitles_path(subtitles)}"
        cmd += ["-vf", vf]
        cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", "medium"]
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", audio_bitrate]

    cmd += ["-shortest", str(output)]
    return cmd


def run_edit(
    video: str, audio: str, output: str,
    subtitles: str | None = None, subtitle_mode: str = "none",
    crf: int = 20, audio_bitrate: str = "192k",
    sub_style: dict | None = None,
) -> dict:
    """Mux video gốc (chỉ hình) + audio mới + sub tùy chọn -> video final. Tách khỏi
    main()/argparse để CLI và worker dùng chung — logic ffmpeg command giữ nguyên 100%."""
    video_p, audio_p = Path(video), Path(audio)
    subtitles_p = Path(subtitles) if subtitles else None

    for label, path in (("video", video_p), ("audio", audio_p)):
        if not path.resolve().is_file():
            raise RuntimeError(f"file {label} không tồn tại: {path}")
    if subtitles_p and not subtitles_p.resolve().is_file():
        raise RuntimeError(f"file subtitles không tồn tại: {subtitles_p}")
    if subtitles_p and subtitle_mode == "none":
        raise RuntimeError("có subtitles nhưng subtitle_mode=none — chọn soft hoặc burn")

    output_p = Path(output).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    cmd = build_command(video_p, audio_p, output_p, subtitles_p, subtitle_mode, crf, audio_bitrate, sub_style)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-20:])
        raise RuntimeError(f"ffmpeg thất bại:\n{tail}")

    return {
        "ok": True,
        "video": str(video_p.resolve()),
        "audio": str(audio_p.resolve()),
        "subtitles": str(subtitles_p.resolve()) if subtitles_p else None,
        "subtitle_mode": subtitle_mode,
        "output": str(output_p),
        "elapsed_s": round(time.time() - start, 2),
    }


def main() -> int:
    args = parse_args()
    try:
        result = run_edit(
            str(args.video), str(args.audio), str(args.output),
            str(args.subtitles) if args.subtitles else None, args.subtitle_mode,
            args.crf, args.audio_bitrate,
        )
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
