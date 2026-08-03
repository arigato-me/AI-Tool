"""Kiểm tra chất lượng tách nền (mode="dialogue"): phát hiện dư âm giọng nói còn sót
trong track instrumental, tái dùng chính VAD model đang có sẵn trong model_registry
("vad") thay vì thêm model mới — chạy VAD trên đúng cửa sổ đã biết là có speech gốc;
lẽ ra ở đó không nên còn giọng, nếu VAD vẫn phát hiện tiếng nói -> nghi dư âm."""
from __future__ import annotations

import numpy as np

import model_registry as models
import pipeline

SAMPLE_RATE = pipeline.SAMPLE_RATE


def score_residual(instrumental_array: np.ndarray, start_s: float, end_s: float) -> float:
    """Trả điểm 0-1: tỉ lệ thời lượng cửa sổ [start_s, end_s] mà VAD vẫn phát hiện tiếng nói
    trong track instrumental (lẽ ra phải im lặng-giọng-người ở đó). Càng cao càng nghi dư âm."""
    clip = pipeline._slice_audio(instrumental_array, start_s, end_s)
    window_s = end_s - start_s
    if window_s <= 0 or len(clip) == 0:
        return 0.0

    vad = models.get("vad", pipeline._load_vad)
    tmp_path = pipeline.write_tmp_wav(clip, SAMPLE_RATE)
    try:
        res = vad.generate(input=str(tmp_path))
        raw_segments = res[0].get("value", [])
    finally:
        tmp_path.unlink(missing_ok=True)

    voiced_s = sum((end - beg) / 1000.0 for beg, end in raw_segments)
    return min(1.0, voiced_s / window_s)


def choose_best_instrumental(
    instrumental_mdx: np.ndarray, instrumental_demucs: np.ndarray, start_s: float, end_s: float,
) -> tuple[np.ndarray, float, str]:
    """So 2 candidate instrumental trong đúng cửa sổ [start_s, end_s], trả (mảng đã chọn,
    residual_risk của candidate đó, tên model đã chọn)."""
    score_mdx = score_residual(instrumental_mdx, start_s, end_s)
    score_demucs = score_residual(instrumental_demucs, start_s, end_s)
    if score_demucs < score_mdx:
        return pipeline._slice_audio(instrumental_demucs, start_s, end_s), score_demucs, "htdemucs_ft"
    return pipeline._slice_audio(instrumental_mdx, start_s, end_s), score_mdx, "mdx_inst_hq3"
