"""Command `loudnorm` — chuẩn hoá loudness 1 file audio thuần (không mux/mix gì khác), dùng cho
nhánh `mode=audio` của orchestrator (tải nhạc YouTube xong nghe nhỏ hơn hẳn player gốc vì player
tự chuẩn hoá loudness lúc phát, còn file tải về là bản thô chưa qua bước đó). Tái dùng nguyên
`loudnorm_pass()` đã có sẵn cho nhánh dialogue — cùng 1 node, import thẳng, không copy code."""
from __future__ import annotations

from pathlib import Path

from mix_dialogue_cli import loudnorm_pass


def run_loudnorm(input_path: str, output_path: str,
                  target_i: float = -14.0, true_peak: float = -1.0, lra: float = 11.0) -> dict:
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    stats = loudnorm_pass(Path(input_path), out, target_i, true_peak, lra)
    return {"ok": True, "output": output_path, "measured": stats}
