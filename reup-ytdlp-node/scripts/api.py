from __future__ import annotations

import os

import redis as redis_lib
from fastapi import FastAPI
from pydantic import BaseModel

import queue_lib as q

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "ytdlp-worker"

conn = redis_lib.Redis.from_url(REDIS_URL)
app = FastAPI()


class YtdlpJobRequest(BaseModel):
    args: list[str]  # y hệt args CLI của docker_yt-dlp gốc, vd ["-o", "/downloads/%(title)s.%(ext)s", "<url>"]
    pipeline_id: str | None = None  # correlation ID cho log — orchestrator truyền vào, không bắt buộc
    video_name: str | None = None


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": q.SERVICE,
        "worker_alive": q.worker_alive(conn, ROLE),
        **q.queue_depth(conn),
    }


@app.post("/jobs")
def submit_job(req: YtdlpJobRequest) -> dict:
    if not req.args:
        return {"ok": False, "error": "thiếu args (vd URL, -o, ...)"}
    job_id = q.new_job(conn, {"args": req.args, "pipeline_id": req.pipeline_id, "video_name": req.video_name})
    return {"ok": True, "job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = q.get_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "job không tồn tại hoặc đã hết hạn (TTL 48h)"}
    return {"ok": True, "job_id": job_id, **job}


@app.post("/pipelines/{pipeline_id}/cancel")
def cancel_by_pipeline(pipeline_id: str) -> dict:
    """Huỷ job ytdlp đang tải cho 1 `pipeline_id` cụ thể — orchestrator gọi vào đây khi người
    dùng bấm Huỷ đúng lúc pipeline đang ở bước `ytdlp` (xem `pipeline_runner.py` /
    `reup-orchestrator-node/README.md` mục "Huỷ job"). Key theo `pipeline_id` (không phải
    job_id nội bộ của node này, vốn không lộ ra ngoài) vì orchestrator chỉ biết pipeline_id.
    `worker.py` đang chạy dở `subprocess.Popen` tự polling cờ này (xem `queue_lib.request_cancel`)
    và kill tiến trình yt-dlp ngay — khác cờ hợp tác "chờ bước sau" của tầng orchestrator."""
    q.request_cancel(conn, pipeline_id)
    return {"ok": True, "pipeline_id": pipeline_id}
