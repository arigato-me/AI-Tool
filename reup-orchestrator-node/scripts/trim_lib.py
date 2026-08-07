"""Cắt đầu/đuôi 1 file mp3/mp4 đã có sẵn (output đã 'finished' của pipeline) bằng `-c copy`
(stream copy, không re-encode) — cắt tức thời, 0 tốn CPU thêm, không ảnh hưởng service khác.
mp3 không có khái niệm keyframe/GOP nên cắt chính xác tuyệt đối tại mọi mốc thời gian. Video
(mp4/webm/mkv) CÓ keyframe/GOP — stream copy chỉ cắt được tại keyframe gần nhất, điểm cắt thực
tế có thể lệch vài giây so với start/end yêu cầu tuỳ khoảng cách keyframe của video gốc (đánh đổi
đã chốt: chấp nhận lệch để đổi lấy tốc độ/an toàn — cắt frame-chính-xác cần re-encode, tốn CPU
đáng kể tuỳ độ dài/độ phân giải, không đáng cho nhu cầu cắt nhanh đầu/đuôi này).

Chạy trực tiếp trong `api` (không qua job queue của worker) vì đây là 1 lệnh ffmpeg cục bộ,
nhanh (stream copy, không re-encode), không phải gọi HTTP sang node khác — xem lý do mount
`/outputs` của api đổi sang rw trong docker-compose.yml.
"""
from __future__ import annotations

import subprocess


class TrimError(RuntimeError):
    pass


def _to_seconds(value: str) -> float:
    s = value.strip()
    if ":" not in s:
        return float(s)
    parts = [float(p) for p in s.split(":")]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def trim_media(input_path: str, output_path: str, start: str | None, end: str | None) -> dict:
    """start/end: giây (số) hoặc "HH:MM:SS"/"MM:SS". Chỉ cần 1 trong 2 (cắt đầu HOẶC cắt đuôi),
    có thể truyền cả 2 (cắt cả 2 đầu).

    Luôn dùng `-t` (duration, đo từ điểm bắt đầu ghi ra) thay vì `-to` (mốc thời gian tuyệt đối)
    khi có `-ss` — gotcha kinh điển của ffmpeg: `-to` kết hợp `-ss` (input option) đo theo
    timeline file GỐC (trước khi seek), không phải theo điểm đã seek tới, dễ cắt sai đoạn nếu
    không để ý. `-t` không có nhập nhằng đó."""
    if not start and not end:
        raise TrimError("cần ít nhất 1 trong 2: start hoặc end")

    cmd = ["ffmpeg", "-y"]
    duration: float | None = None
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", input_path]
    if end:
        end_s = _to_seconds(end)
        start_s = _to_seconds(start) if start else 0.0
        duration = end_s - start_s
        if duration <= 0:
            raise TrimError(f"end ({end}) phải sau start ({start or 0})")
        cmd += ["-t", str(duration)]
    cmd += ["-c", "copy", output_path]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise TrimError(proc.stderr[-2000:] or f"ffmpeg thoát mã {proc.returncode}")
    return {"ok": True, "output": output_path, "start": start, "end": end, "duration_s": duration}
