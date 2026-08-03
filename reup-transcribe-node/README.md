# reup-transcribe-node

Bản kiến trúc job-queue (api + worker, dùng chung `reup-broker`) — VAD + route
Paraformer(zh)/faster-whisper(khác) + punc/align (backend `local`, GPU) hoặc Deepgram fallback
(backend `deepgram`). (Bản one-shot `docker_transcribe` trước đây đã archive vào `legacy/`,
không còn deploy.)

`pipeline.py`/`model_registry.py`/`separator.py`/`residual_qc.py`/`deepgram_backend.py` copy
**không đổi 1 dòng logic** — `pipeline.process()` vốn đã nhận `Path` object thuần, không qua
argparse, nên tái sử dụng thẳng được từ cả CLI cũ lẫn worker mới.

## Khác biệt quan trọng so với `docker_transcribe` (serve-mode FastAPI cũ)

- **`api` và `worker` là 2 container riêng** (trước đây gộp chung 1 process `server.py`) —
  `api` không import `torch`/`pipeline`/`model_registry`, không cần GPU, có thể restart/scale
  độc lập mà không đụng model đang resident trong VRAM của `worker` (đúng nguyên tắc đã review
  ở `review_tts-node.md`).
- **`_job_lock` (threading.Lock) trong `server.py` cũ không còn cần thiết** — vì giờ chỉ có 1
  `worker` process duy nhất xử lý tuần tự từ queue (BLMOVE), tự nhiên đã là "1 job/lúc" không
  cần khoá thủ công.
- **`models_resident` không còn lộ qua `/health`** — `api` không cùng process với `worker` nên
  không đọc trực tiếp được `model_registry` in-memory nữa (khác bản `server.py` cũ). `/health`
  giờ chỉ báo `worker_alive` (heartbeat TTL) + độ dài queue. Đây là đánh đổi chấp nhận được ở
  Phase 1 — nếu cần chi tiết hơn, có thể thêm sau (worker tự ghi `models_resident` vào 1 Redis
  key riêng, giống cách `tts-node` publish `voices:catalog`).

## Build & chạy (bắt buộc trên máy có GPU — `ssh hmtran@100.99.150.90`, không chạy trên máy sửa code)

```bash
cd /u01/reup_tool/docker_build/reup-broker && docker compose up -d   # nếu chưa up

cd /u01/reup_tool/docker_build/reup-transcribe-node
docker compose build
docker compose up -d
```

`api` không cần GPU (không khai báo `deploy.reservations.devices`) — chỉ `worker` cần.

## API

### `POST /jobs`

```bash
curl -s -X POST http://localhost:8104/jobs -H 'Content-Type: application/json' -d '{
  "input": "/source/video.mp4", "output": "/outputs/transcript.json",
  "align": false, "mode": "review"
}'
```

`mode`: `"review"` (mặc định, không đổi hành vi cũ) | `"dialogue"` (giữ nền, chỉ hỗ trợ
`TRANSCRIBE_BACKEND=local`, sinh thêm `<output>_instrumental.wav`/`<output>_original.wav`).
Field tuỳ chọn `pipeline_id`/`video_name` (correlation ID cho structured log — xem mục dưới).

### `GET /jobs/{id}`, `GET /health`

## Structured log 2 ngày

`worker` ghi JSONL vào `./data/logs/YYYY-MM-DD.jsonl` (mount `/logs`): `job_start`/`job_done`/
`job_failed` mỗi job, và ở nhánh `mode=dialogue`, **1 dòng `residual_qc` cho mỗi segment**
(`segment_id`, `start`, `end`, `residual_risk`) — dùng để tra đoạn nào bị nghi dư âm giọng
Trung khi debug lỗi noise/echo theo mốc thời gian video. Giữ 2 ngày, tự xoá qua thread nền. Tắt
bằng env `EVENT_LOG_ENABLED=false` (mặc định `true`).

## Chạy CLI tay (debug, không qua queue — chỉ có backend Deepgram one-shot cũ)

```bash
docker compose run --rm api cli -i /source/video.mp4 -o /outputs/transcript.json
```

## Env quan trọng

Giống hệt `docker_transcribe` gốc: `TRANSCRIBE_BACKEND`, `MODEL_IDLE_TIMEOUT_SECONDS`,
`GPU_MEMORY_FRACTION`, `DEEPGRAM_API_KEY`/`MODEL`/`LANGUAGE`. Xem `docker_transcribe/README.md`
để hiểu chi tiết pipeline (không đổi gì ở lớp này).

## Volume

`./data/cache`, `./data/source`, `./data/outputs`, `./dialogue.yaml`, `./data/logs` (structured
log) — bind-mount, giống convention gốc. Chỉ mount ở `worker` (chỉ worker đọc/ghi file thật).

## Troubleshooting

Xem `reup-translate-node/README.md` (pattern api/worker/queue chung) + `docker_transcribe/README.md`
gốc (VRAM/OOM/dialogue-mode — không đổi gì ở lớp pipeline).
