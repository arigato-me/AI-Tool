# Review: `tts-service/tts-node` (remote `hmtran@100.99.150.90:/home/hmtran/Projects/tts-service`)

Ngày review: 2026-07-16. Khảo sát qua SSH: đọc toàn bộ source (`app/`), `docker-compose.yml`,
2 Dockerfile, `pyproject.toml`, README, `docs/API_GUIDE.md`, kiểm tra container đang chạy
(`docker ps`, `docker inspect`, `nvidia-smi`) và gọi thử API thật (`/`, `/health`).

## 1. Tổng quan

`tts-node` là **API TTS dạng job-queue** bọc quanh VieNeu-TTS (bản GPU), tách biệt hoàn
toàn với cách `docker_build/docker_vieneu-tts_gpu` (repo hiện tại của tao/mày) làm: thay vì
1 container CLI one-shot (`docker compose run --rm ... tts/segments`), nó là **service thường
trực** (`up -d`), nhận HTTP request, xử lý bất đồng bộ, trả kết quả qua polling hoặc webhook.

Repo `tts-service` chứa 2 thư mục:
- `tts-node/` — code của service này (Python, FastAPI + worker), có `.git` riêng (không thấy
  trong khảo sát — thực ra kiểm tra thì `tts-node` **không phải git repo**, không có lịch sử
  commit).
- `VieNeu-TTS/` — **clone riêng** của cùng upstream VieNeu-TTS (`github.com/pnnbao97/VieNeu-TTS`,
  commit mới nhất `74823c7`, 2026-07-13) dùng làm **path dependency** (`uv.sources: vieneu = {path
  = "../VieNeu-TTS"}`). Đây là bản clone thứ 2, độc lập với `docker_build/repo_github/VieNeu-TTS`
  — 2 bản có thể lệch version nếu chỉ `git pull` 1 trong 2 chỗ.

## 2. Kiến trúc 3 container

```
        POST /tts                    GET /jobs/{id}, /voices, /health, /dashboard
           │                                    ▲
           ▼                                    │
      ┌─────────┐     RPUSH job:{id}      ┌──────────┐
      │   api    │ ───────────────────────▶│  redis   │◀────────────┐
      │ FastAPI  │   queue:pending (list)  │ 7-alpine │              │
      │ no GPU   │                          │ AOF      │              │
      └─────────┘                          └────┬─────┘              │
                                                  │ BLMOVE             │
                                                  │ pending→processing │
                                                  ▼                    │
                                            ┌──────────┐               │
                                            │  worker  │───────────────┘
                                            │ 1 GPU    │  update job hash,
                                            │ Vieneu   │  publish voices:catalog,
                                            │ v3turbo  │  heartbeat, webhook
                                            └──────────┘
```

- **`redis`** (`redis:7-alpine`, AOF `everysec`): vừa là **message queue** (2 Redis List:
  `queue:pending` / `queue:processing`, dùng `BLMOVE` — job không bao giờ "mất" giữa lúc pop
  và ack, nếu worker crash giữa chừng job vẫn nằm trong `queue:processing` để
  `recover_stale_jobs()` nhặt lại lúc worker khởi động lại) vừa là **job store** (Redis Hash
  `job:{id}` chứa toàn bộ state + kết quả) vừa là **pub board** cho voice catalog
  (`voices:catalog`), heartbeat worker (`worker:heartbeat`, TTL), và **log buffer** (2 Redis
  List `logs:api`/`logs:worker`, capped 500/1000 dòng, để dashboard tail log mà không cần
  quyền truy cập `docker logs`).
- **`api`**: FastAPI thuần Python, **không import torch/vieneu**, không cần GPU — chỉ nói
  chuyện với Redis. `POST /tts` tạo job (`uuid4 hex`), `LPUSH` vào queue, trả `202` ngay
  (không đợi xử lý). Các route khác (`/jobs/{id}`, `/voices`, `/health`, `/dashboard`, `/guide`)
  đều đọc từ Redis.
- **`worker`**: load `Vieneu(mode="v3turbo")` **1 lần lúc start**, giữ model cố định trong
  VRAM, sau đó vòng lặp vô hạn: `BLMOVE` (block chờ job, timeout 5s) → xử lý tuần tự (**1 job
  tại 1 thời điểm, không có nội bộ song song**) → ghi kết quả `.wav` vào volume dùng chung với
  `api` → cập nhật job hash → dispatch webhook nếu có `callback_url`.

Điểm hay: `api` và `worker` **không gọi trực tiếp lẫn nhau** — toàn bộ giao tiếp qua Redis,
nên có thể restart/scale từng bên độc lập mà không rơi request (miễn Redis còn sống). Đây là
mô hình **producer/consumer** kinh điển, khác hẳn kiểu orchestrate qua `docker.sock` +
`Execute Command` node mà `docker_build/docker_n8n` đang dùng.

## 3. Build & Dockerfile

- Cả `Dockerfile.api` và `Dockerfile.worker` đều bắt buộc `build.context: ..` (context =
  `tts-service/`, thư mục cha) — **giống hệt convention** `docker_build/CLAUDE.md` đã ghi cho
  các service ở repo hiện tại (COPY chéo từ thư mục sibling). Cùng 1 pattern kiến trúc, khác
  dự án.
- `Dockerfile.api`: base `python:3.12-slim`, dùng `uv sync --no-install-project` trước rồi
  mới `COPY app/` — tách layer cache: đổi code Python không phải cài lại toàn bộ dependency.
  **Vẫn `COPY` `VieNeu-TTS/src`** dù không dùng, chỉ để `uv` resolve được `uv.lock` chung (lock
  file gộp cả 2 dependency-group `default` + `worker`) — image nhẹ vì `uv sync` (không kèm
  `--group worker`) chỉ cài nhóm mặc định.
- `Dockerfile.worker`: base `nvidia/cuda:12.8.0-runtime-ubuntu24.04`, cài `python3.12` qua
  `deadsnakes` PPA (base image CUDA gốc không có sẵn Python), `espeak-ng` (phonemizer cho
  VieNeu-TTS), `uv sync --group worker` kéo `torch==2.8.0`/`torchaudio` (index riêng
  `pytorch-cu128`) + `transformers`. Image này chắc chắn nặng (CUDA runtime + torch).
- `pyproject.toml` dùng **`dependency-groups`** (PEP 735) tách `worker` (nặng, GPU) khỏi
  dependency mặc định (nhẹ, cho `api`) — cùng 1 project Python, 2 image kích thước rất khác
  nhau. Cách làm gọn hơn hẳn việc tự tay quản 2 `requirements.txt`.

## 4. So với `docker_vieneu-tts_gpu` (repo hiện tại)

| | `docker_build/docker_vieneu-tts_gpu` | `tts-service/tts-node` |
|---|---|---|
| Mô hình | 1 container, CLI one-shot (`tts`/`segments`/`web`), gọi qua `docker compose run --rm` | 3 container thường trực (`api`+`worker`+`redis`), gọi qua HTTP |
| Đồng bộ | Đồng bộ — lệnh chạy xong mới trả kết quả (in JSON ra stdout) | Bất đồng bộ — `202` ngay, kết quả lấy qua polling/webhook |
| Điều phối multi-request | Không có — n8n Execute Command tự chạy tuần tự theo workflow | Có sẵn hàng đợi Redis, nhiều client gọi `POST /tts` cùng lúc vẫn an toàn, tự xếp hàng |
| Trạng thái/lịch sử job | Không lưu — mỗi lần chạy độc lập, không có "job list" | Có (`jobs:recent`, dashboard xem lại lịch sử, tải lại kết quả cũ trong 24h) |
| Giám sát | `healthcheck.sh` (mới thêm) gọi `--list-voices` | `/health`, `/dashboard` (queue length, heartbeat, log tail realtime) |
| Chia sẻ GPU với process khác | `deploy.reservations.devices` full GPU, không giới hạn VRAM | `GPU_MEMORY_FRACTION=0.5` (giới hạn % VRAM chủ động qua `torch.cuda.set_per_process_memory_fraction`) |
| Multi-segment theo timestamp | Có (`segments_cli.py`, silence-padded, mới thêm log cảnh báo overlap) | Không có — `tts-node` chỉ có 1 đoạn text → 1 job → 1 wav, không có khái niệm timeline/transcript |

**tts-node phù hợp hơn cho use-case "nhiều service khác cùng gọi TTS qua mạng nội bộ,
throughput cao, cần theo dõi job"**; `docker_vieneu-tts_gpu` hiện tại phù hợp hơn cho
**pipeline tuần tự 1 lần/video** (đúng như thiết kế reup pipeline 5 bước trong CLAUDE.md,
nơi mỗi bước là 1 lệnh CLI ăn file, không cần hàng đợi).

## 5. Ưu điểm của mô hình 3-container (api/worker/redis)

1. **Tách API khỏi GPU hoàn toàn** — `api` restart/scale/deploy độc lập, không đụng tới model
   đang load trong VRAM của `worker`. Sập `api` không làm mất job đang xử lý.
2. **Hàng đợi bền (durable queue)** — Redis AOF (`--appendonly yes --appendfsync everysec`)
   nghĩa là restart Redis (hoặc cả máy) chỉ mất tối đa ~1 giây dữ liệu, không mất toàn bộ
   queue như hàng đợi in-memory thuần.
3. **Không mất job khi worker crash giữa chừng** — pattern `BLMOVE` + `recover_stale_jobs()`
   là kỹ thuật chuẩn (giống pattern "visibility timeout" của SQS) để đảm bảo *at-least-once*
   processing, có giới hạn số lần retry (`WORKER_MAX_ATTEMPTS`) tránh job lỗi lặp vô hạn.
4. **1 model load 1 lần, dùng lại nhiều job** — tiết kiệm thời gian nạp model (vốn tốn vài
   giây tới vài chục giây với model TTS) so với cách CLI one-shot phải nạp lại model **mỗi lần
   gọi** (`docker compose run` = process mới = load lại `Vieneu` từ đầu). Đây là lợi thế lớn
   nhất về mặt hiệu năng khi có nhiều request liên tục.
   - **Đối chiếu với `docker_vieneu-tts_gpu`**: mỗi lần `docker compose run --rm ... tts` là 1
     lần nạp model mới hoàn toàn — nếu reup pipeline gọi TTS nhiều lần/video (ví dụ retry hoặc
     nhiều video liên tiếp), chi phí nạp model lặp lại là overhead thật, tts-node giải quyết
     triệt để vấn đề này.
5. **Dashboard giám sát tại chỗ** — không cần `docker logs`/`docker exec` để biết queue dài
   bao nhiêu, worker còn sống không (`worker:heartbeat` TTL — quá hạn = coi như chết), lịch sử
   job. Cách tiếp cận native hơn nhiều so với `healthcheck.sh` (chỉ kiểm tra CLI còn chạy được,
   không nói được gì về job đang xử lý).
6. **Webhook có retry/backoff** (`1s, 3s, 9s`, `never raises`) — thiết kế đúng: 1 client
   callback bị lỗi không được phép làm crash cả worker loop, nên `send_webhook` bọc try/except
   ở mọi lớp.
7. **Tách dependency theo `dependency-groups`** — image `api` nhẹ (không cần torch/CUDA),
   giảm attack surface + thời gian build/pull, đúng nguyên tắc "chỉ cài cái cần dùng".
8. **`GPU_MEMORY_FRACTION`** cho phép chủ động nhường VRAM cho tiến trình khác trên cùng GPU —
   quan trọng vì (xem mục 7) máy remote chỉ có 1 GPU 4GB, có khả năng phải chia với chính
   `docker_vieneu-tts_gpu` của `docker_build`.

## 6. Nhược điểm / rủi ro

1. **Không có xác thực** (đã tự ghi rõ trong `docs/API_GUIDE.md`: "hiện tại API không có xác
   thực... chỉ nên expose trong mạng nội bộ"). Không có API key, không HMAC ký webhook — nếu
   lỡ expose port `8000` ra ngoài internet (kể cả vô tình qua reverse proxy), bất kỳ ai cũng
   `POST /tts` tự do (tốn GPU/tiền điện) hoặc đọc audio của job người khác nếu đoán được
   `job_id` (dù `job_id` là `uuid4` nên brute-force khó, nhưng URL "download" không hề kiểm
   tra chủ sở hữu).
2. **Xử lý tuần tự tuyệt đối, không auto-scale** — 1 worker = 1 job/lúc. README tự nhận: "nếu
   cần thông lượng cao hơn, đây là điểm cần bàn trước (không tự động scale)". `docker-compose.yml`
   không set `deploy.replicas`, và có set replicas cũng không chạy được vì `count: all` GPU +
   máy chỉ có 1 GPU vật lý (RTX 3050 4GB) — nhiều worker cùng lúc sẽ tranh nhau VRAM.
3. **`voice` không validate ở bước submit** — job luôn được nhận (`202`) dù `voice` sai, chỉ
   phát hiện lỗi *sau khi* vào tới worker rồi mới đánh `failed`. Chấp nhận được theo giải thích
   trong docs (tránh API phải đồng bộ danh sách giọng) nhưng client phải tự biết quy ước này,
   dễ bị hiểu lầm là job "đang xử lý" trong khi thực ra sắp fail.
4. **Không giới hạn tần suất (rate limit)** — không thấy cơ chế nào chặn 1 client spam
   `POST /tts` liên tục; hàng đợi Redis vô hạn (không có max length), 1 client có thể làm
   nghẽn hàng đợi cho các client khác dùng chung service.
5. **`text` giới hạn 1–5000 ký tự nhưng không chia nhỏ** — văn bản dài (gần 5000 ký tự) tạo ra
   1 job xử lý rất lâu, chiếm worker suốt thời gian đó — không có cơ chế chunk giống
   `segments_cli.py` bên `docker_vieneu-tts_gpu` (vốn xử lý từng segment ngắn theo transcript).
   Không phù hợp trực tiếp cho use-case "TTS cả 1 video dài theo timestamp" của reup pipeline
   — sẽ cần thêm 1 lớp client tự chia + ghép, tương đương việc `segments_cli.py` đã làm sẵn.
6. **`.env` chứa secret nhưng không thấy secret thật nào trong `.env.example`** (không có
   `HF_TOKEN` set) — ổn, nhưng cần đảm bảo `.env` thật trên remote có `chmod 600` giống quy
   ước đang áp dụng cho `docker_translate/.env` trong `docker_build` (chưa kiểm tra quyền file
   `.env` thật của `tts-node`, nên tự rà lại).
7. **Tranh chấp GPU vật lý với `docker_build/docker_vieneu-tts_gpu`** — `nvidia-smi` xác nhận
   remote chỉ có **1 GPU: RTX 3050 Laptop, tổng 4096MiB VRAM** (thực tế PyTorch báo
   `total_memory=3770MB`), và `docker-compose.yml` của `tts-node/worker` request
   `count: all` (toàn bộ GPU host) — **giống hệt** cách `docker_vieneu-tts_gpu` cũng request
   full GPU. Nếu 2 service này chạy `worker`/`vieneu-tts-gpu` cùng lúc mà xử lý job thật (không
   chỉ đứng im), cả 2 sẽ tranh VRAM trên cùng 1 card 4GB — dễ OOM. `GPU_MEMORY_FRACTION=0.5`
   của `tts-node` giảm nhẹ rủi ro (giới hạn ~2GB) nhưng `docker_vieneu-tts_gpu` phía
   `docker_build` **không có cơ chế giới hạn VRAM tương tự** — cần lưu ý khi cả 2 dự án cùng
   chạy trên máy này.
8. **Redis là single point of failure** — mọi thứ (queue, job store, log buffer, voice
   catalog) đi qua 1 Redis instance duy nhất, không cluster/replica. Mất Redis (hoặc volume
   `redis_data` hỏng) = mất toàn bộ trạng thái đang chạy (dù AOF giảm rủi ro mất dữ liệu, không
   giảm rủi ro *service down* khi Redis crash).
9. **`tts-node` không phải git repo** (không có `.git`) — không có lịch sử commit, không rõ ai
   sửa gì lúc nào, khó rollback nếu 1 thay đổi sau này gây lỗi. Ngược lại `VieNeu-TTS` (thư mục
   con) có git riêng, dẫn tới tình trạng nửa-quản-lý-được nửa-không trong cùng 1 project.
10. **2 bản clone `VieNeu-TTS` độc lập trên cùng máy** (`tts-service/VieNeu-TTS` và
    `docker_build/repo_github/VieNeu-TTS`) — dễ lệch version nếu chỉ update 1 bên, 2 dự án có
    thể chạy 2 phiên bản model khác nhau mà không ai để ý (không có gì báo hiệu sự lệch pha
    này).
11. **File kết quả `.wav` tồn tại vĩnh viễn trên volume dù job hash đã hết TTL** — `mark_done`
    chỉ `expire()` cái Redis Hash `job:{id}` (24h), nhưng **không xoá file `.wav` tương ứng**
    trong volume `audio_output`. Sau TTL, `GET /jobs/{id}` trả 404 nhưng file audio vẫn nằm
    trên đĩa mãi mãi — rò rỉ dung lượng dần theo thời gian, không có cơ chế dọn dẹp (cron/TTL ở
    tầng file).

## 7. Dung lượng đĩa thực tế & so sánh cách lưu trữ với `docker_build` hiện tại

Đo trực tiếp trên remote qua container `alpine` tạm mount 3 named volume của `tts-node`
(không đọc trực tiếp được `/var/lib/docker/volumes/...` vì không có quyền `sudo` non-interactive
qua SSH):

| Volume (`tts-node`) | Dung lượng | Nội dung |
|---|---|---|
| `tts-node_redis_data` | **3.3M** | AOF file — queue + job hash + log buffer, nhỏ vì Redis chỉ lưu text/metadata |
| `tts-node_audio_output` | **59.6M** | ~20 file `.wav` kết quả job (556K–5.7M/file) + thư mục `previews/` (3.6M, 1 preview/giọng, cache vĩnh viễn) |
| `tts-node_huggingface_cache` | **388.8M** | Model weights VieNeu-TTS v3 Turbo tải từ HuggingFace Hub |
| **Tổng** | **~452M** | |

Đối chiếu bind-mount của `docker_build` trên cùng máy (`/home/hmtran/Projects/docker_build`):

| Thư mục (`docker_build/docker_vieneu-tts_gpu`) | Dung lượng | Nội dung |
|---|---|---|
| `data/cache` | **389M** | Model weights — **gần như y hệt** `tts-node_huggingface_cache` (388.8M) vì cùng model VieNeu-TTS v3 Turbo, cùng cơ chế `HF_HOME` cache |
| `data/outputs` | **699M** | Toàn bộ output tích luỹ qua các lần test (`tts`/`segments`), gồm cả file test cũ chưa dọn |
| `data/source` | 60K | File input text/JSON, không đáng kể |
| `docker_n8n/data` | 5.5M | SQLite DB workflow n8n |
| **Tổng `docker_build`** | **~1.4G** | |

**So sánh mô hình lưu trữ:**

1. **Cùng lượng model cache (~389M mỗi bên)** vì cùng dùng VieNeu-TTS v3 Turbo — nhưng đang
   **cache 2 lần độc lập** trên cùng máy (`tts-node_huggingface_cache` và
   `docker_vieneu-tts_gpu/data/cache`), không chia sẻ. Nếu 2 project cùng chạy dài hạn trên
   `100.99.150.90`, đây là ~389M lãng phí có thể gộp lại (ví dụ trỏ chung 1
   `HF_HOME`/volume) — không bắt buộc phải sửa, nhưng đáng cân nhắc nếu máy giới hạn dung
   lượng đĩa.
2. **Named volume (Docker-managed) vs bind-mount**: `tts-node` dùng `volumes: {redis_data,
   audio_output, huggingface_cache}` — Docker tự quản lý, nằm trong
   `/var/lib/docker/volumes/`, **không sudo thì không đọc/xoá trực tiếp được từ host** (như
   vừa gặp ở trên). `docker_build` dùng bind-mount tương đối (`./data/...`) — nằm ngay trong
   repo, `ls`/`rm`/`du` trực tiếp không cần quyền đặc biệt, dễ backup/inspect/xoá thủ công
   hơn, nhưng đổi lại **phải tự quản lý path đúng theo host** (đã từng gây bug thật khi remote
   đổi path repo — xem `note_runtest_v1.md` §8). Named volume tránh được đúng loại bug path đó
   (Docker tự nhớ mountpoint nội bộ, không phụ thuộc đường dẫn tương đối của service gọi nó).
3. **`docker_build/data/outputs` (699M) phình to hơn hẳn `tts-node_audio_output` (59.6M)**
   không phải vì mô hình tệ hơn, mà vì **không có TTL/dọn dẹp tự động ở cả 2 bên** — khác biệt
   chủ yếu do lịch sử test tích luỹ (`docker_build` đã test nhiều vòng, nhiều file `.mp4`/`.wav`
   nặng hơn từ pipeline full video, trong khi `tts-node` mới có ~20 job TTS ngắn). Cả 2 mô hình
   đều **thiếu cơ chế tự xoá output cũ** — với `tts-node` đã ghi ở mục 6.11 (job hash hết TTL
   nhưng file `.wav` ở lại vĩnh viễn); với `docker_build`, `data/outputs` các service cũng
   không có TTL/cron dọn, chỉ có thể tăng dần theo số lần test/chạy thật.
4. **Redis AOF rất nhẹ (3.3M)** so với tổng thể — mô hình job-queue của `tts-node` không tốn
   nhiều đĩa cho phần "state", phần lớn dung lượng luôn là model cache + audio output ở cả 2
   mô hình, không phải do bản thân cơ chế queue/orchestrate.

## 8. Kết luận nhanh

Mô hình `tts-node` (api/worker/redis) là 1 kiến trúc **job-queue chuẩn, làm đúng bài bản**
(BLMOVE reliable-queue, model-load-once, webhook có retry, dashboard giám sát) — hợp lý cho
kịch bản "TTS-as-a-service" dùng chung bởi nhiều client. Nó **không thay thế** mô hình CLI
one-shot của `docker_vieneu-tts_gpu` trong `docker_build` (vốn tối ưu cho pipeline tuần tự 5
bước, mỗi bước ăn file/transcript cụ thể) mà là 1 lựa chọn kiến trúc khác cho 1 bài toán khác.
Nếu sau này `docker_build`'s reup pipeline cần TTS tần suất cao / nhiều video song song, đây
là tài liệu tham khảo tốt để thiết kế lại theo hướng service thường trực thay vì
`docker compose run` mỗi lần.

Rủi ro đáng chú ý nhất trong ngắn hạn: **(7) tranh chấp GPU 4GB** nếu cả 2 dự án cùng chạy
trên máy `100.99.150.90`, và **(1) không có xác thực** nếu port `8000` từng bị expose ra ngoài
mạng nội bộ.
