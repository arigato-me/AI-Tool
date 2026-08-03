"""Stage separate (chỉ dùng khi mode="dialogue"): tách audio gốc thành vocals (tiếng nói)
+ instrumental (nền: nhạc/noise/tiếng môi trường), dùng package `audio-separator`.

Chạy 2 model (MDX-Net Inst HQ 3 làm primary, htdemucs_ft làm ensemble/đối chiếu) — KHÔNG
resident đồng thời: load model này, chạy, evict ngay, rồi mới load model kia — tránh lặp
lại CUDA OOM đã gặp khi nhiều model cùng nằm trên GPU 4GB dùng chung với
docker_vieneu-tts_gpu (xem model_registry.unload()).

XÁC NHẬN CẦN LÀM KHI BUILD THẬT (không thể test ở môi trường không có GPU): đúng chữ ký
`Separator.load_model()`/`separate()` của package `audio-separator` bản đang cài, và đúng
tên model file cho MDX-Net Inst HQ 3 / htdemucs_ft (có thể lệch giữa các version, hoặc cần
tải danh sách model trước qua `list_supported_model_files()`)."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import soundfile as sf

import model_registry as models

CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "/cache")
GPU_DEVICE = "cuda" if os.environ.get("TRANSCRIBE_DEVICE", "cuda") == "cuda" else "cpu"
OUTPUT_DIR = f"{CACHE_DIR}/separator_out"

MDX_MODEL_FILE = os.environ.get("SEPARATE_MODEL_PRIMARY", "UVR-MDX-NET-Inst_HQ_3.onnx")
DEMUCS_MODEL_FILE = os.environ.get("SEPARATE_MODEL_FALLBACK", "htdemucs_ft.yaml")

# Ngưỡng tối thiểu quan sát thực tế trên GPU 4GB dùng chung: lần fail thật chỉ còn 363MiB free
# (CUDA OOM giữa chừng), lần retry thành công có 1889MiB free — chọn mốc giữa, bảo thủ, để fail
# sớm rõ ràng thay vì OOM mơ hồ giữa chừng forward(). Tunable qua env nếu GPU/model đổi.
SEPARATE_MIN_FREE_MB = float(os.environ.get("SEPARATE_MIN_FREE_MB", "1024"))


def _resolve(filename: str) -> Path:
    """`Separator.separate()` trả basename tương đối theo `output_dir`, không phải path
    tuyệt đối — phải tự join lại, nếu không `sf.read`/`Path` sẽ tìm nhầm theo cwd."""
    path = Path(filename)
    return path if path.is_absolute() else Path(OUTPUT_DIR) / path.name


def _new_separator(segment_size: int):
    # audio_separator.Separator không có tham số use_cuda — tự phát hiện GPU qua
    # torch/onnxruntime cài sẵn. sample_rate=16000 để khớp SAMPLE_RATE của pipeline.py
    # (mặc định package là 44100, lệch với chuẩn 16k dùng xuyên suốt VAD/STT).
    from audio_separator.separator import Separator
    return Separator(
        output_dir=f"{CACHE_DIR}/separator_out",
        model_file_dir=f"{CACHE_DIR}/audio-separator",
        sample_rate=16000,
        mdx_params={"segment_size": segment_size, "hop_length": 1024, "overlap": 0.25, "batch_size": 1, "enable_denoise": False},
    )


def _read_mono(path: Path) -> tuple[np.ndarray, int]:
    """audio-separator xuất stereo dù đã set sample_rate=16000 — toàn bộ pipeline (VAD/STT)
    giả định mono 1D giống ffmpeg -ac 1, downmix ngay khi đọc để tránh shape sai lệch
    (đã gặp: faster-whisper cố tạo feature 3D vì nhận nhầm mảng 2D)."""
    arr, sr = sf.read(str(path), dtype="float32")
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    return arr, sr


def _write_mono(path: Path, arr: np.ndarray, sample_rate: int) -> Path:
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    sf.write(str(path), arr, sample_rate)
    return path


def _combine_non_vocal_stems(output_files: list[str], registry_key: str) -> Path:
    """Model nhiều stem (vd Demucs htdemucs_ft: Vocals/Drums/Bass/Other) không xuất sẵn 1
    file "instrumental" gộp như MDX-Net Inst HQ 3 (2-stem) — tự cộng dồn mọi stem không
    phải vocal thành 1 file duy nhất (mono)."""
    non_vocal_files = [_resolve(f) for f in output_files if "vocal" not in Path(f).name.lower()]
    combined: np.ndarray | None = None
    sample_rate = 16000
    for f in non_vocal_files:
        arr, sample_rate = _read_mono(f)
        if combined is None:
            combined = arr.copy()
        elif len(arr) >= len(combined):
            combined = np.pad(combined, (0, len(arr) - len(combined)))
            combined += arr
        else:
            combined[: len(arr)] += arr

    out_path = Path(OUTPUT_DIR) / f"{registry_key}_instrumental_combined.wav"
    return _write_mono(out_path, combined, sample_rate)


def _run_one(model_file: str, wav_path: Path, segment_size: int, registry_key: str) -> tuple[Path, Path]:
    """Load 1 model, tách, evict ngay sau khi dùng (không resident) — trả (vocals, instrumental)."""
    def _load():
        models.check_vram(SEPARATE_MIN_FREE_MB, context=f"tách audio ({registry_key})")
        sep = _new_separator(segment_size)
        sep.load_model(model_filename=model_file)
        return sep

    sep = models.get(registry_key, _load)
    try:
        output_files = sep.separate(str(wav_path))
    finally:
        models.unload(registry_key)

    vocal_files = [f for f in output_files if "vocal" in Path(f).name.lower()]
    if not vocal_files:
        raise RuntimeError(
            f"model '{model_file}' không sinh ra file vocals (output_files={output_files!r}) — "
            "thường do CUDA OOM giữa chừng lúc tách audio (kiểm tra VRAM trống trên GPU); "
            "log container này nên có traceback torch.OutOfMemoryError ngay phía trên nếu đúng vậy."
        )
    vocals_path = _resolve(vocal_files[0])
    arr, sr = _read_mono(vocals_path)
    _write_mono(vocals_path, arr, sr)

    instrumental_candidates = [f for f in output_files if "instrument" in Path(f).name.lower()]
    if instrumental_candidates:
        instrumental_path = _resolve(instrumental_candidates[0])
        arr, sr = _read_mono(instrumental_path)
        _write_mono(instrumental_path, arr, sr)
    else:
        instrumental_path = _combine_non_vocal_stems(output_files, registry_key)
    return vocals_path, instrumental_path


def separate_audio(wav_path: Path, segment_size: int = 256) -> tuple[Path, Path, Path]:
    """Trả (vocals_path, instrumental_mdx_path, instrumental_demucs_path).

    vocals lấy từ model primary (MDX-Net) — dùng làm input sạch cho VAD/STT thay vì audio
    gốc lẫn nền. 2 candidate instrumental để residual_qc.py chọn theo từng đoạn VAD-speech
    (xem pipeline.py: nhánh dialogue), không phải chọn nguyên track."""
    vocals_path, instrumental_mdx_path = _run_one(MDX_MODEL_FILE, wav_path, segment_size, "separator_mdx")
    _, instrumental_demucs_path = _run_one(DEMUCS_MODEL_FILE, wav_path, segment_size, "separator_demucs")
    return vocals_path, instrumental_mdx_path, instrumental_demucs_path
