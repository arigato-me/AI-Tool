"""Worker orchestrator — pipeline hoá theo tầng: nhiều pipeline (video) chạy đồng thời trong
`ThreadPoolExecutor`, mỗi video vẫn đi tuần tự đúng 5 bước như cũ (`run_pipeline()` không đổi),
nhưng KHÁC video có thể ở KHÁC bước cùng lúc (vd video 2 đang `ytdlp` trong khi video 1 còn ở
`transcribe`) — thay vì chặn cả worker vào đúng 1 video như trước. An toàn tài nguyên: mỗi node
con (ytdlp/translate/editor/transcribe/tts-gpu) đã tự giới hạn 1 job/lúc từ thiết kế Phase 1
(không đổi); rủi ro tranh chấp GPU duy nhất giữa transcribe/tts-gpu được chặn riêng bằng
`gpu_lock_acquire()` ở tầng 2 node đó (xem `queue_lib.py`/`worker.py` của chính chúng), không
phải ở đây."""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import redis as redis_lib

import queue_lib as q
from pipeline_runner import PipelineCancelled, run_pipeline

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "orchestrator-worker"
# Chỉ còn là trần an toàn thô (chống nổ thread/memory) — KHÔNG còn phải gánh việc chống OOM GPU
# (đã có gpu_lock_acquire() dùng chung giữa reup-transcribe-node/reup-tts-gpu-node lo riêng) hay
# né rate-limit Douyin (đã có rate_limit_wait() bên reup-ytdlp-node lo riêng). Xem lý do đầy đủ
# + bug thật gặp (job nhẹ mode=audio bị chặn oan bởi 3 job nặng đang ở bước GPU) trong plan lưu
# ở .../warm-knitting-map.md. Giá trị thật lấy từ docker-compose.yml, "10" ở đây chỉ là fallback
# khi chạy ngoài compose (vd test local không set env).
PIPELINE_CONCURRENCY = int(os.environ.get("PIPELINE_CONCURRENCY", "10"))


def _heartbeat_loop(conn: redis_lib.Redis, interval: float = 10.0) -> None:
    """Thread nền riêng — job xử lý xong mới quay lại đầu `while True` gọi heartbeat, nên
    job chạy lâu hơn HEARTBEAT_TTL_S (30s — cả pipeline chạy 5-7 phút) làm key hết hạn giữa
    chừng dù worker vẫn đang bận, khiến `/nodes/status` tưởng nhầm orchestrator down suốt lúc
    chạy pipeline. Bug thật gặp: dashboard monitor báo đúng node đang xử lý job là down."""
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


def _process_pipeline_job(
    conn: redis_lib.Redis, job_id: str, payload: dict, resume_stages: dict, slot: threading.Semaphore,
) -> None:
    """Chạy 1 pipeline (1 video) hết 5 bước, gọi từ 1 thread riêng trong pool — mỗi lần gọi có
    state cục bộ hoàn toàn riêng (`stages` dict bên trong `run_pipeline()`), an toàn chạy đồng
    thời nhiều video. `mark_done`/`mark_failed`/`log_event` dùng chung 1 `redis.Redis` client —
    an toàn gọi đồng thời từ nhiều thread theo đúng tài liệu redis-py (lệnh đơn, không cần
    connection riêng mỗi thread). `slot` được nhả trong `finally` — xem ghi chú ở `main()`.

    `resume_stages` (đọc từ `partial_stages` của job — rỗng nếu job mới, có dữ liệu nếu job này
    được `POST /pipelines/{id}/retry` requeue lại sau khi fail) cho `run_pipeline()` biết bước
    nào đã xong để bỏ qua, không tải/xử lý lại từ đầu."""
    video_name = payload.get("video_name")
    t0 = time.time()
    try:
        result = run_pipeline(job_id, payload, conn, resume_stages=resume_stages)
        q.mark_done(conn, job_id, result)
        q.log_event({"job_id": job_id, "pipeline_id": job_id, "video_name": video_name,
                     "role": ROLE, "event": "job_done", "elapsed_s": round(time.time() - t0, 1)})
        print(f"[worker] pipeline {job_id} xong sau {time.time() - t0:.1f}s", file=sys.stderr)
    except PipelineCancelled as e:
        new_stages = getattr(e, "stages", None)
        q.mark_cancelled(conn, job_id, stages=new_stages)
        q.log_event({"job_id": job_id, "pipeline_id": job_id, "video_name": video_name,
                     "role": ROLE, "event": "job_cancelled", "elapsed_s": round(time.time() - t0, 1)})
        print(f"[worker] pipeline {job_id} đã huỷ theo yêu cầu ({e})", file=sys.stderr)
    except Exception as e:
        traceback.print_exc()
        # str(e) rỗng/vô nghĩa với nhiều loại exception (StopIteration, KeyError...) — lưu cả
        # traceback (cap 4000 ký tự cuối) để biết được NGUYÊN NHÂN thật, không chỉ "lỗi: ".
        err_text = traceback.format_exc()[-4000:]
        # `run_pipeline()` gắn `.stages` (tiến độ từng bước tính tới lúc lỗi) vào exception
        # trước khi raise — lưu lại làm `partial_stages` để lần retry sau bỏ qua bước đã xong.
        new_stages = getattr(e, "stages", None)
        q.mark_failed(conn, job_id, err_text, stages=new_stages)
        q.log_event({"job_id": job_id, "pipeline_id": job_id, "video_name": video_name,
                     "role": ROLE, "event": "job_failed", "elapsed_s": round(time.time() - t0, 1),
                     "error": err_text})
        print(f"[worker] pipeline {job_id} lỗi: {e}", file=sys.stderr)
    finally:
        slot.release()


def main() -> None:
    conn = redis_lib.Redis.from_url(REDIS_URL)
    threading.Thread(target=_heartbeat_loop, args=(conn,), daemon=True).start()
    threading.Thread(target=_log_retention_loop, daemon=True).start()

    recovered = q.recover_stale_jobs(conn)
    if recovered:
        print(f"[worker] Khôi phục {recovered} pipeline job bị kẹt từ lần chạy trước", file=sys.stderr)

    print(f"[worker] Sẵn sàng nhận pipeline job (service={q.SERVICE}, "
          f"PIPELINE_CONCURRENCY={PIPELINE_CONCURRENCY})", file=sys.stderr)
    # Semaphore chặn NGAY TỪ `pop_job()` — không chỉ giới hạn số thread chạy song song mà còn
    # đảm bảo job chỉ rời "pending" (mark_started) khi thật sự có chỗ trống để chạy. Nếu chỉ giới
    # hạn qua ThreadPoolExecutor mà pop_job() vẫn hút hết cả trăm job vào "processing" ngay lập
    # tức (submit() không block), Monitor sẽ hiện "started" cho job còn chưa thật sự chạy — sai
    # lệch trạng thái, dù không phải rủi ro tài nguyên. Semaphore giữ trạng thái phản ánh đúng.
    slots = threading.Semaphore(PIPELINE_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=PIPELINE_CONCURRENCY) as pool:
        while True:
            slots.acquire()
            job_id = q.pop_job(conn, timeout=5)
            if job_id is None:
                slots.release()
                continue

            job = q.get_job(conn, job_id)
            if job is None:
                slots.release()
                continue
            q.mark_started(conn, job_id)
            payload = job["payload"]
            resume_stages = job.get("partial_stages") or {}
            q.log_event({"job_id": job_id, "pipeline_id": job_id, "video_name": payload.get("video_name"),
                         "role": ROLE, "event": "job_start", "url": payload.get("url"), "mode": payload.get("mode"),
                         "resumed": bool(resume_stages)})
            pool.submit(_process_pipeline_job, conn, job_id, payload, resume_stages, slots)


if __name__ == "__main__":
    main()
