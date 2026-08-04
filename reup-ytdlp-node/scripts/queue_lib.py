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
from urllib.parse import urlparse

import redis

SERVICE = "ytdlp"  # đổi giá trị này ở mỗi node khi copy sang (vd "transcribe", "tts-gpu")

JOB_TTL_S = 48 * 3600
HEARTBEAT_TTL_S = 30

RATE_LIMITS_PATH = os.environ.get("RATE_LIMITS_PATH", "/config/rate_limits.yaml")
RATE_LIMIT_POLL_S = 2.0
RATE_LIMIT_MAX_WAIT_S = 300.0  # trần chờ tối đa — quá trần vẫn cho chạy kèm cảnh báo, không treo
# vô thời hạn (xem docstring rate_limit_wait() bên dưới)

LOG_DIR = Path("/logs")
LOG_RETENTION_DAYS = 2
EVENT_LOG_ENABLED = os.environ.get("EVENT_LOG_ENABLED", "true").lower() == "true"


def _pending_key() -> str:
    return f"queue:pending:{SERVICE}"


def _processing_key() -> str:
    return f"queue:processing:{SERVICE}"


def _job_key(job_id: str) -> str:
    return f"job:{SERVICE}:{job_id}"


def _cancel_key(pipeline_id: str) -> str:
    return f"cancel:{SERVICE}:{pipeline_id}"


def request_cancel(conn: redis.Redis, pipeline_id: str) -> None:
    """Đặt cờ huỷ theo `pipeline_id` (không phải job_id nội bộ của node này — orchestrator chỉ
    biết pipeline_id, xem `api.py::cancel_by_pipeline`). `worker.py` đang chạy dở
    `subprocess.Popen` tự polling cờ này (khác cờ hợp tác 'chờ bước sau' bên orchestrator) và
    kill tiến trình yt-dlp ngay khi phát hiện."""
    conn.set(_cancel_key(pipeline_id), "1", ex=JOB_TTL_S)


def is_cancel_requested(conn: redis.Redis, pipeline_id: str) -> bool:
    return bool(conn.exists(_cancel_key(pipeline_id)))


def clear_cancel(conn: redis.Redis, pipeline_id: str) -> None:
    conn.delete(_cancel_key(pipeline_id))


def mark_cancelled(conn: redis.Redis, job_id: str) -> None:
    conn.hset(_job_key(job_id), mapping={
        "status": "cancelled", "error": "Job bị huỷ theo yêu cầu người dùng (đang tải)",
        "updated_at": time.time(),
    })
    conn.lrem(_processing_key(), 0, job_id)


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


def _load_rate_limits(path: str) -> dict[str, Any]:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, ImportError):
        return {}


def _domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def rate_limit_wait(conn: redis.Redis, url: str, config_path: str = RATE_LIMITS_PATH) -> None:
    """Rate-limit theo DOMAIN NGUỒN (vd douyin.com) — TÁCH RIÊNG khỏi `PIPELINE_CONCURRENCY` của
    orchestrator (xem plan lưu ở .../warm-knitting-map.md, mục "3 cơ chế tách biệt"): concurrency
    ở tầng orchestrator lo "bao nhiêu video đang chạy trong hệ thống", còn hàm NÀY lo đúng 1 việc
    khác — "bao nhiêu request đã gửi tới 1 domain trong 1 khoảng thời gian". Trước đây gộp chung
    2 việc vào 1 con số từng làm job vô hại (vd mode=audio, domain không tần suất cao) bị chặn
    oan chỉ vì có video KHÁC (domain khác hẳn) đang chạy — xem lý do đầy đủ trong plan.

    Sliding-window log bằng Redis sorted set (key `ratelimit:ytdlp:{domain}`) — mỗi lần gọi ghi
    1 timestamp, đếm số lần trong `window_s` gần nhất, vượt `max_requests` thì poll-sleep tới
    khi có chỗ. Domain KHÔNG có trong `rate_limits.yaml` -> bỏ qua ngay, không giới hạn gì (an
    toàn mặc định — platform chưa từng cấu hình không bị ảnh hưởng, hành vi y hệt trước khi có
    hàm này). Có trần chờ `RATE_LIMIT_MAX_WAIT_S`: quá trần vẫn cho chạy (kèm cảnh báo) thay vì
    treo vô thời hạn — ưu tiên "job vẫn chạy được" hơn tuân thủ tuyệt đối, tránh 1 lần cấu hình
    sai/quá chặt biến thành treo cứng cả node."""
    domain = _domain_of(url)
    limits = _load_rate_limits(config_path)
    # So khớp theo HẬU TỐ domain, không phải khớp tuyệt đối — bug thật gặp lúc test: URL Douyin
    # thực tế luôn có subdomain (vd "v.douyin.com" — link rút gọn hay dùng), khớp tuyệt đối với
    # "douyin.com" trong config sẽ KHÔNG BAO GIỜ match, khiến rate limiter im lặng không kích
    # hoạt (trông như "chạy OK" nhưng thực chất chưa hề giới hạn gì). Domain con nào của
    # "douyin.com" cũng áp đúng 1 cấu hình đó.
    cfg = next((v for d, v in limits.items() if domain == d or domain.endswith(f".{d}")), None)
    if not cfg:
        return
    max_requests = int(cfg.get("max_requests", 0))
    window_s = float(cfg.get("window_s", 0))
    if max_requests <= 0 or window_s <= 0:
        return

    key = f"ratelimit:ytdlp:{domain}"
    deadline = time.time() + RATE_LIMIT_MAX_WAIT_S
    warned = False
    while True:
        now = time.time()
        conn.zremrangebyscore(key, 0, now - window_s)
        count = conn.zcard(key)
        if count < max_requests:
            conn.zadd(key, {uuid.uuid4().hex: now})
            conn.expire(key, int(window_s) + 60)
            return
        if now >= deadline:
            print(f"[rate_limit] domain={domain} vượt trần chờ {RATE_LIMIT_MAX_WAIT_S}s "
                  f"({count}/{max_requests} request trong {window_s}s gần nhất) — vẫn cho chạy, "
                  f"cân nhắc nới rate_limits.yaml nếu lặp lại thường xuyên", file=sys.stderr)
            return
        if not warned:
            print(f"[rate_limit] domain={domain} đã {count}/{max_requests} request trong "
                  f"{window_s}s gần nhất — chờ tới khi có chỗ (tối đa {RATE_LIMIT_MAX_WAIT_S}s)",
                  file=sys.stderr)
            warned = True
        time.sleep(RATE_LIMIT_POLL_S)
