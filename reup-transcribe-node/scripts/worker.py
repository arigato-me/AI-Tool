"""Worker GPU — load model 1 lần lúc start (`ensure_vad_loaded`), giữ resident xuyên suốt
nhiều job qua `model_registry.py` (lazy-load/idle-unload không đổi), vòng lặp thuần (không
fork/subprocess per-job) giống mọi node khác trong kiến trúc mới — nhưng ở đây tính "không
fork" quan trọng hơn hẳn các node CPU: nếu fork mỗi job, FunASR/faster-whisper phải load lại
từ đầu mỗi lần, phá vỡ toàn bộ lý do docker_transcribe được tách serve-mode ban đầu."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import redis as redis_lib

import deepgram_backend
import model_registry as models
import pipeline
import queue_lib as q

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "transcribe-worker"

BACKEND = os.environ.get("TRANSCRIBE_BACKEND", "local")
IDLE_TIMEOUT_S = float(os.environ.get("MODEL_IDLE_TIMEOUT_SECONDS", "120"))
GPU_MEMORY_FRACTION = float(os.environ.get("GPU_MEMORY_FRACTION", "0.5"))
DIALOGUE_CONFIG_PATH = os.environ.get("DIALOGUE_CONFIG_PATH", "/config/dialogue.yaml")


def _load_dialogue_config() -> dict:
    default = {"enabled": True, "segment_size": 256}
    try:
        import yaml
        with open(DIALOGUE_CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return {**default, **cfg.get("separate", {})}
    except FileNotFoundError:
        return default


def _idle_unload_loop() -> None:
    while True:
        time.sleep(30)
        models.unload_idle(IDLE_TIMEOUT_S, keep={"vad"})


def _heartbeat_loop(conn: redis_lib.Redis, interval: float = 10.0) -> None:
    """Thread nền riêng — job xử lý xong mới quay lại đầu `while True` gọi heartbeat, nên
    job chạy lâu hơn HEARTBEAT_TTL_S (30s, vd transcribe mode=dialogue chạy cả separate+VAD+STT)
    làm key hết hạn giữa chừng dù worker vẫn đang bận, khiến `/health`/`/nodes/status` tưởng
    nhầm là down. Bug thật gặp: dashboard monitor báo đúng node đang xử lý job là down."""
    while True:
        try:
            q.heartbeat(conn, ROLE)
        except Exception as e:
            # Bug thật gặp: 1 lỗi Redis (network blip/idle-timeout, không cần Redis chết hẳn)
            # ném exception ra khỏi vòng lặp làm CHẾT LUÔN thread này — không ai khởi động lại,
            # nên node báo down vĩnh viễn dù main loop vẫn xử lý job bình thường, tới khi có
            # người restart tay. Nuốt lỗi + lặp tiếp: redis-py tự mở lại kết nối ở lần gọi kế,
            # không cần tự viết logic reconnect.
            print(f"[heartbeat] lỗi ghi (bỏ qua, thử lại sau {interval}s): {e}", file=sys.stderr)
        time.sleep(interval)


def _run_deepgram(input_path: Path, output_path: Path) -> dict:
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    model = os.environ.get("DEEPGRAM_MODEL", "nova-2")
    language = os.environ.get("DEEPGRAM_LANGUAGE", "vi")
    if not api_key:
        raise RuntimeError("thiếu DEEPGRAM_API_KEY")

    result = deepgram_backend.transcribe(input_path, api_key, model, language)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {"language": result["language"], "duration_s": result["duration_s"], "segments": result["segments"]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "input": str(input_path),
        "output": str(output_path),
        "segments": len(result["segments"]),
        "duration_s": result["duration_s"],
        "elapsed_s": result["elapsed_s"],
    }


def process_job(payload: dict) -> dict:
    input_path = Path(payload["input"])
    output_path = Path(payload["output"])
    mode = payload.get("mode", "review")
    align = payload.get("align", False)
    source_lang = payload.get("source_lang", "zh")
    pipeline_id = payload.get("pipeline_id")
    video_name = payload.get("video_name")

    if not input_path.is_file():
        raise RuntimeError(f"file input không tồn tại: {input_path}")
    if mode not in ("review", "dialogue"):
        raise RuntimeError(f"mode không hợp lệ: {mode!r}")
    if mode == "dialogue" and BACKEND != "local":
        raise RuntimeError("mode='dialogue' chỉ hỗ trợ TRANSCRIBE_BACKEND=local")
    if source_lang not in ("zh", "other"):
        raise RuntimeError(f"source_lang không hợp lệ: {source_lang!r} (chỉ nhận 'zh'/'other')")

    if BACKEND == "deepgram":
        return _run_deepgram(input_path, output_path)

    segment_size = _load_dialogue_config().get("segment_size", 256) if mode == "dialogue" else 256
    return pipeline.process(input_path, output_path, align=align, mode=mode, segment_size=segment_size,
                             source_lang=source_lang, pipeline_id=pipeline_id, video_name=video_name)


def _log_retention_loop(interval: float = 3600.0) -> None:
    """Thread nền dọn `/logs/*.jsonl` cũ hơn `LOG_RETENTION_DAYS` (mặc định 2 ngày) — chạy
    độc lập với xử lý job, không phụ thuộc `EVENT_LOG_ENABLED` (dọn nốt log cũ nếu có)."""
    while True:
        q.prune_old_logs()
        time.sleep(interval)


GPU_LOCK_PRIORITY = 1.0  # thấp hơn tts-gpu (0.0) -> tts-gpu luôn được ưu tiên vào trước khi khoá rảnh
GPU_LOCK_LEASE_S = 30.0
GPU_LOCK_RENEW_INTERVAL_S = 10.0


def _gpu_lock_renew_loop(conn: redis_lib.Redis, token: str, stop_event: threading.Event) -> None:
    """Gia hạn lease trong lúc đang giữ khoá GPU — nếu process crash, thread này chết theo,
    lease tự hết hạn tối đa GPU_LOCK_LEASE_S sau đó (không kẹt vĩnh viễn)."""
    while not stop_event.wait(GPU_LOCK_RENEW_INTERVAL_S):
        try:
            q.gpu_lock_renew(conn, token, lease_s=GPU_LOCK_LEASE_S)
        except Exception as e:
            print(f"[gpu_lock] lỗi gia hạn khoá (bỏ qua): {e}", file=sys.stderr)


def _process_job_locked(conn: redis_lib.Redis, payload: dict) -> dict:
    """Bọc `process_job()` bằng khoá GPU dùng chung với reup-tts-gpu-node — chỉ khi
    BACKEND=='local' (Deepgram là API ngoài, không đụng GPU host, không cần khoá — tối ưu hợp
    lý, không phải đánh đổi an toàn lấy tốc độ vì ở đây vốn dĩ không có rủi ro). Nếu giành khoá
    lỗi (mất kết nối Redis...), lỗi lan thẳng ra ngoài — job fail rõ ràng, không bao giờ chạy
    GPU mà thiếu khoá xác nhận."""
    if BACKEND != "local":
        return process_job(payload)
    token = q.gpu_lock_acquire(conn, node="transcribe", priority=GPU_LOCK_PRIORITY,
                                lease_s=GPU_LOCK_LEASE_S)
    stop_event = threading.Event()
    threading.Thread(target=_gpu_lock_renew_loop, args=(conn, token, stop_event), daemon=True).start()
    try:
        return process_job(payload)
    finally:
        # unload_idle(0, ...) buộc nhả NGAY mọi model nặng (whisper/paraformer/align) vừa
        # dùng trong job này — không đợi MODEL_IDLE_TIMEOUT_SECONDS (120s) như trước. Giữ lại
        # "vad" (nhẹ, luôn cần cho job kế tiếp). Đánh đổi: job STT kế tiếp luôn phải load lại
        # whisper/paraformer từ đầu, nhưng job reup-tts-gpu-node tới ngay sau không bị kẹt
        # VRAM bởi 1 model STT ~1.9GB (large-v3) còn "ngồi chờ" hết giờ idle mới nhả.
        models.unload_idle(0, keep={"vad"})
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass
        stop_event.set()
        q.gpu_lock_release(conn, token)


def main() -> None:
    conn = redis_lib.Redis.from_url(REDIS_URL)
    threading.Thread(target=_heartbeat_loop, args=(conn,), daemon=True).start()
    threading.Thread(target=_log_retention_loop, daemon=True).start()

    if BACKEND == "local":
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.set_per_process_memory_fraction(GPU_MEMORY_FRACTION, device=0)
        except ImportError:
            pass
        pipeline.ensure_vad_loaded()
        threading.Thread(target=_idle_unload_loop, daemon=True).start()

    recovered = q.recover_stale_jobs(conn)
    if recovered:
        print(f"[worker] Khôi phục {recovered} job bị kẹt từ lần chạy trước", file=sys.stderr)

    print(f"[worker] Sẵn sàng nhận job (service={q.SERVICE}, backend={BACKEND})", file=sys.stderr)
    while True:
        job_id = q.pop_job(conn, timeout=5)
        if job_id is None:
            continue

        job = q.get_job(conn, job_id)
        if job is None:
            continue
        q.mark_started(conn, job_id)
        payload = job["payload"]
        pipeline_id = payload.get("pipeline_id")
        video_name = payload.get("video_name")
        q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                     "role": ROLE, "event": "job_start", "mode": payload.get("mode")})
        t0 = time.time()
        try:
            result = _process_job_locked(conn, payload)
            q.mark_done(conn, job_id, result)
            q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                         "role": ROLE, "event": "job_done", "elapsed_s": round(time.time() - t0, 1),
                         "result": result})
            print(f"[worker] job {job_id} xong sau {time.time() - t0:.1f}s", file=sys.stderr)
        except Exception as e:
            traceback.print_exc()
            err_text = traceback.format_exc()[-4000:]
            q.mark_failed(conn, job_id, err_text)
            q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                         "role": ROLE, "event": "job_failed", "elapsed_s": round(time.time() - t0, 1),
                         "error": err_text})
            print(f"[worker] job {job_id} lỗi: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
