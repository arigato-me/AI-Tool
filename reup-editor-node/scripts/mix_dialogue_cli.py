#!/usr/bin/env python3
"""CLI (nhánh video thoại, mode=dialogue): trộn track nền (instrumental) + audio gốc mức
thấp + track TTS thành 1 file audio final — chỉ thay bằng instrumental ở đúng cửa sổ
VAD-speech (từ transcript), ngoài đó giữ nguyên audio gốc 100%. Có crossfade ở ranh giới
để tránh nghe rõ chỗ chuyển. Kết quả (`-o`) dùng làm input `-a` cho `edit_cli.py` — không
đụng gì tới nhánh "review" hiện có."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml
from scipy.signal import resample_poly
from math import gcd

import queue_lib as q

DEFAULT_CONFIG_PATH = "/config/dialogue.yaml"


def load_config(path: str) -> dict:
    default = {
        "original_mix_level": 0.15, "speech_only_replace": True, "crossfade_ms": 100,
        "tts_base_gain": 1.4, "tts_target_ratio": 2.5, "tts_gain_min": 1.0, "tts_gain_max": 4.0,
        # Chuẩn hoá LOUDNESS TỔNG THỂ track final (EBU R128 / ffmpeg loudnorm) — khác hẳn
        # tts_target_ratio ở trên (vốn chỉ cân bằng TTS/nền TRONG track). Đo thật trên 1 job
        # dialogue thật: video gốc -12.83 LUFS, output final -20.97 LUFS — lệch ~8 LUFS dù
        # tts_gain đã hoạt động đúng (log thật: median gain=1.0, chỉ 8/612 segment chạm trần
        # 4.0) — root cause KHÔNG phải tỉ lệ TTS/nền mà là thiếu bước chuẩn hoá độ to tổng thể
        # so với video gốc. -14.0 LUFS gần với loudness thường thấy của video Douyin/TikTok đã
        # master sẵn (đo được -12.83 ở ví dụ thật) mà không cần đo riêng từng video gốc.
        "loudnorm_enabled": True,
        "loudnorm_target_i": -14.0,
        "loudnorm_true_peak": -1.5,
        "loudnorm_lra": 11.0,
    }
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return {**default, **cfg.get("mix_dialogue", {})}
    except FileNotFoundError:
        return default


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mix-dialogue CLI: instrumental + audio gốc (mức thấp) + TTS -> 1 track final (nhánh video thoại)"
    )
    parser.add_argument("--original", type=Path, required=True, help="Audio gốc đầy đủ (docker_transcribe mode=dialogue)")
    parser.add_argument("--instrumental", type=Path, required=True, help="Track nền đã tách (docker_transcribe mode=dialogue)")
    parser.add_argument("--tts", type=Path, required=True, help="Track TTS tiếng Việt (docker_vieneu-tts_gpu segments)")
    parser.add_argument("--transcript", type=Path, required=True, help="Transcript JSON (translated.json) — lấy start/end làm cửa sổ speech")
    parser.add_argument("--windows", type=Path, default=None,
                         help="File *_windows.json (từ reup-tts-gpu-node/scripts/segments_cli.py) ghi vị trí THẬT "
                              "từng segment sau cascading — dùng thay --transcript làm cửa sổ mute/duck nếu có "
                              "(fallback về --transcript nếu không truyền/file không tồn tại, không đổi hành vi cũ)")
    parser.add_argument("-o", "--output", type=Path, required=True, help="File audio final (dùng làm -a cho edit_cli.py)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="File dialogue.yaml (mặc định /config/dialogue.yaml)")
    parser.add_argument("--original-mix-level", type=float, default=None, help="Override config: mức audio gốc trộn vào instrumental (0-1)")
    parser.add_argument("--crossfade-ms", type=float, default=None, help="Override config: độ dài crossfade (ms)")
    parser.add_argument("--speech-only", dest="speech_only", action="store_true", default=None)
    parser.add_argument("--no-speech-only", dest="speech_only", action="store_false")
    parser.add_argument("--tts-target-ratio", type=float, default=None,
                         help="Override config: tỉ lệ RMS mục tiêu TTS/nền trong mỗi speech window (vd 1.3 = TTS to hơn nền 30%)")
    parser.add_argument("--tts-base-gain", type=float, default=None,
                         help="Override config: hệ số khuếch đại cố định nhân thẳng vào track TTS trước khi tính gain theo nền")
    return parser.parse_args()


def resample_to(arr: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Resample bằng polyphase filter (scipy) — cần vì TTS (VieNeu) xuất ở sample rate
    riêng của engine (thường 48kHz, chất lượng cao) trong khi original/instrumental ở
    16kHz (chuẩn STT/VAD xuyên suốt pipeline transcribe)."""
    if src_sr == dst_sr:
        return arr
    g = gcd(src_sr, dst_sr)
    return resample_poly(arr, dst_sr // g, src_sr // g).astype(np.float32)


def crossfade_ramp(n: int) -> np.ndarray:
    """Cosine ramp 0->1 độ dài n sample — chuyển mượt giữa 2 nguồn audio, tránh tiếng click."""
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    t = np.linspace(0, np.pi, n, dtype=np.float32)
    return (1 - np.cos(t)) / 2


def rms(x: np.ndarray) -> float:
    """Root-mean-square — dùng làm proxy độ to (loudness) khi so TTS với audio nền, thay
    vì so peak (dễ lệch do 1-2 sample đột biến)."""
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0


SILENCE_EPS = 1e-4  # ngưỡng biên độ tuyệt đối coi là im lặng (đệm silence, không phải giọng nói thật)


def active_rms(x: np.ndarray, eps: float = SILENCE_EPS) -> float:
    """RMS chỉ tính trên phần biên độ thật (loại khoảng lặng đệm đầu/cuối slot). segments_cli
    đặt mỗi câu TTS đúng vị trí timestamp rồi silence-pad phần dư trong slot — nếu 1 câu chỉ
    nói hết 60% thời lượng slot, tính RMS trên CẢ window (như background) sẽ bị pha loãng bởi
    40% im lặng còn lại, khiến gain tính ra thấp hơn thực tế cần so với độ to lúc đang nói
    thật — đây là lý do TTS vẫn nghe nhỏ dù đã áp target_ratio đúng."""
    if len(x) == 0:
        return 0.0
    active = x[np.abs(x) > eps]
    return rms(active) if len(active) else 0.0


def loudnorm_pass(input_path: Path, output_path: Path, target_i: float, true_peak: float, lra: float) -> dict:
    """2-pass ffmpeg loudnorm (EBU R128): pass 1 đo loudness thật của file, pass 2 áp dụng lại
    filter với thông số `measured_*` (linear=true) — chính xác hơn hẳn single-pass (ffmpeg docs:
    single-pass loudnorm chỉ ước lượng, dễ lệch target). Chuẩn hoá ĐỘ TO TỔNG THỂ của track đã
    mix xong về target_i, không đụng gì tới cân bằng TTS/nền đã tính ở apply_segment_replace/
    tts_gain phía trên — 2 cơ chế độc lập, xử lý 2 vấn đề khác nhau (xem comment ở load_config)."""
    measure_cmd = [
        "ffmpeg", "-i", str(input_path),
        "-af", f"loudnorm=I={target_i}:TP={true_peak}:LRA={lra}:print_format=json",
        "-f", "null", "-",
    ]
    measured = subprocess.run(measure_cmd, capture_output=True, text=True)
    json_start = measured.stderr.rfind("{")
    if json_start == -1:
        raise RuntimeError(f"ffmpeg loudnorm (đo) không trả JSON:\n{measured.stderr[-2000:]}")
    stats = json.loads(measured.stderr[json_start:])

    apply_filter = (
        f"loudnorm=I={target_i}:TP={true_peak}:LRA={lra}:"
        f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
        f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
        "linear=true:print_format=json"
    )
    apply_cmd = ["ffmpeg", "-y", "-i", str(input_path), "-af", apply_filter, str(output_path)]
    applied = subprocess.run(apply_cmd, capture_output=True, text=True)
    if applied.returncode != 0:
        raise RuntimeError(f"ffmpeg loudnorm (áp dụng) thất bại:\n{applied.stderr[-2000:]}")
    return stats


def apply_segment_replace(
    background: np.ndarray, instrumental: np.ndarray, original: np.ndarray,
    start_s: float, end_s: float, sample_rate: int, mix_level: float, crossfade_n: int,
) -> None:
    """Ghi đè background[start:end] = instrumental + original*mix_level (in-place), crossfade
    ở 2 biên với nội dung background hiện có để tránh nghe rõ chỗ chuyển."""
    start_i = max(0, round(start_s * sample_rate))
    end_i = min(len(background), round(end_s * sample_rate))
    if end_i <= start_i:
        return

    length = end_i - start_i
    replacement = instrumental[start_i:end_i] + original[start_i:end_i] * mix_level
    if len(replacement) < length:
        replacement = np.pad(replacement, (0, length - len(replacement)))

    cf = min(crossfade_n, length // 2)
    if cf > 0:
        ramp_in = crossfade_ramp(cf)
        old_start = background[start_i:start_i + cf]
        replacement[:cf] = old_start * (1 - ramp_in) + replacement[:cf] * ramp_in

        ramp_out = crossfade_ramp(cf)[::-1]
        old_end = background[end_i - cf:end_i]
        replacement[-cf:] = replacement[-cf:] * ramp_out + old_end * (1 - ramp_out)

    background[start_i:end_i] = replacement


def load_mix_windows(transcript_p: Path, windows_p: Path | None) -> list[dict]:
    """Cửa sổ mute/duck audio gốc cho từng segment — ưu tiên `windows_p` (*_windows.json từ
    segments_cli.py, ghi vị trí TTS THẬT sau cascading placement) nếu có, fallback về
    start/end GỐC trong transcript nếu không (hành vi y hệt trước khi có cascading).

    Root cause bug thật (job d2bd708a, user báo tiếng bị lặn/mất/chạy nhanh hơn sub từ phút
    4:48): cascading placement (segments_cli.py) cho phép audio TTS "trôi" trễ hơn timestamp
    gốc khi câu trước nói dài hơn slot — đúng ý đồ cho nhánh review (chỉ có 1 track audio).
    Nhưng nhánh dialogue mix theo CỬA SỔ cố định lấy từ transcript gốc: ngoài cửa sổ = trả về
    100% audio gốc. TTS trôi ra khỏi cửa sổ gốc vẫn được cộng vào track final ở đúng vị trí nó
    thực sự đứng (xem `final = background + tts_arr` bên dưới) — tức là giọng Việt bị cộng
    chồng lên đúng đoạn mix đã coi là "hết thoại, trả về gốc 100%", nghe như 2 tiếng chồng
    nhau. File *_windows.json khớp cửa sổ với vị trí TTS thật, xoá tận gốc vấn đề này."""
    data = json.loads(transcript_p.read_text(encoding="utf-8"))
    transcript_segs = data.get("segments", [])
    if windows_p is not None and windows_p.is_file():
        try:
            windows_data = json.loads(windows_p.read_text(encoding="utf-8"))
            windows_segs = windows_data.get("segments")
            if isinstance(windows_segs, list) and windows_segs:
                return windows_segs
        except (json.JSONDecodeError, OSError):
            pass
    return transcript_segs


def run_mix_dialogue(
    original: str, instrumental: str, tts: str, transcript: str, output: str,
    config: str = DEFAULT_CONFIG_PATH,
    original_mix_level: float | None = None,
    crossfade_ms: float | None = None,
    speech_only: bool | None = None,
    tts_target_ratio: float | None = None,
    tts_base_gain: float | None = None,
    windows: str | None = None,
    pipeline_id: str | None = None, video_name: str | None = None,
) -> dict:
    """Trộn instrumental + audio gốc mức thấp + TTS -> 1 track final. Tách khỏi
    main()/argparse để CLI và worker dùng chung — logic mix/crossfade giữ nguyên 100%."""
    original_p, instrumental_p, tts_p, transcript_p = (
        Path(original), Path(instrumental), Path(tts), Path(transcript)
    )
    windows_p = Path(windows) if windows else None
    cfg = load_config(config)
    mix_level = original_mix_level if original_mix_level is not None else cfg["original_mix_level"]
    crossfade_ms_v = crossfade_ms if crossfade_ms is not None else cfg["crossfade_ms"]
    speech_only_v = speech_only if speech_only is not None else cfg["speech_only_replace"]
    target_ratio = tts_target_ratio if tts_target_ratio is not None else cfg["tts_target_ratio"]
    base_gain = tts_base_gain if tts_base_gain is not None else cfg["tts_base_gain"]
    gain_min, gain_max = cfg["tts_gain_min"], cfg["tts_gain_max"]
    q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "mix_dialogue_config",
                 "original_mix_level": mix_level, "crossfade_ms": crossfade_ms_v, "speech_only_replace": speech_only_v,
                 "tts_base_gain": base_gain, "tts_target_ratio": target_ratio,
                 "tts_gain_min": gain_min, "tts_gain_max": gain_max})

    for label, path in (
        ("original", original_p), ("instrumental", instrumental_p),
        ("tts", tts_p), ("transcript", transcript_p),
    ):
        if not path.resolve().is_file():
            raise RuntimeError(f"file {label} không tồn tại: {path}")

    start_time = time.time()
    original_arr, sr_o = sf.read(str(original_p), dtype="float32")
    instrumental_arr, sr_i = sf.read(str(instrumental_p), dtype="float32")
    tts_arr, sr_t = sf.read(str(tts_p), dtype="float32")

    # TTS (VieNeu) xuất ở sample rate riêng của engine (thường cao hơn 16k) — resample
    # original/instrumental (chuẩn STT 16k) lên khớp TTS thay vì hạ chất lượng TTS xuống.
    sample_rate = sr_t
    if sr_o != sample_rate:
        original_arr = resample_to(original_arr, sr_o, sample_rate)
    if sr_i != sample_rate:
        instrumental_arr = resample_to(instrumental_arr, sr_i, sample_rate)

    length = max(len(original_arr), len(instrumental_arr), len(tts_arr))
    original_arr = np.pad(original_arr, (0, length - len(original_arr)))
    instrumental_arr = np.pad(instrumental_arr, (0, length - len(instrumental_arr)))
    tts_arr = np.pad(tts_arr, (0, length - len(tts_arr)))
    # Nhân cố định TRƯỚC khi tính gain theo nền — bù case Vieneu tự sinh âm lượng thấp, áp
    # dụng cho MỌI mẫu (kể cả window tính ra gain=1.0 theo nền, vốn không được boost thêm gì
    # trước đây). Peak-normalize cuối hàm vẫn chống clip nếu base_gain đẩy quá 0.99.
    tts_arr = tts_arr * base_gain

    crossfade_n = round(crossfade_ms_v / 1000 * sample_rate)
    mix_segs = load_mix_windows(transcript_p, windows_p)

    if speech_only_v:
        background = original_arr.copy()
        for seg in mix_segs:
            apply_segment_replace(
                background, instrumental_arr, original_arr, seg["start"], seg["end"],
                sample_rate, mix_level, crossfade_n,
            )
            q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "mix_window",
                         "segment_id": seg.get("id"), "start": seg["start"], "end": seg["end"]})
    else:
        background = instrumental_arr + original_arr * mix_level

    # Cộng TTS mức gốc (gain 1.0) trước — giữ nguyên hành vi ngoài mọi speech window (nơi
    # TTS gần như im lặng do segments_cli chỉ ghi audio đúng slot). Sau đó BOOST thêm trong
    # từng speech window theo tỉ lệ RMS (loudness) TTS/nền — trước đây TTS cộng thẳng ở mức
    # do engine sinh ra, không so với nền nên dễ bị nền (instrumental + 15% audio gốc) át đi.
    final = background + tts_arr
    EPS = 1e-6
    for seg in mix_segs:
        start_i = max(0, round(seg["start"] * sample_rate))
        end_i = min(length, round(seg["end"] * sample_rate))
        if end_i <= start_i:
            continue
        bg_win = background[start_i:end_i]
        tts_win = tts_arr[start_i:end_i]
        bg_rms, tts_rms = rms(bg_win), active_rms(tts_win)
        if tts_rms > EPS and bg_rms > EPS:
            gain = min(max(target_ratio * bg_rms / tts_rms, gain_min), gain_max)
        else:
            gain = 1.0
        if gain != 1.0:
            final[start_i:end_i] += tts_win * (gain - 1.0)
        q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "tts_gain",
                     "segment_id": seg.get("id"), "start": seg["start"], "end": seg["end"],
                     "bg_rms": round(bg_rms, 5), "tts_rms": round(tts_rms, 5), "gain": round(gain, 3)})

    peak = float(np.abs(final).max()) if len(final) else 0.0
    if peak > 0.99:
        final = final * (0.99 / peak)

    output_p = Path(output).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)

    loudnorm_enabled = cfg["loudnorm_enabled"]
    loudnorm_stats: dict | None = None
    if loudnorm_enabled:
        # Ghi mix thô ra file tạm trước, loudnorm_pass đọc file tạm -> ghi bản đã chuẩn hoá
        # loudness vào đúng output_p (path cuối cùng dùng làm -a cho edit_cli.py).
        prenorm_p = output_p.with_name(output_p.stem + "_prenorm" + output_p.suffix)
        sf.write(str(prenorm_p), final, sample_rate)
        try:
            loudnorm_stats = loudnorm_pass(
                prenorm_p, output_p, cfg["loudnorm_target_i"], cfg["loudnorm_true_peak"], cfg["loudnorm_lra"],
            )
        finally:
            prenorm_p.unlink(missing_ok=True)
    else:
        sf.write(str(output_p), final, sample_rate)

    q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "mix_dialogue_loudnorm",
                 "enabled": loudnorm_enabled, "target_i": cfg["loudnorm_target_i"],
                 "measured_input_i": loudnorm_stats.get("input_i") if loudnorm_stats else None})

    return {
        "ok": True,
        "original": str(original_p.resolve()),
        "instrumental": str(instrumental_p.resolve()),
        "tts": str(tts_p.resolve()),
        "output": str(output_p),
        "original_mix_level": mix_level,
        "crossfade_ms": crossfade_ms_v,
        "speech_only_replace": speech_only_v,
        "tts_base_gain": base_gain,
        "tts_target_ratio": target_ratio,
        "loudnorm_enabled": loudnorm_enabled,
        "loudnorm_target_i": cfg["loudnorm_target_i"],
        "loudnorm_measured_input_i": loudnorm_stats.get("input_i") if loudnorm_stats else None,
        "elapsed_s": round(time.time() - start_time, 2),
    }


def main() -> int:
    args = parse_args()
    try:
        result = run_mix_dialogue(
            str(args.original), str(args.instrumental), str(args.tts), str(args.transcript),
            str(args.output), args.config, args.original_mix_level, args.crossfade_ms, args.speech_only,
            args.tts_target_ratio, args.tts_base_gain, str(args.windows) if args.windows else None,
        )
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
