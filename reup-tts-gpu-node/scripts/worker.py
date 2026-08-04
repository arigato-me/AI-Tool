"""Worker GPU — load Vieneu() 1 lần lúc start, giữ resident xuyên suốt nhiều job (đúng
pattern tts-node đã review: "1 model load 1 lần, dùng lại nhiều job" — khác hẳn cách CLI
one-shot cũ tự tạo Vieneu() mới mỗi lần `docker compose run --rm ... tts/segments`)."""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import redis as redis_lib
from vieneu import Vieneu

import queue_lib as q
from tts_cli import list_voices_dict, run_tts
from segments_cli import run_segments

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "tts-gpu-worker"
GPU_MEMORY_FRACTION = float(os.environ.get("GPU_MEMORY_FRACTION", "0.5"))
DEFAULT_VOICE = os.environ.get("VIENEU_VOICE")
DEFAULT_STYLE = os.environ.get("VIENEU_STYLE", "tu_nhien")


def process_job(cmd: str, params: dict, tts: Vieneu, pipeline_id: str | None = None, video_name: str | None = None) -> dict:
    voice = params.get("voice") or DEFAULT_VOICE
    style = params.get("style") or DEFAULT_STYLE
    ref_audio = params.get("ref_audio")

    if cmd == "tts":
        return run_tts(params["input_path"], params["output_path"], voice, style, tts=tts, ref_audio=ref_audio)
    if cmd == "segments":
        return run_segments(params["input_path"], params["output_path"], voice, style, tts=tts, ref_audio=ref_audio,
                             pipeline_id=pipeline_id, video_name=video_name)
    if cmd == "list-voices":
        return list_voices_dict(tts=tts)
    raise RuntimeError(f"cmd không hợp lệ: {cmd!r}")


def _heartbeat_loop(conn: redis_lib.Redis, interval: float = 10.0) -> None:
    """Thread nền riêng — job xử lý xong mới quay lại đầu `while True` gọi heartbeat, nên
    job chạy lâu hơn HEARTBEAT_TTL_S (30s, vd segments 85 câu từng mất ~90-160s) làm key hết
    hạn giữa chừng dù worker vẫn đang bận, khiến `/health`/`/nodes/status` tưởng nhầm là down.
    Bug thật gặp: dashboard monitor báo đúng node đang xử lý job là down."""
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


def _log_retention_loop(interval: float = 3600.0) -> None:
    """Thread nền dọn `/logs/*.jsonl` cũ hơn `LOG_RETENTION_DAYS` (mặc định 2 ngày) — chạy
    độc lập với xử lý job, không phụ thuộc `EVENT_LOG_ENABLED` (dọn nốt log cũ nếu có)."""
    while True:
        q.prune_old_logs()
        time.sleep(interval)


GPU_LOCK_PRIORITY = 0.0  # cao hơn transcribe (1.0) -> tts-gpu luôn được ưu tiên vào trước khi khoá rảnh
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


def _process_job_locked(conn: redis_lib.Redis, cmd: str, params: dict, tts: Vieneu,
                         pipeline_id: str | None, video_name: str | None) -> dict:
    """Bọc `process_job()` bằng khoá GPU dùng chung với reup-transcribe-node. Nếu giành khoá lỗi
    (mất kết nối Redis...), lỗi lan thẳng ra ngoài — job fail rõ ràng, không bao giờ chạy GPU mà
    thiếu khoá xác nhận."""
    token = q.gpu_lock_acquire(conn, node="tts-gpu", priority=GPU_LOCK_PRIORITY,
                                lease_s=GPU_LOCK_LEASE_S)
    stop_event = threading.Event()
    threading.Thread(target=_gpu_lock_renew_loop, args=(conn, token, stop_event), daemon=True).start()
    try:
        return process_job(cmd, params, tts, pipeline_id=pipeline_id, video_name=video_name)
    finally:
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

    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.set_per_process_memory_fraction(GPU_MEMORY_FRACTION, device=0)
    except ImportError:
        pass

    print("[worker] Đang load Vieneu()...", file=sys.stderr)
    tts = Vieneu()
    print("[worker] Model sẵn sàng, giữ resident xuyên suốt job", file=sys.stderr)

    recovered = q.recover_stale_jobs(conn)
    if recovered:
        print(f"[worker] Khôi phục {recovered} job bị kẹt từ lần chạy trước", file=sys.stderr)

    print(f"[worker] Sẵn sàng nhận job (service={q.SERVICE})", file=sys.stderr)
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
                     "role": ROLE, "event": "job_start", "cmd": payload.get("cmd")})
        t0 = time.time()
        try:
            result = _process_job_locked(conn, payload["cmd"], payload.get("params", {}), tts,
                                          pipeline_id, video_name)
            q.mark_done(conn, job_id, result)
            q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                         "role": ROLE, "event": "job_done", "cmd": payload.get("cmd"),
                         "elapsed_s": round(time.time() - t0, 1), "result": result})
            print(f"[worker] job {job_id} ({payload['cmd']}) xong sau {time.time() - t0:.1f}s", file=sys.stderr)
        except Exception as e:
            traceback.print_exc()
            err_text = traceback.format_exc()[-4000:]
            q.mark_failed(conn, job_id, err_text)
            q.log_event({"job_id": job_id, "pipeline_id": pipeline_id, "video_name": video_name,
                         "role": ROLE, "event": "job_failed", "cmd": payload.get("cmd"),
                         "elapsed_s": round(time.time() - t0, 1), "error": err_text})
            print(f"[worker] job {job_id} ({payload.get('cmd')}) lỗi: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
