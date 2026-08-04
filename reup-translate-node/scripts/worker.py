"""Worker — vòng lặp thuần (không fork/subprocess per-job), theo đúng pattern tts-node:
BLMOVE chờ job, xử lý tuần tự trong cùng process, ack (mark_done/mark_failed) sau khi xong.
Node này (translate) không có model nặng để giữ resident, nhưng cùng 1 pattern worker được
dùng thống nhất cho mọi node (kể cả GPU) — xem review_tts-node.md / CLAUDE.md."""
from __future__ import annotations

import os
import sys
import threading
import time
import traceback

import redis as redis_lib

import queue_lib as q
from translate_cli import run_translate

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "translate-worker"


def _heartbeat_loop(conn: redis_lib.Redis, interval: float = 10.0) -> None:
    """Thread nền riêng — job xử lý xong mới quay lại đầu `while True` gọi heartbeat, nên
    job chạy lâu hơn HEARTBEAT_TTL_S (30s) làm key hết hạn giữa chừng dù worker vẫn đang bận,
    khiến `/health`/`/nodes/status` tưởng nhầm là down. Bug thật gặp: dashboard monitor báo
    đúng node đang xử lý job là down."""
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


def main() -> None:
    conn = redis_lib.Redis.from_url(REDIS_URL)
    threading.Thread(target=_heartbeat_loop, args=(conn,), daemon=True).start()
    threading.Thread(target=_log_retention_loop, daemon=True).start()

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
            result = run_translate(
                payload["input"],
                payload["output"],
                payload["api_key"],
                payload.get("model") or "deepseek-chat",
                payload.get("base_url") or "https://api.deepseek.com",
                payload.get("target_lang") or "tiếng Việt",
                int(payload.get("batch_size") or 20),
                pipeline_id=pipeline_id,
                video_name=video_name,
                tag_speakers_enabled=bool(payload.get("tag_speakers", False)),
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
