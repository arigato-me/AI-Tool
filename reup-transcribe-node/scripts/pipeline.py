"""Pipeline local: VAD (fsmn-vad) -> route STT theo `source_lang` người dùng chọn thẳng
trên web (Paraformer-large cho zh / faster-whisper cho ngôn ngữ khác — không còn tự đoán
ngôn ngữ, xem lịch sử bug ở transcribe_whisper()) -> punc (ct-punc, nhánh zh)
-> align (WhisperX, opt-in, nhánh khác zh).

Lưu ý timestamp: STT chạy riêng theo từng đoạn VAD nên model trả timestamp tương đối
theo đoạn đó — mọi hàm transcribe_* dưới đây cộng lại offset đoạn (start_s) trước khi
trả về, để segment cuối luôn ở mốc thời gian tuyệt đối theo file gốc (bắt buộc, nếu
không sẽ lệch toàn bộ timeline downstream: translate/tts/subtitle).

XÁC NHẬN CẦN LÀM KHI BUILD THẬT TRÊN REMOTE GPU (không thể test ở môi trường không có
GPU/model weights):
- Đúng API `AutoModel.generate()` của FunASR cho fsmn-vad/paraformer-zh/ct-punc (key trả
  về, đơn vị thời gian ms vs s) — có thể lệch giữa các version FunASR.
- Đúng chữ ký `WhisperModel.transcribe()` của faster-whisper bản đang cài.
- Đúng API `whisperx.load_align_model()`/`whisperx.align()` của bản WhisperX đang cài, và
  việc WhisperX có model align chất lượng tốt cho ngôn ngữ đang route qua nhánh whisper
  (tiếng Việt/Nhật có thể không có align model chính thức — cần kiểm tra, fallback bỏ qua
  align nếu ngôn ngữ không hỗ trợ thay vì crash).
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

import model_registry as models
import queue_lib as q
from audio_utils import extract_audio

SAMPLE_RATE = 16000
ZH_PREFIX = "zh"
MAX_SEGMENT_SECONDS = 20.0
# VAD (fsmn-vad) đôi khi gộp thoại liên tục dài (>60s) thành 1 segment duy nhất khi không
# tìm được khoảng lặng rõ — đã gây 2 lỗi thật: TTS tràn slot (segments_cli.py) và mov_text
# (soft sub trong docker_ffmpeg-edit) bị lỗi/cắt video khi 1 cue quá dài (>1800 ký tự).
# Chặn ở đây (thay vì sửa từng chỗ dùng) để áp dụng cho MỌI downstream: TTS, subtitle,
# translate — cả nhánh review lẫn dialogue.

CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "/cache")
GPU_DEVICE = "cuda" if os.environ.get("TRANSCRIBE_DEVICE", "cuda") == "cuda" else "cpu"


def _load_vad():
    from funasr import AutoModel
    return AutoModel(model="fsmn-vad", cache_dir=f"{CACHE_DIR}/funasr", device=GPU_DEVICE, disable_update=True)


def _load_paraformer():
    from funasr import AutoModel
    return AutoModel(model="paraformer-zh", cache_dir=f"{CACHE_DIR}/funasr", device=GPU_DEVICE, disable_update=True)


def _load_punc():
    # ct-punc chỉ xử lý text ngắn (không phải audio) — chạy CPU để tránh tranh VRAM
    # với Paraformer/whisper đang resident (đã gây CUDA OOM thực tế trên GPU 4GB dùng
    # chung với docker_vieneu-tts_gpu/tts-node khi cả 3 model cùng load lên GPU).
    from funasr import AutoModel
    return AutoModel(model="ct-punc", cache_dir=f"{CACHE_DIR}/funasr", device="cpu", disable_update=True)


def _load_whisper():
    from faster_whisper import WhisperModel
    return WhisperModel(
        "large-v3", device=GPU_DEVICE, compute_type="int8",
        download_root=f"{CACHE_DIR}/faster-whisper",
    )


def _load_align(lang: str):
    import whisperx
    return whisperx.load_align_model(language_code=lang, device=GPU_DEVICE)


def ensure_vad_loaded() -> None:
    models.get("vad", _load_vad)


def run_vad(wav_path: Path) -> list[tuple[float, float]]:
    vad = models.get("vad", _load_vad)
    # max_single_segment_time (ms): tham số gốc của FunASR VAD để tự chặn độ dài segment —
    # XÁC NHẬN CẦN LÀM KHI BUILD: đúng tên/đơn vị tham số này theo bản FunASR đang cài (có
    # thể không tồn tại hoặc tên khác). Dù đúng hay sai, `_split_long_ranges()` bên dưới
    # vẫn là lưới an toàn cuối cùng — không phụ thuộc hoàn toàn vào tham số này.
    res = vad.generate(input=str(wav_path), max_single_segment_time=int(MAX_SEGMENT_SECONDS * 1000))
    raw_segments = res[0].get("value", [])
    ranges = [(beg / 1000.0, end / 1000.0) for beg, end in raw_segments]
    return _split_long_ranges(ranges, MAX_SEGMENT_SECONDS)


def _split_long_ranges(ranges: list[tuple[float, float]], max_seconds: float) -> list[tuple[float, float]]:
    """Lưới an toàn cuối: nếu VAD vẫn trả về đoạn dài hơn max_seconds (tham số native ở
    trên không có tác dụng/sai tên), tự chia đều thành các đoạn con bằng nhau. Chấp nhận
    đánh đổi: có thể cắt ngang từ/câu ở ranh giới chia — vẫn tốt hơn nhiều so với 1 segment
    60s+ gây tràn TTS + hỏng mov_text."""
    result = []
    for start_s, end_s in ranges:
        duration = end_s - start_s
        if duration <= max_seconds:
            result.append((start_s, end_s))
            continue
        n_chunks = math.ceil(duration / max_seconds)
        chunk_len = duration / n_chunks
        for i in range(n_chunks):
            result.append((start_s + i * chunk_len, start_s + (i + 1) * chunk_len))
    return result


def _slice_audio(wav_array: np.ndarray, start_s: float, end_s: float) -> np.ndarray:
    start_i = max(0, round(start_s * SAMPLE_RATE))
    end_i = min(len(wav_array), round(end_s * SAMPLE_RATE))
    return wav_array[start_i:end_i]


def write_tmp_wav(wav_array: np.ndarray, sample_rate: int) -> Path:
    """Ghi 1 mảng numpy ra file wav tạm — dùng khi cần đưa 1 đoạn audio đã cắt qua lại
    cho 1 model FunASR (chỉ nhận path, không nhận mảng cho vài API như VAD)."""
    fd, tmp_path_str = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    sf.write(str(tmp_path), wav_array, sample_rate)
    return tmp_path


def transcribe_paraformer(wav_array: np.ndarray, voiced_ranges: list[tuple[float, float]]) -> list[dict]:
    engine = models.get("paraformer", _load_paraformer)
    segments = []
    for start_s, end_s in voiced_ranges:
        clip = _slice_audio(wav_array, start_s, end_s)
        if len(clip) == 0:
            continue
        res = engine.generate(input=clip)
        text = (res[0].get("text") or "").strip()
        if not text:
            continue
        segments.append({"start": round(start_s, 2), "end": round(end_s, 2), "text": text})
    return segments


def transcribe_whisper(wav_array: np.ndarray, voiced_ranges: list[tuple[float, float]],
                        lang: str | None) -> list[dict]:
    """`lang=None` (source_lang="other") để whisper tự nhận diện ngôn ngữ NGAY TRÊN từng
    đoạn voiced đang xử lý — đáng tin hơn hẳn 1 lần đoán cố định từ đoạn voiced đầu tiên
    (tiny-model lang-id cũ đã bỏ, dễ đoán sai nếu đoạn đầu quá ngắn/nhiễu, xem lịch sử job
    bị route nhầm zh->ko)."""
    engine = models.get("whisper", _load_whisper)
    segments = []
    for start_s, end_s in voiced_ranges:
        clip = _slice_audio(wav_array, start_s, end_s)
        if len(clip) == 0:
            continue
        whisper_segments, _ = engine.transcribe(clip, language=lang)
        text = "".join(s.text for s in whisper_segments).strip()
        if not text:
            continue
        segments.append({"start": round(start_s, 2), "end": round(end_s, 2), "text": text})
    return segments


def restore_punctuation(segments: list[dict]) -> list[dict]:
    punc = models.get("punc", _load_punc)
    for seg in segments:
        res = punc.generate(input=seg["text"])
        seg["text"] = (res[0].get("text") or seg["text"]).strip()
    return segments


def align_words(wav_path: Path, segments: list[dict], lang: str) -> list[dict]:
    import whisperx

    try:
        align_model, metadata = models.get(f"align:{lang}", lambda: _load_align(lang))
    except Exception:
        # Không có align model chính thức cho ngôn ngữ này (vd hiếm gặp với vi/ja) —
        # bỏ qua align thay vì làm hỏng cả job, giữ nguyên segment không có "words".
        return segments

    audio = whisperx.load_audio(str(wav_path))
    ws_segments = [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in segments]
    aligned = whisperx.align(ws_segments, align_model, metadata, audio, GPU_DEVICE)
    for seg, aligned_seg in zip(segments, aligned.get("segments", [])):
        seg["words"] = [
            {
                "word": w.get("word"),
                "start": round(w.get("start", seg["start"]), 2),
                "end": round(w.get("end", seg["end"]), 2),
            }
            for w in aligned_seg.get("words", [])
            if w.get("start") is not None
        ]
    return segments


def prepare_audio(input_path: Path, mode: str, segment_size: int = 256) -> dict:
    """Trích audio gốc; nếu mode="dialogue" tách thêm vocals/instrumental (2 candidate).
    "clean_wav" = input cho VAD/STT (= audio gốc nếu review, = vocals.wav nếu dialogue —
    tín hiệu sạch hơn để định biên câu nói khi có nhiều nền)."""
    fd, tmp_wav_str = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    original_wav = Path(tmp_wav_str)
    extract_audio(input_path, original_wav)

    if mode != "dialogue":
        return {"clean_wav": original_wav, "original_wav": None, "instrumental_mdx": None, "instrumental_demucs": None}

    import separator as separator_mod
    vocals_path, instrumental_mdx_path, instrumental_demucs_path = separator_mod.separate_audio(
        original_wav, segment_size
    )
    return {
        "clean_wav": vocals_path,
        "original_wav": original_wav,
        "instrumental_mdx": instrumental_mdx_path,
        "instrumental_demucs": instrumental_demucs_path,
    }


def process(
    input_path: Path, output_path: Path, align: bool = False,
    mode: str = "review", segment_size: int = 256,
    source_lang: str = ZH_PREFIX,
    pipeline_id: str | None = None, video_name: str | None = None,
) -> dict:
    start = time.time()
    prep = prepare_audio(input_path, mode, segment_size)
    tmp_wav = prep["clean_wav"]
    final_instrumental = None
    try:
        wav_array, sr = sf.read(str(tmp_wav), dtype="float32")
        if sr != SAMPLE_RATE:
            raise RuntimeError(f"ffmpeg trả sample rate {sr}, kỳ vọng {SAMPLE_RATE}")
        duration_s = len(wav_array) / SAMPLE_RATE

        voiced_ranges = run_vad(tmp_wav)
        # Ngôn ngữ do người dùng chọn thẳng trên web (không còn tự đoán — tiny-model lang-id
        # cũ chỉ xét đoạn voiced ĐẦU TIÊN, dễ đoán sai nếu đoạn đó quá ngắn/nhiễu, đã gặp thật
        # 1 video Douyin/tiếng Trung bị đoán ra "ko" vì đoạn đầu chỉ dài 1.6s).
        engine = "paraformer" if source_lang == ZH_PREFIX else "whisper"
        q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                     "event": "engine_selected", "source_lang": source_lang, "engine": engine})

        if engine == "paraformer":
            raw_segments = transcribe_paraformer(wav_array, voiced_ranges)
            raw_segments = restore_punctuation(raw_segments)
        else:
            # lang=None -> whisper tự nhận diện ngôn ngữ trên CHÍNH đoạn đang xử lý (khác
            # source_lang="other", vốn chỉ là lựa chọn engine, không phải mã ngôn ngữ cụ thể).
            raw_segments = transcribe_whisper(wav_array, voiced_ranges, lang=None)
            if align:
                raw_segments = align_words(tmp_wav, raw_segments, source_lang)

        if mode == "dialogue":
            import residual_qc

            instrumental_mdx_arr, _ = sf.read(str(prep["instrumental_mdx"]), dtype="float32")
            instrumental_demucs_arr, _ = sf.read(str(prep["instrumental_demucs"]), dtype="float32")
            final_instrumental = instrumental_mdx_arr.copy()
            for idx, seg in enumerate(raw_segments):
                chosen, score, _ = residual_qc.choose_best_instrumental(
                    instrumental_mdx_arr, instrumental_demucs_arr, seg["start"], seg["end"],
                )
                start_i = round(seg["start"] * SAMPLE_RATE)
                end_i = start_i + len(chosen)
                final_instrumental[start_i:end_i] = chosen
                seg["residual_risk"] = round(score, 3)
                q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                             "event": "residual_qc", "segment_id": idx,
                             "start": seg["start"], "end": seg["end"], "residual_risk": round(score, 3)})

        segments = []
        for idx, seg in enumerate(raw_segments):
            entry = {"id": idx, "start": seg["start"], "end": seg["end"], "text": seg["text"]}
            if "words" in seg:
                entry["words"] = seg["words"]
            if "residual_risk" in seg:
                entry["residual_risk"] = seg["residual_risk"]
            segments.append(entry)
    finally:
        tmp_wav.unlink(missing_ok=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {"language": source_lang, "duration_s": round(duration_s, 2), "segments": segments},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )

    result = {
        "ok": True,
        "input": str(input_path),
        "output": str(output_path),
        "segments": len(segments),
        "duration_s": round(duration_s, 2),
        "language": source_lang,
        "engine": engine,
        "mode": mode,
        "elapsed_s": round(time.time() - start, 2),
    }

    if mode == "dialogue":
        import shutil

        instrumental_out = output_path.with_name(output_path.stem + "_instrumental.wav")
        original_out = output_path.with_name(output_path.stem + "_original.wav")
        sf.write(str(instrumental_out), final_instrumental, SAMPLE_RATE)
        shutil.copyfile(prep["original_wav"], original_out)
        prep["original_wav"].unlink(missing_ok=True)
        result["instrumental"] = str(instrumental_out)
        result["original"] = str(original_out)
        flagged = sum(1 for s in segments if s.get("residual_risk", 0.0) > 0.5)
        result["residual_flagged"] = flagged
        result["residual_flagged_ratio"] = round(flagged / len(segments), 3) if segments else 0.0

    return result
