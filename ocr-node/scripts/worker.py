"""Worker OCR — Tier 1 (MarkItDown/PyMuPDF, CPU) chạy KHÔNG giữ khoá GPU; Tier 2 (nhận dạng
ảnh/trang scan qua `ocr_page()` bên dưới) chỉ giữ khoá GPU dùng chung với reup-transcribe-node/
reup-tts-gpu-node ĐÚNG LÚC gọi engine — không giữ khoá suốt cả job (khác 2 node kia, ở đây phần
lớn 1 job "sách" là công việc CPU thuần, chỉ vài trang/ảnh thật sự cần GPU).

Khác `_process_job_locked()` ở reup-transcribe-node (giữ khoá + thread gia hạn nền suốt CẢ job
dài vài phút): mỗi lần giữ khoá ở đây chỉ quanh ĐÚNG 1 lần OCR 1 trang/1 ảnh (vài giây) — không
cần thread gia hạn nền, 1 lần acquire với lease đủ dài (GPU_LOCK_LEASE_S) là đủ, đơn giản hơn
hẳn mà vẫn đúng an toàn (lease tự hết hạn nếu process chết giữa chừng, không kẹt khoá vĩnh viễn).

Load PaddleOCR + VietOCR 1 lần lúc start (model-load-once, giống mọi node GPU khác trong repo)."""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import redis as redis_lib

import queue_lib as q
from document_extractor import run_extract
from ocr_engine import OCREngine

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "ocr-worker"
GPU_MEMORY_FRACTION = float(os.environ.get("GPU_MEMORY_FRACTION", "0.3"))
DEFAULT_LANG = os.environ.get("OCR_LANG", "vi")
VIETOCR_MODEL_NAME = os.environ.get("VIETOCR_MODEL_NAME", "vgg_transformer")
VIETOCR_DEVICE = os.environ.get("VIETOCR_DEVICE", "cuda:0")

GPU_LOCK_PRIORITY = 2.0  # thấp nhất (tts-gpu=0, transcribe=1) — việc nền (chuyển sách->audio),
# không cần latency thấp như 2 node kia, nhường GPU cho cả 2 khi tranh chấp.
GPU_LOCK_LEASE_S = 60.0


def _heartbeat_loop(conn: redis_lib.Redis, interval: float = 10.0) -> None:
    while True:
        try:
            q.heartbeat(conn, ROLE)
        except Exception as e:
            # Bug thật gặp (giống 6 node reup-*-node khác): 1 lỗi Redis (network blip/
            # idle-timeout, không cần Redis chết hẳn) ném exception ra khỏi vòng lặp làm CHẾT
            # LUÔN thread này — không ai khởi động lại, nên node báo down vĩnh viễn dù main loop
            # vẫn xử lý job bình thường, tới khi có người restart tay. Nuốt lỗi + lặp tiếp:
            # redis-py tự mở lại kết nối ở lần gọi kế, không cần tự viết logic reconnect.
            print(f"[heartbeat] lỗi ghi (bỏ qua, thử lại sau {interval}s): {e}", file=sys.stderr)
        time.sleep(interval)


def _log_retention_loop(interval: float = 3600.0) -> None:
    while True:
        q.prune_old_logs()
        time.sleep(interval)


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

    print(f"[worker] Đang load OCR engine (lang={DEFAULT_LANG}) ...", file=sys.stderr)
    engine = OCREngine(lang=DEFAULT_LANG, vietocr_model_name=VIETOCR_MODEL_NAME, vietocr_device=VIETOCR_DEVICE)
    print("[worker] OCR engine sẵn sàng", file=sys.stderr)

    def ocr_page(image_path: str, lang: str) -> list[dict]:
        token = q.gpu_lock_acquire(conn, node="ocr", priority=GPU_LOCK_PRIORITY, lease_s=GPU_LOCK_LEASE_S)
        try:
            return engine.run(image_path, lang=lang)
        finally:
            q.gpu_lock_release(conn, token)

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
                     "role": ROLE, "event": "job_start"})
        t0 = time.time()
        try:
            result = run_extract(
                payload["input"], payload["output"], payload.get("source_lang") or DEFAULT_LANG,
                ocr_page, pipeline_id=pipeline_id, video_name=video_name,
            )
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
