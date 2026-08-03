"""FastAPI — không import vieneu/torch (giống nguyên tắc tts-node/transcribe: api luôn nhẹ,
không cần GPU). 1 API cho 2 job type cũ (tts/segments) + list-voices, phân biệt qua `cmd`."""
from __future__ import annotations

import os

import redis as redis_lib
from fastapi import FastAPI
from pydantic import BaseModel

import queue_lib as q

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "tts-gpu-worker"

conn = redis_lib.Redis.from_url(REDIS_URL)
app = FastAPI()

VALID_CMDS = {"tts", "segments", "list-voices"}


class TtsJobRequest(BaseModel):
    cmd: str  # "tts" | "segments" | "list-voices"
    params: dict = {}
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
def submit_job(req: TtsJobRequest) -> dict:
    if req.cmd not in VALID_CMDS:
        return {"ok": False, "error": f"cmd không hợp lệ: {req.cmd!r} (chỉ nhận {sorted(VALID_CMDS)})"}
    job_id = q.new_job(conn, {"cmd": req.cmd, "params": req.params,
                              "pipeline_id": req.pipeline_id, "video_name": req.video_name})
    return {"ok": True, "job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = q.get_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "job không tồn tại hoặc đã hết hạn (TTL 48h)"}
    return {"ok": True, "job_id": job_id, **job}
