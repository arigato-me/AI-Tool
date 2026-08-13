"""Reliable job queue trên Redis — theo đúng pattern đã review ở tts-node
(xem review_tts-node.md, gốc repo): BLMOVE pending->processing (job không mất giữa lúc pop
và lúc xử lý xong), job state ở Redis Hash, heartbeat TTL, recover_stale_jobs() lúc worker
khởi động lại. Mỗi node (`reup-*-node`) có 1 bản copy độc lập của file này — đúng nguyên tắc
"standalone scaffold" của repo (không import code chéo giữa các thư mục service).

`SERVICE` (đặt ở đầu mỗi bản copy) làm prefix key — nhiều node dùng chung 1 Redis
(`reup-broker`) mà không đụng key của nhau.
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

SERVICE = "notify"  # đổi giá trị này ở mỗi node khi copy sang (vd "transcribe", "tts-gpu")

JOB_TTL_S = 48 * 3600
HEARTBEAT_TTL_S = 30

LOG_DIR = Path("/logs")
LOG_RETENTION_DAYS = 2
EVENT_LOG_ENABLED = os.environ.get("EVENT_LOG_ENABLED", "true").lower() == "true"


def _pending_key() -> str:
    return f"queue:pending:{SERVICE}"


def _processing_key() -> str:
    return f"queue:processing:{SERVICE}"


def _job_key(job_id: str) -> str:
    return f"job:{SERVICE}:{job_id}"


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
    })
    conn.expire(_job_key(job_id), JOB_TTL_S)
    conn.rpush(_pending_key(), job_id)
    return job_id


def get_job(conn: redis.Redis, job_id: str) -> dict[str, Any] | None:
    data = conn.hgetall(_job_key(job_id))
    if not data:
        return None
    out = {k.decode(): v.decode() for k, v in data.items()}
    out["payload"] = json.loads(out["payload"]) if out.get("payload") else {}
    out["result"] = json.loads(out["result"]) if out.get("result") else None
    return out


def pop_job(conn: redis.Redis, timeout: int = 5) -> str | None:
    """BLMOVE pending->processing — job chỉ rời khỏi 'processing' khi worker ack
    (mark_done/mark_failed), nên worker crash giữa chừng không làm mất job.

    Bug thật gặp khi test: redis-py ném `redis.exceptions.TimeoutError` (thay vì trả None)
    khi socket-level timeout xảy ra sát ngưỡng blocking timeout của BLMOVE — tuỳ version/độ
    trễ mạng, không phải lỗi thật (không có job) — coi như 1 vòng poll rỗng thay vì crash
    worker (trước đó làm worker crash-loop liên tục, không giữ model resident được).

    Bug thật gặp lần 2 (test cắt mạng thật, không chỉ timeout sát ngưỡng): mất kết nối Redis
    hẳn (network blip, DNS resolution fail...) ném `redis.exceptions.ConnectionError` — KHÔNG
    phải subclass của `TimeoutError`, lọt qua except cũ, crash cả worker. Bắt rộng ra
    `redis.exceptions.RedisError` (class cha chung của mọi lỗi redis-py, gồm cả 2 loại trên) —
    cùng coi như 1 vòng poll rỗng, để lần gọi kế tự thử kết nối lại, không cần tự viết logic
    reconnect."""
    try:
        job_id = conn.blmove(_pending_key(), _processing_key(), timeout, "LEFT", "RIGHT")
    except redis.exceptions.RedisError:
        return None
    return job_id.decode() if job_id else None


def mark_started(conn: redis.Redis, job_id: str) -> None:
    conn.hset(_job_key(job_id), mapping={"status": "started", "updated_at": time.time()})


def mark_done(conn: redis.Redis, job_id: str, result: dict[str, Any]) -> None:
    conn.hset(_job_key(job_id), mapping={
        "status": "finished",
        "result": json.dumps(result, ensure_ascii=False),
        "updated_at": time.time(),
    })
    conn.lrem(_processing_key(), 0, job_id)


def mark_failed(conn: redis.Redis, job_id: str, error: str) -> None:
    conn.hset(_job_key(job_id), mapping={"status": "failed", "error": error, "updated_at": time.time()})
    conn.lrem(_processing_key(), 0, job_id)


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
