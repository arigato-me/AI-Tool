#!/usr/bin/env python3
"""CLI: tạo sẵn 1 file wav mẫu 15-20s cho mỗi built-in voice, để `reup-ui` cho nghe thử trước
khi chọn giọng. Chạy thủ công 1 lần (hoặc lại mỗi khi `voices_v3_turbo.json` đổi preset) trên
máy có GPU — không phải job queue, không dùng lúc chạy pipeline thật."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vieneu import Vieneu

# Mỗi voice preset đã tự gắn sẵn 1 "style điểm mạnh" trong voices_v3_turbo.json (Minh Đức/Mai
# Anh -> tin_tuc, Thái Sơn/Thanh Bình/Ngọc Linh/Thục Đoan -> doc_truyen, còn lại -> tu_nhien) —
# đọc đúng nội dung khớp style đó (bản tin cho tin_tuc, truyện kể cho doc_truyen, đời thường cho
# tu_nhien) mới thể hiện đúng thế mạnh của từng voice, thay vì 1 văn bản trung tính dùng chung.
# ~65-75 từ mỗi đoạn -> khoảng 15-20s ở tốc độ đọc tiếng Việt thông thường.
STYLE_TEXTS = {
    "tu_nhien": (
        "Chào bạn, hôm nay chúng ta sẽ cùng nhau khám phá một góc phố nhỏ rất đáng yêu ở "
        "trung tâm thành phố. Không khí buổi sáng ở đây thật trong lành, mọi người đi lại "
        "nhộn nhịp, quán cà phê ven đường thơm nức mùi cà phê rang xay. Đây chính là nét đẹp "
        "bình dị mà mình muốn giới thiệu đến các bạn trong video hôm nay."
    ),
    "tin_tuc": (
        "Xin chào quý vị và các bạn, đây là bản tin nhanh trong ngày. Theo thông tin mới nhất, "
        "tình hình thời tiết tại các tỉnh miền Bắc có chuyển biến tích cực, nhiệt độ tăng nhẹ "
        "vào buổi trưa. Bên cạnh đó, nhiều hoạt động văn hóa, thể thao cũng được tổ chức sôi "
        "nổi tại các địa phương trong tuần này. Mời quý vị tiếp tục theo dõi các bản tin tiếp "
        "theo của chúng tôi."
    ),
    "doc_truyen": (
        "Ngày xửa ngày xưa, ở một ngôi làng nhỏ ven sông, có một cậu bé rất thích nghe kể "
        "chuyện cổ tích mỗi tối trước khi đi ngủ. Cậu thường ngồi bên bếp lửa, lắng nghe bà kể "
        "về những nàng tiên, những chàng hoàng tử dũng cảm và những cuộc phiêu lưu kỳ thú. Câu "
        "chuyện cứ thế trôi qua, đưa cậu bé chìm vào giấc mơ đẹp mỗi đêm."
    ),
}
DEFAULT_STYLE = "tu_nhien"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tạo sample audio 15-20s cho từng built-in voice, mỗi voice đọc đúng nội "
        "dung khớp style/điểm mạnh riêng của nó (dùng cho nghe thử trên reup-ui)"
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(os.environ.get("VOICE_SAMPLES_DIR", "/voice_samples")),
        help="Thư mục ghi file <voice_id>.wav (mặc định env VOICE_SAMPLES_DIR hoặc /voice_samples)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    tts = Vieneu()
    voices = tts.list_preset_voices()
    if not voices:
        print("Lỗi: không có built-in voice nào để tạo sample.", file=sys.stderr)
        return 1

    results = []
    for label, voice_id in voices:
        preset = tts.get_preset_voice(voice_id)
        style = preset.get("style") or DEFAULT_STYLE
        text = STYLE_TEXTS.get(style, STYLE_TEXTS[DEFAULT_STYLE])

        audio = tts.infer(text, voice=voice_id, style=style)
        out_path = args.out_dir / f"{voice_id}.wav"
        tts.save(audio, str(out_path))
        duration_s = round(len(audio) / tts.sample_rate, 2)
        if not (12 <= duration_s <= 22):
            print(
                f"[generate_voice_samples] cảnh báo: '{voice_id}' dài {duration_s}s, "
                "ngoài khoảng 12-22s mong muốn (không tự chỉnh lại text)",
                file=sys.stderr,
            )
        results.append({
            "voice": voice_id, "label": label, "style": style,
            "output": str(out_path), "duration_s": duration_s,
        })
        print(f"[generate_voice_samples] {voice_id} ({style}): {duration_s}s -> {out_path}", file=sys.stderr)

    print(json.dumps({"ok": True, "count": len(results), "voices": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
