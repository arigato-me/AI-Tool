#!/usr/bin/env python3
"""CLI: transcript JSON (segments start/end/text) -> 1 file wav, mỗi segment đặt tại timestamp
gốc của nó khi có thể (silence-padded) — nếu segment liền trước nói dài hơn slot, segment này
dời lùi lại đúng bằng đó thay vì bị đè lên (cascading placement, xem comment trong
run_segments()). Vẫn dùng để mux với video gốc; phụ đề có thể lệch nhẹ so với audio trong đoạn
"trôi" — xem giới hạn đã biết trong docstring run_segments()."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import librosa
import numpy as np

import queue_lib as q

if TYPE_CHECKING:  # tránh import "vieneu" (nặng, cần GPU) ở top-level chỉ để chạy CLI thật —
    # test (test_segments_cli.py) tiêm sẵn 1 fake qua tham số `tts=`, không bao giờ chạy nhánh
    # thật Vieneu() bên dưới nên không cần package "vieneu" cài sẵn để chạy test
    from vieneu import Vieneu

STYLES = ("tu_nhien", "tin_tuc", "doc_truyen")
CJK_RE = re.compile(r"[一-鿿]")
CJK_SKIP_RATIO = 0.3  # ngưỡng giống hệt text_looks_untranslated() bên translate_cli.py
STRETCH_TRIGGER_RATIO = 1.1  # bỏ qua chênh lệch nhỏ (<10%, khó nhận ra khi nghe) — mỗi lần nén
# tốn 1 lượt phase-vocoder CPU, đo thực tế 1.05 khiến ~72% segment bị nén, tốn thêm ~165s cho
# video ~10 phút; nâng lên 1.1 giảm số lượt nén không đáng, giữ nguyên lợi ích chính
STRETCH_MAX_RATIO = 1.4  # trần tốc độ nén — nhanh hơn mức này nghe khó hiểu/méo giọng
HALLUCINATION_MIN_CHARS_PER_SEC = 5.0  # tốc độ đọc chậm nhất từng quan sát trên video thật là
# ~14-21 ký tự/giây — đặt ngưỡng thấp hơn nhiều lần để không false-positive với câu đọc chậm
# thật, chỉ bắt hallucination (quan sát thực tế: model sinh 23.92s audio cho câu "Ơ." 2 ký tự =
# 0.08 ký tự/giây, gây "trôi" cascading 17s dồn qua toàn bộ phần còn lại của video)
HALLUCINATION_FLOOR_S = 6.0  # sàn tối thiểu cho câu quá ngắn (thán từ) — câu hợp lệ ngắn nhất
# từng quan sát dài 4.08s cho 2 ký tự, thêm biên an toàn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Segments CLI: transcript JSON -> 1 file wav ghép theo timestamp"
    )
    parser.add_argument("-i", "--input", type=Path, required=True, help="File JSON transcript đầu vào")
    parser.add_argument("-o", "--output", type=Path, required=True, help="File wav đầu ra")
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


def run_segments(
    input_path: str, output_path: str,
    voice: str | None = None, style: str = "tu_nhien",
    tts: Vieneu | None = None, ref_audio: str | None = None,
    pipeline_id: str | None = None, video_name: str | None = None,
) -> dict:
    """Transcript JSON -> 1 file wav ghép theo timestamp (silence-padded), cascading khi cần
    (xem comment tại chỗ tính `start_sample`) — đo thực tế video test: không cascading, 81%
    segment bị đè lên đầu segment sau (tiếng Việt nói dài hơn tiếng Trung cho cùng nội dung),
    người dùng xác nhận không chấp nhận mất chữ do bị đè dù đã time-stretch tới trần nghe được.
    Giới hạn CÒN LẠI sau fix này: phụ đề (`json_to_srt.py`) vẫn dùng timestamp GỐC, không biết
    audio đã "trôi" — trong đoạn trôi, phụ đề hiện sớm hơn lời thoại vài trăm ms tới vài giây
    (chấp nhận được, còn hơn bị đè). Nhánh dialogue (`mix_dialogue_cli.py`) dùng start/end GỐC
    làm cửa sổ mix instrumental/original — audio trôi ra ngoài cửa sổ đó sẽ mix sai; cascading
    này CHỈ an toàn cho nhánh review, chưa xử lý cho dialogue. Tách khỏi main()/argparse để CLI
    và worker dùng chung — `tts` cho phép worker truyền vào 1 instance Vieneu đã load sẵn (giữ
    resident xuyên suốt nhiều job).
    `ref_audio` (file mẫu 3-5s) bật clone giọng cho toàn bộ segment — bỏ qua hẳn
    `resolve_voice()`/preset khi được set, dùng thẳng `infer(text, ref_audio=...)`."""
    input_p = Path(input_path).resolve()
    output_p = Path(output_path).resolve()

    if not input_p.is_file():
        raise RuntimeError(f"file input không tồn tại: {input_p}")

    data = json.loads(input_p.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    if not segments:
        raise RuntimeError(f"file input không có 'segments': {input_p}")

    output_p.parent.mkdir(parents=True, exist_ok=True)

    start_time = time.time()
    if tts is not None:
        tts_engine = tts
    else:
        from vieneu import Vieneu  # import ở đây (không ở top-level) — nặng/cần GPU, test tiêm sẵn `tts=`
        tts_engine = Vieneu()
    ref_p = None
    if ref_audio:
        ref_p = Path(ref_audio).resolve()
        if not ref_p.is_file():
            raise RuntimeError(f"file ref_audio không tồn tại: {ref_p}")
        voice_id = f"clone:{ref_p.name}"
    else:
        voice_id = resolve_voice(tts_engine, voice)
    sample_rate = tts_engine.sample_rate

    buffer = np.zeros(0, dtype=np.float32)
    cursor_sample = 0  # điểm audio thực tế đang kết thúc — segment sau KHÔNG BAO GIỜ được đặt
    # trước điểm này (xem docstring run_segments() và comment ở chỗ tính actual_start bên dưới)
    max_drift_s = 0.0
    drifted_segments = 0
    # Vị trí THẬT (sau cascading) của từng segment — nhánh dialogue (mix_dialogue_cli.py) cần
    # đúng con số này làm "cửa sổ" mute/duck audio gốc, KHÔNG phải timestamp gốc trong transcript
    # (mix_dialogue_cli vẫn dùng timestamp gốc nếu không có file này — xem docstring
    # run_segments() mục giới hạn dialogue). Segment bị skip (còn tiếng Trung) vẫn ghi 1 dòng
    # dùng ĐÚNG timestamp gốc — dialogue mode vẫn cần mute đoạn đó dù không có TTS để chèn vào.
    mix_windows: list[dict] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        # Câu vẫn còn nhiều chữ Hán = translate đã bỏ cuộc, giữ nguyên văn gốc (xem
        # translate_with_fallback() bên translate_cli.py) — VieNeu-TTS là model tiếng Việt,
        # đưa thẳng câu Hán vào infer() ra audio gần như câm/rác thay vì báo lỗi rõ ràng,
        # trước đây lặng lẽ trôi qua tới tận lúc nghe final.mp4 mới phát hiện. Bỏ qua, để
        # khoảng lặng CÓ CHỦ ĐÍCH (buffer đã zero-init sẵn) + log rõ ràng thay vì audio rác.
        if len(CJK_RE.findall(text)) / len(text) > CJK_SKIP_RATIO:
            print(
                f"Cảnh báo: segment id={seg.get('id')} vẫn còn tiếng Trung (translate chưa dịch "
                f"được) — bỏ qua TTS, để khoảng lặng, cần dịch tay: {text[:80]!r}",
                file=sys.stderr,
            )
            q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                         "event": "tts_skipped_untranslated", "segment_id": seg.get("id"),
                         "start": seg["start"], "end": seg["end"], "text": text[:300]})
            mix_windows.append({"id": seg.get("id"), "start": seg["start"], "end": seg["end"]})
            continue
        def _synth() -> np.ndarray:
            if ref_p is not None:
                return tts_engine.infer(text, ref_audio=str(ref_p), style=style).astype(np.float32)
            return tts_engine.infer(text, voice=voice_id, style=style).astype(np.float32)

        audio = _synth()

        # Hallucination guard: model đôi khi "chạy trốn" với câu quá ngắn, sinh audio dài bất
        # thường (thực tế: 23.92s cho câu "Ơ." 2 ký tự, thay vì <1s) — vượt xa trần nén
        # STRETCH_MAX_RATIO nên cascading placement bên dưới đẩy lùi (drift) toàn bộ timeline
        # phía sau cho tới hết video. Thử infer() lại 1 lần (seed khác thường ra audio bình
        # thường); vẫn bất thường thì bỏ qua giống hệt case còn tiếng Trung ở trên — thà mất 1
        # câu ngắn còn hơn kéo lệch cả video.
        max_plausible_s = max(HALLUCINATION_FLOOR_S, len(text) / HALLUCINATION_MIN_CHARS_PER_SEC)
        if len(audio) / sample_rate > max_plausible_s:
            print(
                f"Cảnh báo: segment id={seg.get('id')} audio TTS dài bất thường "
                f"({len(audio) / sample_rate:.1f}s cho {len(text)} ký tự) — thử lại 1 lần: {text[:80]!r}",
                file=sys.stderr,
            )
            audio = _synth()
            if len(audio) / sample_rate > max_plausible_s:
                print(
                    f"Cảnh báo: segment id={seg.get('id')} vẫn hallucination sau retry — bỏ qua "
                    f"TTS, để khoảng lặng, cần kiểm tra tay: {text[:80]!r}",
                    file=sys.stderr,
                )
                q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                             "event": "tts_hallucination_skipped", "segment_id": seg.get("id"),
                             "start": seg["start"], "end": seg["end"], "text": text[:300],
                             "raw_duration_s": round(len(audio) / sample_rate, 2)})
                mix_windows.append({"id": seg.get("id"), "start": seg["start"], "end": seg["end"]})
                continue

        # Tiếng Việt nói dài hơn tiếng Trung cho cùng nội dung (tỉ lệ âm tiết) — đo thực tế
        # trên video test: 81% segment overrun slot gốc, tổng >100s bị đè lên đầu segment sau.
        # Nén (time-stretch) audio về vừa slot khi overrun đáng kể, giới hạn tốc độ tối đa
        # STRETCH_MAX_RATIO — nhanh hơn mức này nghe khó hiểu/méo giọng (đo thực tế: median tỉ
        # lệ audio/slot ~1.19x, nén nhẹ không ảnh hưởng chất lượng nghe). Slot quá ngắn so với
        # câu (ratio vượt xa mức trần) vẫn còn overrun sau khi nén hết mức — chấp nhận, còn hơn
        # nén tới mức không nghe rõ được.
        raw_duration_s = round(len(audio) / sample_rate, 2)
        slot_duration_s = round(float(seg["end"]) - float(seg["start"]), 2)
        stretch_ratio = 1.0
        if slot_duration_s > 0.05 and raw_duration_s / slot_duration_s > STRETCH_TRIGGER_RATIO:
            stretch_ratio = min(raw_duration_s / slot_duration_s, STRETCH_MAX_RATIO)
            audio = librosa.effects.time_stretch(audio, rate=stretch_ratio)

        # Đặt segment tại max(timestamp gốc, điểm audio trước kết thúc) — KHÔNG BAO GIỜ đè lên
        # audio đang phát (khác hành vi cũ: đặt cứng theo timestamp gốc, ghi đè lệch cả segment
        # trước nếu nó dài hơn slot). Có khoảng nghỉ tự nhiên trong video gốc (audio trước kết
        # thúc sớm hơn seg["start"]) -> dùng thẳng seg["start"], không đổi gì, tự "bắt kịp" giờ
        # gốc ngay khi có thể. Không có khoảng nghỉ -> dời lùi đúng bằng phần audio trước còn
        # đang nói, tiếng Việt "trôi" trễ dần so với hình trong đoạn thoại dày đặc — chấp nhận
        # được cho review/thuyết minh (không lipsync theo miệng), còn hơn mất chữ do bị đè.
        natural_start_sample = round(float(seg["start"]) * sample_rate)
        start_sample = max(natural_start_sample, cursor_sample)
        end_sample = start_sample + len(audio)
        seg_audio_duration_s = round(len(audio) / sample_rate, 2)
        overrun_s = round(max(0.0, seg_audio_duration_s - slot_duration_s), 2)
        drift_s = round((start_sample - natural_start_sample) / sample_rate, 2)
        if drift_s > 0:
            drifted_segments += 1
            max_drift_s = max(max_drift_s, drift_s)
        q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "tts_segment",
                     "segment_id": seg.get("id"), "start": seg["start"], "end": seg["end"],
                     "text_chars": len(text), "raw_duration_s": raw_duration_s,
                     "stretch_ratio": round(stretch_ratio, 3), "audio_duration_s": seg_audio_duration_s,
                     "slot_duration_s": slot_duration_s, "overrun_s": overrun_s, "drift_s": drift_s})
        if end_sample > len(buffer):
            buffer = np.pad(buffer, (0, end_sample - len(buffer)))
        buffer[start_sample:end_sample] = audio
        cursor_sample = end_sample
        mix_windows.append({"id": seg.get("id"), "start": round(start_sample / sample_rate, 3),
                             "end": round(end_sample / sample_rate, 3)})

    tts_engine.save(buffer, str(output_p))
    windows_p = output_p.with_name(f"{output_p.stem}_windows.json")
    windows_p.write_text(json.dumps({"segments": mix_windows}, ensure_ascii=False), encoding="utf-8")
    synthesis_time_s = round(time.time() - start_time, 2)
    audio_duration_s = round(len(buffer) / sample_rate, 2)

    return {
        "ok": True,
        "input": str(input_p),
        "output": str(output_p),
        "voice": voice_id,
        "style": style,
        "segments": len(segments),
        "synthesis_time_s": synthesis_time_s,
        "audio_duration_s": audio_duration_s,
        # Segment không bao giờ bị đè nữa (xem cascading placement ở trên) — thay vào đó có thể
        # "trôi" trễ hơn giờ gốc trong đoạn thoại dày đặc; 2 số này cho biết mức trôi tệ nhất.
        "drifted_segments": drifted_segments,
        "max_drift_s": max_drift_s,
        # File JSON vị trí THẬT từng segment (xem mix_windows ở trên) — nhánh dialogue
        # (mix_dialogue_cli.py --windows) cần file này để mute/duck đúng chỗ audio gốc đang
        # thật sự phát, không phải ở chỗ transcript gốc ghi lúc chưa cascading.
        "windows_path": str(windows_p),
    }


def main() -> int:
    args = parse_args()
    try:
        result = run_segments(str(args.input), str(args.output), args.voice, args.style, ref_audio=str(args.ref_audio) if args.ref_audio else None)
    except (RuntimeError, ValueError) as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
