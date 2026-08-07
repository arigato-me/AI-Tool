# reup-orchestrator-node

Chạy full pipeline (`ytdlp → transcribe → translate → tts-gpu segments → editor srt/edit`, cả 2
nhánh **review**/**dialogue** như CLAUDE.md "Reup pipeline") bằng cách gọi HTTP API của 5
`reup-*-node` đã có, **không dùng Docker-out-of-Docker/`docker.sock`** — không phụ thuộc gid
nhóm `docker` hay `NODES_EXCLUDE` nào cả. (`docker_n8n`, cơ chế sequencing kiểu Execute-Command
trước đây, đã archive vào `legacy/`, không còn deploy.)

## Kiến trúc

- `api` + `worker` dùng chung `reup-broker`, đúng pattern `queue_lib.py` (BLMOVE) như 5 node
  kia — nhưng job ở đây là **"pipeline job"** (1 job = chạy hết 1 video qua tất cả các bước),
  không phải job đơn lẻ như các node kia.
- **Chỉ `worker` mới gọi các node khác** (qua `node_client.submit_and_wait()`, submit +
  poll `POST /jobs`/`GET /jobs/{id}` — đúng hợp đồng chung mọi node Phase 1). `api` chỉ nói
  chuyện với Redis, không có logic gọi node — restart/scale độc lập.
- **Chuyển file giữa các bước**: mỗi node Phase 1 chỉ thấy `data/source`/`data/outputs` của
  chính nó. `worker` của node này bind-mount thêm các thư mục đó của cả 5 node kia (xem
  `docker-compose.yml`), tự `shutil.copy2` giữa chúng trên cùng host — không sửa gì ở 5 node
  kia. Tên file trung gian luôn có tiền tố `pipeline_id` để không đụng nhau giữa các lần chạy.
- **1 pipeline chạy tại 1 thời điểm** (worker loop tuần tự) — tránh tranh chấp VRAM giữa
  `reup-transcribe-node`/`reup-tts-gpu-node` trên cùng GPU 4GB.
- **Phải deploy trên remote GPU host**, cùng chỗ với 5 node kia — gọi nhau qua tên container
  trong network Docker `reup-net`, chỉ resolve được trong cùng 1 Docker daemon.

## Build & chạy (bắt buộc trên máy có 5 node Phase 1 + `reup-broker` đã `up` — `ssh hmtran@100.99.150.90`, không chạy trên máy sửa code)

```bash
cd /u01/reup_tool/docker_build/reup-orchestrator-node
docker compose build
docker compose up -d
```

## API

### `POST /pipelines`

```bash
curl -s -X POST http://localhost:8106/pipelines -H 'Content-Type: application/json' -d '{
  "url": "https://www.douyin.com/video/...",
  "mode": "review"
}'
# -> {"ok": true, "pipeline_id": "..."}
```

Field tuỳ chọn: `video_name` (tên chuẩn xuyên suốt pipeline — prefix mọi file trung gian, và
file xuất cuối luôn có tag nhánh: `review_<tên>.mp4`/`dialogue_<tên>.mp4`, hoặc
`review_final.mp4`/`dialogue_final.mp4` nếu không đặt tên), `ytdlp_args` (list, thêm vào lệnh
yt-dlp), `align` (transcribe), `voice`/`style` (tts-gpu, xem `GET /voices`/`GET /styles`),
`ref_audio_b64`/`ref_audio_ext` (base64 file audio mẫu 3-5s để **clone giọng** — khi set, bỏ
qua `voice`, dùng chung cho toàn bộ video), `subtitle_mode` (`none`/`soft`/`burn`, mặc định
`burn`), `target_lang`/`batch_size` (translate).

**`mode="audio"`** — nhánh riêng, chỉ tải + xuất mp3, không transcribe/translate/tts/editor.
Chỉ cần `url` (+ `video_name`/`ytdlp_args` tuỳ chọn) — mọi field khác (`voice`, `subtitle_mode`,
nhạc nền, ...) bị bỏ qua. Dừng ngay sau bước `ytdlp` (gọi kèm `-x --audio-format mp3`, ffmpeg có
sẵn trong image `reup-ytdlp-node`), `result.stages` chỉ có 1 entry `ytdlp`. Output:
`/outputs/<pipeline_id>/audio_<tên>.mp3` (hoặc `audio_final.mp3` nếu không đặt `video_name`).

**`mode="video"`** — y hệt `mode="audio"` (chỉ tải, dừng ngay sau `ytdlp`, chỉ cần `url` +
`video_name`/`ytdlp_args` tuỳ chọn, mọi field khác bị bỏ qua) nhưng **không** ép `-x
--audio-format mp3` — giữ nguyên file yt-dlp tải về. Output:
`/outputs/<pipeline_id>/video_<tên><đuôi gốc>` (đuôi theo đúng file yt-dlp tải về, thường
`.mp4`, có thể `.webm`/`.mkv` tuỳ nguồn — không ép cứng `.mp4`).

**Nhạc nền** (`music_preset`/`music_b64`+`music_ext`/`music_project`+`music_track`/
`music_level`) — chỉ áp dụng khi `mode="review"` (nhánh dialogue đã có `instrumental` tách từ
audio gốc, không cần thêm nhạc ngoài). Thứ tự ưu tiên khi gửi nhiều field cùng lúc: `music_b64`
(upload tức thời lúc submit) > `music_project`+`music_track` (chọn từ thư viện nhạc, xem
`GET /music/projects*` dưới) > `music_preset` (tên file phẳng legacy, đặt sẵn trong
`reup-editor-node/data/music`). Có field nhạc nào thì thêm 1 bước `editor_mix_music` vào
`result.stages`, không gửi field nào thì bỏ qua bước này (giữ hành vi cũ).

**`mode="mix"`** — ghép N video nối tiếp + N audio nối tiếp thành 1 video final, KHÔNG
transcribe/dịch/TTS/sub gì cả (khác hẳn review/dialogue/subtitle). Không dùng `url` đơn — dùng
`video_items`/`audio_items` (list, bắt buộc ≥1 mỗi bên):

```bash
curl -s -X POST http://localhost:8106/pipelines -H 'Content-Type: application/json' -d '{
  "mode": "mix",
  "video_name": "demo",
  "video_items": [
    {"type": "url", "url": "https://www.douyin.com/video/..."},
    {"type": "reuse", "pipeline_id": "8f2a1c...cũ đã tải"}
  ],
  "audio_items": [
    {"type": "library", "music_project": "chill", "music_track": "lofi-1.mp3"}
  ]
}'
```

Mỗi item trong `video_items`/`audio_items` là 1 object `{"type": ...}`:

| type | field kèm theo | Ý nghĩa | Áp dụng |
|---|---|---|---|
| `url` | `url` | Tải qua yt-dlp (Douyin/YouTube/TikTok/FB...) — audio item kiểu này tự thêm `-x --audio-format mp3` lúc tải (ffmpeg tự tách audio) | video + audio |
| `upload` | `data_b64`, `ext` | Upload tay 1 file base64 | video + audio |
| `reuse` | `pipeline_id` | Tái dùng file đã tải của 1 pipeline cũ (file còn nằm ở `reup-ytdlp-node/data/downloads`, KHÔNG tạo job ytdlp mới, không tải lại) | video + audio |
| `library` | `music_project`, `music_track` | Track có sẵn trong thư viện nhạc nền (xem mục "Thư viện nhạc nền" dưới) | chỉ audio |
| `image` | `data_b64`, `ext`, `duration` (giây, xem dưới) | Ảnh tĩnh -> 1 clip video giữ nguyên ảnh (xem `reup-editor-node/README.md` cmd `image-to-video`) | chỉ video |

`duration` (item `type="image"`): bắt buộc nếu `video_items` TRỘN ảnh với video thật khác. Nếu
`video_items` TOÀN BỘ là ảnh, `duration` tuỳ chọn — bỏ trống thì item đó tự chia đều PHẦN audio
còn lại (sau khi trừ các ảnh đã tự nhập `duration`) cho các ảnh còn thiếu (vd audio 10s, 2 ảnh
không nhập `duration` -> mỗi ảnh 5s; 1 ảnh nhập `duration: 4` + 1 ảnh không nhập -> ảnh còn lại
tự lấy 6s).

Xử lý: video nhiều item khác resolution/fps/codec (khác nền tảng) LUÔN được chuẩn hoá (scale/pad
+ fps theo video ĐẦU TIÊN) trước khi nối — tránh lỗi ffmpeg `concat` khi nguồn không đồng nhất
(xem `reup-editor-node/README.md` cmd `concat-video`). Video và audio sau khi nối riêng được mux
lại bằng đúng cơ chế `-shortest` sẵn có (cmd `edit`) — **track nào ngắn hơn (tổng video hay tổng
audio) quyết định độ dài video xuất cuối**, không cắt/pad thêm gì khác. Output:
`/outputs/<pipeline_id>/mix_<tên>.mp4` (hoặc `mix_final.mp4` nếu không đặt `video_name`). Xem
`reup-orchestrator-node/scripts/pipeline_runner.py::_run_mix_pipeline`/`_resolve_mix_item`.

**Lưu ý thứ tự xử lý**: `audio_items` được resolve + nối (`concat-audio`) TRƯỚC `video_items` —
cần biết tổng độ dài audio trước để tính mặc định `duration` cho ảnh (nếu có). Không ảnh hưởng
kết quả cuối khi không có ảnh, chỉ đổi thứ tự 2 bước độc lập dữ liệu.

### `POST /pipelines/{id}/cancel` — huỷ job

```bash
curl -s -X POST http://localhost:8106/pipelines/<pipeline_id>/cancel
# -> {"ok": true, "pipeline_id": "...", "status": "cancelled" | "cancelling"}
```

Chỉ huỷ được job đang `pending` hoặc `started` (job `finished`/`failed`/đã `cancelled` bị từ
chối). 2 kết quả khác nhau tuỳ thời điểm gọi:

- **`pending`** (worker chưa chạm tới): gỡ thẳng khỏi hàng đợi Redis, huỷ **tức thì**, trả
  `status: "cancelled"`.
- **`started`** (worker đang chạy): đặt 1 cờ hợp tác trong Redis
  (`request_cancel`/`is_cancel_requested`, xem `queue_lib.py`), chỉ chặn `pipeline_runner.py`
  không cho tiến sang bước KẾ TIẾP (`_run_stage()` tự kiểm tra cờ này ngay trước mỗi bước). Trả
  `status: "cancelling"` — job vẫn `started` tới khi bước hiện tại dừng, sau đó tự chuyển
  `cancelled`. Trường hợp gọi cancel đúng lúc `pop_job()` (BLMOVE) vừa nhấc job ra khỏi `pending`
  (race hiếm) cũng rơi vào nhánh này, không có gì đặc biệt phải xử lý riêng.
  - **Riêng bước `ytdlp`**: cờ hợp tác không đủ (chờ HTTP response mới kiểm), nên
    `cancel_pipeline()` gọi kèm `POST {ytdlp}/pipelines/{id}/cancel` để **kill thật** tiến trình
    yt-dlp đang tải ngay lập tức (xem `reup-ytdlp-node/README.md` mục "Huỷ job đang tải"), không
    phải chờ tải xong mới dừng. 4 node con còn lại (`transcribe`/`translate`/`tts-gpu`/`editor`)
    vẫn chỉ dừng được ở cờ hợp tác — xem lý do bên dưới.

Job `cancelled` dùng chung `POST /pipelines/{id}/retry` với `failed` (cùng cơ chế
`partial_stages` — chạy lại bỏ qua bước đã xong, không tải/xử lý lại từ đầu).

**Vì sao 4/5 node con còn lại chưa kill cứng được bước đang chạy**: `transcribe`/`translate`/
`tts-gpu`/`editor` đều xử lý job đơn luồng, đồng bộ (gọi hàm Python trực tiếp hoặc subprocess
ffmpeg, xem `worker.py` từng node) — không có cơ chế nhận tín hiệu huỷ giữa chừng. `ytdlp` là
node đầu tiên được thêm khả năng này (subprocess CLI đơn giản, dễ `Popen`+poll+kill nhất, và là
bước tốn thời gian/băng thông nhất khi phải chờ vô ích); mở rộng sang 4 node còn lại (đổi
`subprocess.run`/gọi hàm chặn cứng sang polling tương tự) để lại làm sau khi cần, theo đúng
pattern đã dùng ở `ytdlp` — cancel ở 4 node đó vẫn là **best-effort ở tầng orchestrator**, không
phải kill tiến trình thật.

### `POST /pipelines/{id}/trim` — cắt đầu/đuôi mp3/video

```bash
curl -s -X POST http://localhost:8106/pipelines/<pipeline_id>/trim \
  -H 'Content-Type: application/json' -d '{"start": "00:00:05", "end": "00:01:30"}'
# -> {"ok": true, "output": "/outputs/<id>/audio_<tên>_trimmed.mp3", "duration_s": 85.0}
```

`start`/`end` (giây hoặc `"HH:MM:SS"`) — truyền 1 trong 2 để chỉ cắt đầu HOẶC chỉ cắt đuôi,
truyền cả 2 để cắt cả 2 đầu. Chỉ dùng được khi job đã `finished`. Định theo **đuôi file output**,
không theo `mode`: mp4/webm/mkv (review/dialogue/subtitle/video/mix) đều cắt được; mp3 GIỮ
NGUYÊN gate cũ — chỉ `mode="audio"` (mp3 của mode khác, vd `book`, cố tình KHÔNG cho cắt ở đây,
ngoài phạm vi). Chạy ffmpeg **`-c copy` (stream copy, không re-encode)** trực tiếp trong container
`api` — không qua job queue của `worker`, tức thời, 0 tốn CPU thêm/không ảnh hưởng service khác.
mp3 cắt chính xác tuyệt đối tại mọi mốc thời gian (không có keyframe/GOP); **video (mp4/webm/mkv)
chỉ cắt được tại keyframe gần nhất** — điểm cắt thực tế có thể lệch vài giây so với `start`/`end`
yêu cầu tuỳ khoảng cách keyframe của video gốc, KHÔNG frame-chính-xác tuyệt đối (đánh đổi đã chốt:
chấp nhận lệch để đổi lấy tốc độ/an toàn — cắt frame-chính-xác cần re-encode, tốn CPU đáng kể,
xem `trim_lib.py`). Ghi file `<tên>_trimmed<đuôi gốc>` cạnh file gốc (không đụng/xoá file gốc) —
gọi lại nhiều lần (thử start/end khác) chỉ ghi đè đúng file `_trimmed` này, xem lại được cả file
gốc lẫn bản đã cắt bất kỳ lúc nào qua `GET /pipelines/{id}` (field `trim_output`).

### `GET /pipelines/{id}` — poll trạng thái

```bash
curl -s http://localhost:8106/pipelines/<pipeline_id>
# -> {"ok": true, "status": "pending|started|finished|failed|cancelled",
#     "result": {"output": "/outputs/<id>/final.mp4", "stages": {...}} | null}
```

`result.stages` ghi lại từng bước (`ytdlp`, `transcribe`, `translate`, `tts`, `editor_srt`,
`editor_mix_dialogue` (chỉ nhánh dialogue), `editor_edit`) kèm `status`/`result`/`elapsed_s`
hoặc `error` — dùng để hiển thị tiến độ trên `reup-ui`.

### `GET /pipelines?limit=50` — danh sách job gần đây (tối đa 200, dùng cho `reup-ui` Monitor)

### `GET /health` — thêm field `total` (tổng số pipeline từng tạo, đếm riêng không giới hạn 200)

### `GET /voices` — proxy `list-voices` job sang `reup-tts-gpu-node`, trả `{voices: [{label, id}], count}`

### `GET /voices/{id}/sample` — nghe thử giọng

Stream trực tiếp file wav ~15-20s (tạo sẵn offline bằng `reup-tts-gpu-node/scripts/
generate_voice_samples.py`, không qua job queue — cùng pattern `GET /music/.../raw` bên dưới)
từ mount read-only `VOICE_SAMPLES_DIR=/nodes/tts/voice_samples`. `reup-ui` gọi ngay khi người
dùng chọn giọng trong form submit (`<audio>` preview cạnh dropdown Voice). Trả
`{"ok": false, "error": ...}` nếu voice đó chưa có sample sẵn (chưa chạy script, hoặc voice mới
thêm vào `voices_v3_turbo.json`).

### `GET /styles` — danh sách style cố định (`tu_nhien`/`tin_tuc`/`doc_truyen`), dùng cho UI

### `GET /nodes/status` — đèn xanh/đỏ 6 node (đọc `/health` từng node, timeout 3s/node để 1 node
down không treo cả request) — dùng cho `reup-ui` Monitor dashboard

### `GET /outputs/<pipeline_id>/final.mp4` — tải/phát trực tiếp video final (static file)

### Thư viện nhạc nền (proxy sang `reup-editor-node`)

- `GET /music` — **legacy**, danh sách preset phẳng (đọc thẳng filesystem mount read-only
  `MUSIC_DIR=/nodes/editor/music`, không round-trip HTTP) — giữ để không vỡ client cũ.
- `GET /music/projects`, `GET /music/projects/{slug}/tracks`, `POST /music/projects`,
  `POST /music/projects/{slug}/tracks`, `DELETE /music/projects/{slug}/tracks/{track}`,
  `DELETE /music/projects/{slug}` — proxy đồng bộ 1:1 sang cùng path (method tương ứng) của
  `reup-editor-node` (1 nguồn logic duy nhất `music_library.py`, tránh drift giữa 2 nơi liệt kê
  project/track).
- `GET /music/projects/{slug}/tracks/{track}/raw` — stream trực tiếp bytes từ mount read-only
  (không qua editor-node, tránh double-hop nhị phân) — dùng để nghe thử track trên `reup-ui`.
- `GET /music/default`, `POST /music/default`, `DELETE /music/default` — proxy đồng bộ 1:1 sang
  `reup-editor-node` — track nhạc nền mặc định cho nhánh review (xem README node đó), `reup-ui`
  SubmitJob gọi `GET` lúc load form để tự chọn sẵn, `#/music` gọi `POST`/`DELETE` khi bấm tick.

## Structured log 2 ngày

`worker` ghi JSONL vào `./data/logs/YYYY-MM-DD.jsonl` (mount `/logs`): `job_start`/`job_done`/
`job_failed` (pipeline-level), và **1 dòng `pipeline_done`/`pipeline_failed` tổng kết** (kèm
nguyên `stages` — giống hệt `result.stages` trả qua API) ngay sau khi pipeline chạy xong hoặc
lỗi. Đây là "correlation ID gốc" — `pipeline_id`/`video_name` được orchestrator gắn vào **mọi**
request gửi 5 node con, nên grep đúng `pipeline_id` này xuyên cả 6 file log (`reup-*-node/data/logs/*.jsonl`)
là dựng lại được toàn bộ chuỗi xử lý 1 video (dịch câu nào, TTS đoạn nào tràn slot, residual_risk
đoạn nào cao...). Giữ 2 ngày, tự xoá qua thread nền. Tắt bằng env `EVENT_LOG_ENABLED=false`
(mặc định `true`).

## Volume

`./data/outputs` (chỉ chứa `<pipeline_id>/<mode>_<tên>.mp4` + `.srt` copy về, không phải nơi lưu
trữ chính — file trung gian nằm ở data dir gốc của từng node), `./data/logs` (structured log,
xem mục trên) + 9 mount read-write sang `data/source`/`data/outputs`/`downloads` của 5 node kia
— xem chi tiết trong `docker-compose.yml`. Chỉ `worker` cần các mount sang node khác; `api` chỉ
mount `./data/outputs` (read-only) để phục vụ static file, cộng thêm 2 mount riêng:
`../reup-editor-node/data/music:/nodes/editor/music:ro` (cho `GET /music` legacy + `GET /music/
projects/{slug}/tracks/{track}/raw`) và `../reup-tts-gpu-node/data/voice_samples:/nodes/tts/
voice_samples:ro` (cho `GET /voices/{id}/sample`) — cả hai đọc trực tiếp filesystem, không qua
job queue.

## Troubleshooting

Xem `reup-translate-node/README.md` (pattern api/worker/queue chung). Riêng node này: nếu 1
stage báo lỗi, xem `result.stages.<stage>.error` (đã kèm tên node + job_id gốc) rồi debug trực
tiếp ở node đó (`docker compose logs worker` của node tương ứng) — orchestrator không che giấu
lỗi gốc.
