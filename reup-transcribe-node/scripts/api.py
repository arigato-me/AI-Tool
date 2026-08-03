"""FastAPI — không import torch/pipeline/model_registry (giống nguyên tắc tts-node: api
luôn nhẹ, không cần GPU, restart/scale độc lập với worker đang giữ model trong VRAM). Chỉ
nói chuyện với Redis."""
from __future__ import annotations

import os

import redis as redis_lib
from fastapi import FastAPI
from pydantic import BaseModel

import queue_lib as q

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "transcribe-worker"

conn = redis_lib.Redis.from_url(REDIS_URL)
app = FastAPI()


class TranscribeJobRequest(BaseModel):
    input: str
    output: str
    align: bool = False
    mode: str = "review"  # "review" (mặc định, không đổi hành vi cũ) | "dialogue" (giữ nền)
    source_lang: str = "zh"  # "zh" (Paraformer) | "other" (whisper, tự nhận diện per-clip) —
    # người dùng chọn thẳng trên web, không còn tự đoán (xem pipeline.py)
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
def submit_job(req: TranscribeJobRequest) -> dict:
    if req.mode not in ("review", "dialogue"):
        return {"ok": False, "error": f"mode không hợp lệ: {req.mode!r} (chỉ nhận 'review'/'dialogue')"}
    if req.source_lang not in ("zh", "other"):
        return {"ok": False, "error": f"source_lang không hợp lệ: {req.source_lang!r} (chỉ nhận 'zh'/'other')"}
    job_id = q.new_job(conn, req.model_dump())
    return {"ok": True, "job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = q.get_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "job không tồn tại hoặc đã hết hạn (TTL 48h)"}
    return {"ok": True, "job_id": job_id, **job}
