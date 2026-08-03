"""Reliable job queue trên Redis — theo đúng pattern đã review ở tts-node
(xem review_tts-node.md, gốc repo): BLMOVE pending->processing (job không mất giữa lúc pop
và lúc xử lý xong), job state ở Redis Hash, heartbeat TTL, recover_stale_jobs() lúc worker
khởi động lại. Mỗi node (`reup-*-node`) có 1 bản copy độc lập của file này — đúng nguyên tắc
"standalone scaffold" của repo (không import code chéo giữa các thư mục service).

`SERVICE` (đặt ở đầu mỗi bản copy) làm prefix key — nhiều node dùng chung 1 Redis
(`reup-broker`) mà không đụng key của nhau.

Bản copy này (orchestrator) thêm `list_recent()`/ghi vào `pipelines:recent:{SERVICE}` trong
`new_job()` — riêng node này cần liệt kê job gần đây cho `reup-ui`, 5 node kia không cần vì
luôn được gọi bởi đúng 1 orchestrator đã biết sẵn job_id.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import redis

SERVICE = "orchestrator"  # đổi giá trị này ở mỗi node khi copy sang (vd "transcribe", "tts-gpu")

JOB_TTL_S = 48 * 3600
HEARTBEAT_TTL_S = 30
RECENT_MAX = 200

LOG_DIR = Path("/logs")
LOG_RETENTION_DAYS = 2
EVENT_LOG_ENABLED = os.environ.get("EVENT_LOG_ENABLED", "true").lower() == "true"


def _pending_key() -> str:
    return f"queue:pending:{SERVICE}"


def _processing_key() -> str:
    return f"queue:processing:{SERVICE}"


def _job_key(job_id: str) -> str:
    return f"job:{SERVICE}:{job_id}"


def _recent_key() -> str:
    return f"pipelines:recent:{SERVICE}"


def _total_key() -> str:
    return f"pipelines:total:{SERVICE}"


def _cancel_key(job_id: str) -> str:
    return f"cancel:{SERVICE}:{job_id}"


def new_job(conn: redis.Redis, payload: dict[str, Any]) -> str:
    job_id = uuid.uuid4().hex
    now = time.time()
    conn.hset(_job_key(job_id), mapping={
        "status": "pending",
        "payload": json.dumps(payload, ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
        "result": "",
        "error": "",
        "partial_stages": "",
    })
    conn.expire(_job_key(job_id), JOB_TTL_S)
    conn.rpush(_pending_key(), job_id)
    conn.lpush(_recent_key(), job_id)
    conn.ltrim(_recent_key(), 0, RECENT_MAX - 1)
    conn.incr(_total_key())
    return job_id


def requeue_for_retry(conn: redis.Redis, job_id: str) -> None:
    """Đưa lại 1 job đang 'failed' vào đầu hàng đợi pending để chạy lại — GIỮ NGUYÊN job_id
    (khác `new_job()` luôn sinh id mới), để `pipeline_runner.py` nhận diện đúng `pipeline_id`
    và tái sử dụng file trung gian đã có (mọi tên file trong pipeline đều prefix theo
    pipeline_id/video_name) thay vì tải/xử lý lại từ đầu. `partial_stages` (ghi bởi
    `mark_failed()` ở lần fail trước) vẫn giữ nguyên trong hash — `worker.py` đọc lại field này
    khi job được xử lý lần nữa để biết bước nào đã xong."""
    conn.hset(_job_key(job_id), mapping={"status": "pending", "updated_at": time.time()})
    conn.expire(_job_key(job_id), JOB_TTL_S)
    conn.rpush(_pending_key(), job_id)
    # Dọn cờ huỷ cũ (nếu job này từng bị cancel rồi được retry) — không dọn thì job vừa
    # requeue bị is_cancel_requested() phát hiện dương tính ngay từ bước đầu, huỷ lại tức thì.
    conn.delete(_cancel_key(job_id))


def total_count(conn: redis.Redis) -> int:
    """Tổng số job từng tạo (đếm riêng, không giới hạn 200 như `pipelines:recent`)."""
    val = conn.get(_total_key())
    return int(val) if val else 0


def _translate_tokens_key(day: date) -> str:
    return f"translate_tokens:{SERVICE}:{day.isoformat()}"


def add_translate_tokens(conn: redis.Redis, n: int) -> None:
    """Cộng dồn token Deepseek (usage.total_tokens trả về từ translate-node, xem
    pipeline_runner.py) vào bộ đếm theo ngày — không TTL (khác `job:*` hết hạn 48h), vì đây là
    cơ sở duy nhất còn sống để tổng theo tuần/tháng (job list/JSONL log đều hết hạn quá sớm)."""
    if n:
        conn.incrby(_translate_tokens_key(date.today()), n)


def translate_tokens_stats(conn: redis.Redis) -> dict[str, int]:
    """Tổng token hôm nay / tuần này (thứ 2 -> nay) / tháng này (ngày 1 -> nay) — cộng ngược từ
    các key theo ngày, 1 lượt MGET (tối đa ~37 ngày kể cả phần tuần lấn sang tháng trước)."""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    range_start = min(week_start, month_start)
    days = [range_start + timedelta(days=i) for i in range((today - range_start).days + 1)]
    vals = conn.mget([_translate_tokens_key(d) for d in days])
    counts = [int(v) if v else 0 for v in vals]
    return {
        "today": counts[-1],
        "this_week": sum(c for d, c in zip(days, counts) if d >= week_start),
        "this_month": sum(c for d, c in zip(days, counts) if d >= month_start),
    }


def get_job(conn: redis.Redis, job_id: str) -> dict[str, Any] | None:
    data = conn.hgetall(_job_key(job_id))
    if not data:
        return None
    out = {k.decode(): v.decode() for k, v in data.items()}
    out["payload"] = json.loads(out["payload"]) if out.get("payload") else {}
    out["result"] = json.loads(out["result"]) if out.get("result") else None
    out["partial_stages"] = json.loads(out["partial_stages"]) if out.get("partial_stages") else {}
    out["current_stage_started_at"] = float(out["current_stage_started_at"]) if out.get("current_stage_started_at") else None
    return out


def list_recent(conn: redis.Redis, limit: int = 100) -> list[str]:
    ids = conn.lrange(_recent_key(), 0, limit - 1)
    return [i.decode() for i in ids]


def pop_job(conn: redis.Redis, timeout: int = 5) -> str | None:
    """BLMOVE pending->processing — job chỉ rời khỏi 'processing' khi worker ack
    (mark_done/mark_failed), nên worker crash giữa chừng không làm mất job.

    Bug thật gặp khi test: redis-py ném `redis.exceptions.TimeoutError` (thay vì trả None)
    khi socket-level timeout xảy ra sát ngưỡng blocking timeout của BLMOVE — tuỳ version/độ
    trễ mạng, không phải lỗi thật (không có job) — coi như 1 vòng poll rỗng thay vì crash
    worker (trước đó làm worker crash-loop liên tục, không giữ model resident được)."""
    try:
        job_id = conn.blmove(_pending_key(), _processing_key(), timeout, "LEFT", "RIGHT")
    except redis.exceptions.TimeoutError:
        return None
    return job_id.decode() if job_id else None


def mark_started(conn: redis.Redis, job_id: str) -> None:
    conn.hset(_job_key(job_id), mapping={"status": "started", "updated_at": time.time()})


def update_stage(conn: redis.Redis, job_id: str, stage: str) -> None:
    """Ghi stage đang chạy vào job hash — cho `reup-ui` Monitor hiển thị "đang xử lý ở node
    nào" ngay cả khi job còn `started` (field `result` chỉ có đầy đủ sau khi xong hết pipeline,
    xem `mark_done`). `current_stage_started_at` cho phép UI tính "đang chạy được bao lâu" cho
    đúng bước hiện tại (không có timestamp riêng thì không tính được elapsed lúc job chưa xong)."""
    conn.hset(_job_key(job_id), mapping={"current_stage": stage, "current_stage_started_at": time.time()})


def save_stage_progress(conn: redis.Redis, job_id: str, stages: dict[str, Any]) -> None:
    """Ghi tiến độ từng bước NGAY khi 1 bước xong — khác `mark_failed(..., stages=...)` (chỉ ghi
    lúc fail): gọi hàm này sau MỖI bước thành công, dùng lại field `partial_stages` (không tạo
    field mới) để `reup-ui` hiển thị elapsed_s từng node cả khi job còn đang chạy, không phải
    đợi tới lúc fail mới thấy. Không đụng `status`/`error` — an toàn gọi song song với luồng
    chính, không ảnh hưởng job đang chạy dở."""
    conn.hset(_job_key(job_id), mapping={
        "partial_stages": json.dumps(stages, ensure_ascii=False),
        "updated_at": time.time(),
    })


def mark_done(conn: redis.Redis, job_id: str, result: dict[str, Any]) -> None:
    # Dọn "error"/"partial_stages" của lần fail trước (nếu job này từng retry) — nếu không,
    # 1 job resume-tới-thành-công vẫn hiện traceback cũ ở JobDetail dù status đã "finished".
    conn.hset(_job_key(job_id), mapping={
        "status": "finished",
        "result": json.dumps(result, ensure_ascii=False),
        "updated_at": time.time(),
        "error": "",
        "partial_stages": "",
    })
    conn.lrem(_processing_key(), 0, job_id)


def mark_failed(conn: redis.Redis, job_id: str, error: str, stages: dict[str, Any] | None = None) -> None:
    """`stages` (tiến độ từng bước tính tới lúc lỗi, xem `pipeline_runner.run_pipeline()`) được
    lưu lại làm `partial_stages` — cho phép `POST /pipelines/{id}/retry` chạy lại CHỈ từ bước
    lỗi, bỏ qua các bước đã xong (không tải/xử lý lại từ đầu)."""
    mapping: dict[str, Any] = {"status": "failed", "error": error, "updated_at": time.time()}
    if stages is not None:
        mapping["partial_stages"] = json.dumps(stages, ensure_ascii=False)
    conn.hset(_job_key(job_id), mapping=mapping)
    conn.lrem(_processing_key(), 0, job_id)


def cancel_pending(conn: redis.Redis, job_id: str) -> bool:
    """Gỡ job khỏi hàng đợi 'pending' (chưa worker nào pop) — trả True nếu gỡ được (job đúng
    là chưa chạy). LREM đơn lệnh, atomic ở tầng Redis. Trả False nếu job đã bị `pop_job()`
    (BLMOVE) chuyển sang 'processing' đúng lúc race với request cancel — khi đó gọi
    `request_cancel()` thay (coi như job đã 'started')."""
    return bool(conn.lrem(_pending_key(), 0, job_id))


def request_cancel(conn: redis.Redis, job_id: str) -> None:
    """Đặt cờ huỷ hợp tác (cooperative cancel) cho 1 job đang 'started' — KHÔNG dừng ngay bước
    đang chạy dở (gọi HTTP tới node con vẫn chạy tới lúc xong), chỉ ngăn `pipeline_runner.py`
    tiến sang bước KẾ TIẾP (`_run_stage()` tự kiểm tra cờ này trước mỗi bước). Xem giới hạn này
    ở README mục "Huỷ job"."""
    conn.set(_cancel_key(job_id), "1", ex=JOB_TTL_S)


def is_cancel_requested(conn: redis.Redis, job_id: str) -> bool:
    return bool(conn.exists(_cancel_key(job_id)))


def mark_cancelled(conn: redis.Redis, job_id: str, stages: dict[str, Any] | None = None) -> None:
    """Status riêng 'cancelled' (khác 'failed') — để `reup-ui` phân biệt huỷ chủ động với lỗi
    thật, và `POST /pipelines/{id}/retry` vẫn resume được từ `partial_stages` y hệt 'failed'."""
    mapping: dict[str, Any] = {
        "status": "cancelled", "error": "Job bị huỷ theo yêu cầu người dùng", "updated_at": time.time(),
    }
    if stages is not None:
        mapping["partial_stages"] = json.dumps(stages, ensure_ascii=False)
    conn.hset(_job_key(job_id), mapping=mapping)
    conn.lrem(_processing_key(), 0, job_id)
    conn.delete(_cancel_key(job_id))


def set_trim_result(conn: redis.Redis, job_id: str, output: str) -> None:
    """Ghi lại path file mp3 đã cắt (`POST /pipelines/{id}/trim`) vào job hash — field đơn giản
    (string, không JSON) nên `get_job()` tự trả về qua vòng lặp decode chung, không cần sửa gì
    thêm ở đó. Ghi đè nếu cắt lại nhiều lần (cùng tên file `_trimmed.mp3`, đúng convention "tải
    lại cùng tên thì đè" đã dùng ở yt-dlp)."""
    conn.hset(_job_key(job_id), mapping={"trim_output": output, "updated_at": time.time()})


def recover_stale_jobs(conn: redis.Redis) -> int:
    """Lúc worker khởi động: job còn kẹt trong 'processing' (worker cũ crash giữa chừng,
    vd bị OOM-kill) được đẩy lại đầu 'pending' để xử lý lại — at-least-once processing."""
    n = 0
    while True:
        job_id = conn.rpop(_processing_key())
        if not job_id:
            break
        conn.lpush(_pending_key(), job_id)
        n += 1
    return n


def heartbeat(conn: redis.Redis, role: str) -> None:
    conn.set(f"worker:heartbeat:{SERVICE}:{role}", time.time(), ex=HEARTBEAT_TTL_S)


def worker_alive(conn: redis.Redis, role: str) -> bool:
    return conn.get(f"worker:heartbeat:{SERVICE}:{role}") is not None


def queue_depth(conn: redis.Redis) -> dict[str, int]:
    return {"pending": conn.llen(_pending_key()), "processing": conn.llen(_processing_key())}


def log_event(payload: dict[str, Any]) -> None:
    """Ghi 1 dòng JSONL vào `/logs/YYYY-MM-DD.jsonl` — structured, mang `pipeline_id`/
    `video_name` để tra chéo giữa các node khi debug lỗi chất lượng (dịch sai, mute, noise,
    echo...). Không bao giờ được phép làm crash job thật đang chạy — mọi lỗi (disk đầy, quyền
    ghi...) bị nuốt tại đây, chỉ in cảnh báo ra stderr."""
    if not EVENT_LOG_ENABLED:
        return
    try:
        entry = {"ts": time.time(), "service": SERVICE, **payload}
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / f"{date.today().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[log_event] lỗi ghi log (bỏ qua, không ảnh hưởng job): {e}", file=sys.stderr)


def prune_old_logs() -> int:
    """Xoá file `/logs/*.jsonl` cũ hơn `LOG_RETENTION_DAYS` — gọi định kỳ từ
    `_log_retention_loop()` trong worker.py. Tự nuốt lỗi, không bao giờ crash worker."""
    n = 0
    try:
        if not LOG_DIR.is_dir():
            return 0
        cutoff = date.today() - timedelta(days=LOG_RETENTION_DAYS)
        for f in LOG_DIR.glob("*.jsonl"):
            try:
                file_date = date.fromisoformat(f.stem)
            except ValueError:
                continue
            if file_date < cutoff:
                f.unlink()
                n += 1
    except Exception as e:
        print(f"[prune_old_logs] lỗi dọn log (bỏ qua): {e}", file=sys.stderr)
    return n
