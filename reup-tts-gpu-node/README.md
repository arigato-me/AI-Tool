# reup-tts-gpu-node

Bản kiến trúc job-queue (api + worker, dùng chung `reup-broker`) — TTS tiếng Việt (VieNeu-TTS,
GPU/PyTorch). (Bản one-shot `docker_vieneu-tts_gpu`, gồm cả chế độ `web` Gradio, đã archive vào
`legacy/`, không còn deploy.) `docker_vieneu-tts` (CPU/ONNX) **chưa từng có bản job-queue tương
đương** và cũng đã archive — hiện không có lựa chọn TTS nào chạy được mà không cần GPU.

## Khác biệt quan trọng so với CLI one-shot cũ

- **`Vieneu()` chỉ load 1 lần lúc `worker` khởi động, giữ resident xuyên suốt mọi job** — CLI
  cũ (`docker compose run --rm ... tts/segments`) tạo model mới **mỗi lần gọi** (tốn thời gian
  nạp model mỗi request); worker mới tái sử dụng đúng 1 instance, đúng lợi ích lớn nhất của
  kiến trúc job-queue đã ghi trong `review_tts-node.md`.
- **`api` không import `vieneu`/`torch`** — không cần GPU, restart/scale độc lập với worker.
- Thêm `GPU_MEMORY_FRACTION` (mặc định `0.5`, giống `docker_transcribe`) — trước đây
  `docker_vieneu-tts_gpu` không giới hạn VRAM chủ động (khoảng trống đã ghi nhận ở
  `review_tts-node.md` mục 4), giờ port thêm để nhất quán với node GPU còn lại, giảm rủi ro
  tranh chấp VRAM với `reup-transcribe-node` trên cùng GPU 4GB.
- Không còn chế độ `web` (Gradio UI thủ công) — kiến trúc mới hướng tới gọi qua API/queue,
  không cần UI thử giọng riêng ở node này (vẫn dùng `docker_vieneu-tts_gpu` cũ nếu cần).

## Build & chạy (bắt buộc trên máy có GPU — `ssh hmtran@100.99.150.90`, không chạy trên máy sửa code)

```bash
cd /u01/reup_tool/docker_build/reup-broker && docker compose up -d   # nếu chưa up

cd /u01/reup_tool/docker_build/reup-tts-gpu-node
docker compose build
docker compose up -d
```

## API — `POST /jobs` body `{"cmd": "tts"|"segments"|"list-voices", "params": {...}}`

**`tts`** — params: `input_path`, `output_path` (bắt buộc), `voice`, `style` (tùy chọn, mặc
định lấy từ env `VIENEU_VOICE`/`VIENEU_STYLE`):

```bash
curl -s -X POST http://localhost:8105/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "tts",
  "params": {"input_path": "/source/text.txt", "output_path": "/outputs/out.wav"}
}'
```

**`segments`** — params: `input_path`, `output_path` (bắt buộc), `voice`, `style` (tùy chọn) —
transcript JSON (`{"segments":[{"start","end","text"}]}`) -> 1 wav ghép theo timestamp:

```bash
curl -s -X POST http://localhost:8105/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "segments",
  "params": {"input_path": "/source/translated.json", "output_path": "/outputs/track.wav"}
}'
```

**`list-voices`** — params rỗng, trả danh sách built-in voices.

Cả `tts` và `segments` nhận thêm params tuỳ chọn:
- `ref_audio`: path file audio mẫu (3-5 giây, khuyến nghị WAV) để **clone giọng** — dùng thẳng
  `engine.infer(text, ref_audio=...)` của v3turbo (voice cloning có sẵn trong engine, không cần
  enroll giọng trước). Khi set, **bỏ qua hẳn** `voice`/preset — không cần có built-in voice nào.
  Kết quả trả `voice: "clone:<tên_file_ref_audio>"` thay vì tên preset.
- `pipeline_id`/`video_name` (correlation ID cho structured log — xem mục dưới).

```bash
curl -s -X POST http://localhost:8105/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "segments",
  "params": {"input_path": "/source/translated.json", "output_path": "/outputs/track.wav",
             "ref_audio": "/source/my_voice_sample.wav"}
}'
```

### `GET /jobs/{id}`, `GET /health`

`result` khi `finished` giữ đúng field cũ: `synthesis_time_s` (thời gian tính toán) và
`audio_duration_s` (độ dài audio thật, `len(audio) / tts.sample_rate`) — **không nhầm lẫn 2
field này** (bug này từng có ở bản CPU, đã fix ở bản GPU — xem CLAUDE.md).

## Structured log 2 ngày

`worker` ghi JSONL vào `./data/logs/YYYY-MM-DD.jsonl` (mount `/logs`): `job_start`/`job_done`/
`job_failed` mỗi job (kèm `cmd`), và ở `segments`, **1 dòng `tts_segment` cho mỗi segment**
(`segment_id`, `start`, `end`, `text_chars`, `audio_duration_s`, `slot_duration_s`,
`overrun_s`) — dùng để tra đoạn nào TTS bị cắt/tràn slot khi debug lỗi mute/audio bất thường
theo mốc thời gian video. Giữ 2 ngày, tự xoá qua thread nền. Tắt bằng env
`EVENT_LOG_ENABLED=false` (mặc định `true`).

## Chạy CLI tay (debug, không qua queue — tự tạo model mới mỗi lần, không giữ resident)

```bash
docker compose run --rm api tts -i /source/text.txt -o /outputs/out.wav
docker compose run --rm api segments -i /source/translated.json -o /outputs/track.wav
```

## Nghe thử giọng (`samples` mode)

Tạo sẵn 1 file wav ~15-20s cho **mỗi** built-in voice (`scripts/generate_voice_samples.py`) —
mỗi voice đọc đúng nội dung khớp `style` đã gán sẵn trong `voices_v3_turbo.json` (bản tin cho
`tin_tuc`, truyện kể cho `doc_truyen`, đời thường cho `tu_nhien`), thể hiện đúng điểm mạnh riêng
của từng voice thay vì 1 văn bản trung tính dùng chung. Không phải job queue, chạy thủ công 1
lần (hoặc lại mỗi khi `voices_v3_turbo.json` đổi preset) trên máy có GPU
(`ssh hmtran@100.99.150.90`) qua service `worker` (cần GPU reservation, khác `api`/`tts`/
`segments` ở trên):

```bash
docker compose run --rm worker samples
```

Ghi ra `./data/voice_samples/<voice_id>.wav`, được `reup-orchestrator-node` mount read-only và
serve qua `GET /voices/{id}/sample` để `reup-ui` phát trực tiếp (`<audio>`) ngay khi người dùng
chọn giọng trong form submit — xem `reup-orchestrator-node/README.md` mục tương ứng.

## Volume

`./data/cache` (HF model cache), `./data/source`, `./data/outputs`, `./data/logs` (structured
log), `./data/voice_samples` (sample nghe thử giọng, xem mục trên) — bind-mount, chỉ mount ở
`worker` (chỉ worker đọc/ghi file thật + cache model).

## Troubleshooting

Xem `reup-translate-node/README.md` (pattern api/worker/queue chung) + `docker_vieneu-tts_gpu/README.md`
gốc (voice/style, VRAM sizing) — không đổi gì ở lớp inference.
