# reup-translate-node

Bản kiến trúc job-queue (api + worker, dùng chung `reup-broker`) — dịch transcript JSON sang
tiếng Việt qua Deepseek API. (Bản one-shot `docker_translate` trước đây đã archive vào
`legacy/`, không còn deploy.)

Logic dịch (retry/recursive-split khi Deepseek trả sai — xem `translate_cli.py`) giữ nguyên
100%, chỉ tách phần `main()`/argparse ra thành hàm `run_translate()` để cả CLI lẫn worker dùng
chung.

## Kiến trúc

```
POST /jobs, GET /jobs/{id}, GET /health
        │                        ▲
        ▼                        │
   ┌─────────┐   RPUSH      ┌──────────┐
   │   api   │─────────────▶│  redis   │◀──────────┐
   │ FastAPI │  queue:      │(reup-    │            │
   │         │  pending:    │ broker)  │            │
   └─────────┘  translate   └────┬─────┘            │
                                  │ BLMOVE            │
                                  ▼                   │
                            ┌──────────┐              │
                            │  worker  │──────────────┘
                            │ (1 job   │ ghi job hash,
                            │  /lúc)   │ heartbeat
                            └──────────┘
```

Theo đúng pattern đã review ở `review_tts-node.md` (gốc repo `docker_build/`): Redis List thô +
`BLMOVE` (reliable queue, job không mất nếu worker crash giữa chừng — `recover_stale_jobs()`
chạy lúc worker khởi động lại), job state ở Redis Hash `job:translate:{id}` (TTL 48h). `queue_lib.py`
là module dùng chung viết 1 lần, copy y hệt sang mỗi node khác trong kiến trúc mới (không share
qua import chéo — đúng nguyên tắc standalone scaffold của repo).

## Structured log 2 ngày (debug chất lượng dịch)

`worker` ghi JSONL vào `./data/logs/YYYY-MM-DD.jsonl` (mount `/logs`): `job_start`/`job_done`/
`job_failed` cho mỗi job, và **1 dòng `translate_segment` cho mỗi segment** (`segment_id`,
`start`, `end`, `text_original`, `text`) — dùng để tra lại chính xác câu nào dịch sai khi biết
mốc thời gian trong video. Mọi dòng đều có `pipeline_id`/`video_name` nếu job được gọi từ
`reup-orchestrator-node` (gọi trực tiếp qua `POST /jobs` không kèm 2 field này thì log
`pipeline_id: null`). Giữ log 2 ngày, tự xoá qua thread nền `_log_retention_loop`. Tắt log bằng
env `EVENT_LOG_ENABLED=false` (mặc định `true`) trong `docker-compose.yml`, không ảnh hưởng xử
lý job (đã test).

## Build & chạy

```bash
# 1. Broker phải up trước (tạo network reup-net)
cd /u01/reup_tool/docker_build/reup-broker && docker compose up -d

# 2. Build + chạy node này
cd /u01/reup_tool/docker_build/reup-translate-node
docker compose build
docker compose up -d
```

> Build context là thư mục `docker_build` (cha), giống mọi service khác trong repo.

## API

### `POST /jobs` — submit job dịch

```bash
curl -s -X POST http://localhost:8101/jobs \
  -H 'Content-Type: application/json' \
  -d '{"input": "/source/transcript.json", "output": "/outputs/translated.json"}'
# -> {"ok": true, "job_id": "..."}
```

Field tuỳ chọn: `api_key`/`model`/`base_url` (mặc định lấy từ `.env`), `target_lang` (mặc định
`"tiếng Việt"`), `batch_size` (mặc định `20`), `pipeline_id`/`video_name` (correlation ID cho
structured log — `reup-orchestrator-node` tự gắn khi gọi qua pipeline, không bắt buộc khi gọi
tay).

### `GET /jobs/{id}` — poll trạng thái

```bash
curl -s http://localhost:8101/jobs/<job_id>
# -> {"ok": true, "job_id": "...", "status": "pending|started|finished|failed",
#     "result": {...} | null, "error": "" | "..."}
```

`result` khi `finished` có đúng schema JSON mà CLI cũ in ra stdout (`ok`, `input`, `output`,
`segments`, `elapsed_s`) — không đổi contract, downstream không cần sửa gì nếu đọc field này.

### `GET /health`

```bash
curl -s http://localhost:8101/health
# -> {"ok": true, "service": "translate", "worker_alive": true, "pending": 0, "processing": 0}
```

## Chạy CLI tay (debug, không qua queue)

```bash
docker compose run --rm api cli \
  -i /source/transcript.json -o /outputs/translated.json
```

## Volume

`./data/source`, `./data/outputs`, `./data/logs` (structured log JSONL, xem mục trên) — bind-mount,
giống convention `docker_translate` gốc.

## Troubleshooting

| Vấn đề | Cách xử lý |
|---|---|
| `POST /jobs` báo thiếu API key | Set `DEEPSEEK_API_KEY` trong `.env` (copy từ `.env.example`) |
| `worker_alive: false` ở `/health` | Container `worker` chưa `up` hoặc đã crash quá 30s (`docker compose logs worker`) |
| Job kẹt ở `pending` mãi | Kiểm tra container `worker` đang chạy và có kết nối được `reup-redis` không (`docker compose logs worker`) |
| Không kết nối được `reup-redis` | `reup-broker` chưa `up -d`, hoặc network `reup-net` chưa tồn tại — up `reup-broker` trước |
