"""Lazy-load + idle-unload cho các model nặng (VAD/Paraformer/faster-whisper/ct-punc/align).

GPU trên host chỉ có 4GB, chia sẻ với docker_vieneu-tts_gpu — không giữ mọi model sống
vĩnh viễn. VAD nhẹ nên luôn resident; các model STT/punc/align chỉ load khi cần và bị
giải phóng sau một khoảng thời gian không dùng (xem unload_idle(), gọi định kỳ từ server.py).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("transcribe.models")

_lock = threading.Lock()
_last_used: dict[str, float] = {}
_instances: dict[str, object] = {}


def get(name: str, loader: Callable[[], object]) -> object:
    """Trả instance model `name`; load lần đầu qua loader() nếu chưa có, cache lại.

    Nếu loader() lỗi giữa chừng (vd CUDA OOM lúc `.to(device)`), tensor đã cấp phát dở
    không tự trả về driver — caching allocator của PyTorch chỉ giải phóng thật khi
    `empty_cache()` được gọi trong CHÍNH process đó. Trước đây chỉ `unload()`/`unload_idle()`
    gọi `empty_cache()`, nên 1 lần load lỗi để lại VRAM bị "kẹt" (không tăng usage của model
    nào cả, nhưng cũng không nhả ra) — mỗi lần thử lại thất bại làm usage tăng dần
    (874MiB -> 1086MiB quan sát thấy thật giữa 2 lần retry liên tiếp), cuối cùng làm cả GPU
    hết chỗ dù model chưa từng load thành công."""
    with _lock:
        if name not in _instances:
            logger.info("Loading model %s", name)
            try:
                _instances[name] = loader()
            except Exception:
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except ImportError:
                    pass
                raise
        _last_used[name] = time.time()
        return _instances[name]


def check_vram(min_free_mb: float, context: str = "") -> None:
    """Kiểm tra VRAM free TRƯỚC khi load model nặng — fail sớm, rõ ràng, thay vì để torch OOM
    giữa chừng lúc forward() rồi lỗi lan sang tầng sau đó khó hiểu (đã gặp thật: OOM trong
    htdemucs_ft khiến output_files rỗng, code tìm file "vocal" bị StopIteration — traceback
    thật của OOM bị chôn phía trên, người dùng chỉ thấy "lỗi: " trống trơn). Không chặn gì nếu
    máy không có CUDA (dev/test trên CPU)."""
    try:
        import torch
        if not torch.cuda.is_available():
            return
        free_bytes, total_bytes = torch.cuda.mem_get_info()
        free_mb = free_bytes / (1024 * 1024)
        if free_mb < min_free_mb:
            suffix = f" ({context})" if context else ""
            raise RuntimeError(
                f"Không đủ VRAM để load model{suffix}: cần tối thiểu ~{min_free_mb:.0f}MiB, "
                f"hiện chỉ free {free_mb:.0f}MiB/{total_bytes / (1024 * 1024):.0f}MiB. "
                "Giải phóng bớt tiến trình đang dùng GPU (kể cả tenant khác trên host) rồi thử lại."
            )
    except ImportError:
        pass


def resident_names() -> list[str]:
    with _lock:
        return list(_instances.keys())


def unload(name: str) -> None:
    """Giải phóng ngay 1 model cụ thể (không đợi idle-timeout) — dùng cho các model
    dùng 1 lần/job như separator (MDX-Net/Demucs), tránh 2 model nặng cùng resident."""
    unloaded = False
    with _lock:
        if name in _instances:
            logger.info("Unloading model %s", name)
            del _instances[name]
            _last_used.pop(name, None)
            unloaded = True
    if unloaded:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


def unload_idle(idle_timeout_s: float, keep: set[str]) -> None:
    """Giải phóng model không dùng quá idle_timeout_s giây, trừ các tên trong `keep`."""
    now = time.time()
    unloaded = False
    with _lock:
        for name in list(_instances.keys()):
            if name in keep:
                continue
            if now - _last_used.get(name, now) >= idle_timeout_s:
                logger.info("Unloading idle model %s", name)
                del _instances[name]
                _last_used.pop(name, None)
                unloaded = True
    if unloaded:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
