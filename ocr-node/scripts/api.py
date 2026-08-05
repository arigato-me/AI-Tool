"""FastAPI — chỉ nói chuyện với Redis, không import markitdown/PyMuPDF/PaddleOCR/VietOCR
(nặng, không cần thiết — đúng nguyên tắc tách api/worker của mọi node khác: api luôn nhẹ,
restart/scale độc lập với worker đang giữ model trong VRAM)."""
from __future__ import annotations

import os

import redis as redis_lib
from fastapi import FastAPI
from pydantic import BaseModel

import queue_lib as q

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "ocr-worker"

conn = redis_lib.Redis.from_url(REDIS_URL)
app = FastAPI()


class OcrJobRequest(BaseModel):
    input: str
    output: str
    source_lang: str = "vi"  # "vi" (PaddleOCR detect + VietOCR recognize) | "en"/"fr" (PaddleOCR rec Latin)
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
def submit_job(req: OcrJobRequest) -> dict:
    if req.source_lang not in ("vi", "en", "fr"):
        return {"ok": False, "error": f"source_lang không hợp lệ: {req.source_lang!r} (chỉ nhận 'vi'/'en'/'fr')"}
    job_id = q.new_job(conn, req.model_dump())
    return {"ok": True, "job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = q.get_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "job không tồn tại hoặc đã hết hạn (TTL 48h)"}
    return {"ok": True, "job_id": job_id, **job}
