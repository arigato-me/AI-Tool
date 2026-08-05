"""FastAPI — chỉ nói chuyện với Redis, không import logic dịch nặng (không cần thiết,
translate không có model để load, nhưng giữ đúng nguyên tắc tách api/worker của tts-node:
api luôn nhẹ, restart/scale độc lập với worker)."""
from __future__ import annotations

import os

import redis as redis_lib
from fastapi import FastAPI
from pydantic import BaseModel

import queue_lib as q

REDIS_URL = os.environ.get("REDIS_URL", "redis://reup-redis:6379/0")
ROLE = "translate-worker"

conn = redis_lib.Redis.from_url(REDIS_URL)
app = FastAPI()


class TranslateJobRequest(BaseModel):
    input: str
    output: str
    api_key: str | None = None
    model: str | None = None
    base_url: str | None = None
    target_lang: str = "tiếng Việt"
    batch_size: int = 20
    pipeline_id: str | None = None  # correlation ID cho log — orchestrator truyền vào, không bắt buộc
    video_name: str | None = None
    # Gán vai (narrator/tên nhân vật) mỗi segment — CHỈ orchestrator bật cho nhánh sách
    # (mode="book", xem translate_cli.py::tag_speakers). Mặc định tắt, không đổi hành vi nhánh
    # video review/dialogue/subtitle.
    tag_speakers: bool = False


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": q.SERVICE,
        "worker_alive": q.worker_alive(conn, ROLE),
        **q.queue_depth(conn),
    }


@app.post("/jobs")
def submit_job(req: TranslateJobRequest) -> dict:
    payload = req.model_dump()
    payload["api_key"] = payload["api_key"] or os.environ.get("DEEPSEEK_API_KEY")
    payload["model"] = payload["model"] or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    payload["base_url"] = payload["base_url"] or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if not payload["api_key"]:
        return {"ok": False, "error": "thiếu Deepseek API key (--api_key trong body hoặc env DEEPSEEK_API_KEY)"}

    job_id = q.new_job(conn, payload)
    return {"ok": True, "job_id": job_id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = q.get_job(conn, job_id)
    if job is None:
        return {"ok": False, "error": "job không tồn tại hoặc đã hết hạn (TTL 48h)"}
    job["payload"] = {k: v for k, v in job["payload"].items() if k != "api_key"}
    return {"ok": True, "job_id": job_id, **job}
