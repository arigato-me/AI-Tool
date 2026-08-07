# reup-editor-node

Bản kiến trúc job-queue (api + worker, dùng chung `reup-broker`) — mux video+audio+sub, sinh
`.srt`, và nhánh video thoại `mix-dialogue`. (Bản one-shot `docker_ffmpeg-edit` trước đây đã
archive vào `legacy/`, không còn deploy.)

Cả 3 subcommand cũ (`edit`/`srt`/`mix-dialogue`) dùng chung 1 API (`POST /jobs`, phân biệt qua
field `cmd`) — logic ffmpeg/mux/tách dòng sub/mix-crossfade giữ nguyên 100%, chỉ tách phần
`main()`/argparse ra thành hàm Python thuần (`run_edit()`/`run_srt()`/`run_mix_dialogue()`) để
worker gọi trực tiếp.

## Build & chạy

```bash
cd /u01/reup_tool/docker_build/reup-broker && docker compose up -d   # nếu chưa up

cd /u01/reup_tool/docker_build/reup-editor-node
docker compose build
docker compose up -d
```

## API

### `POST /jobs` — body `{"cmd": "edit"|"srt"|"mix-dialogue"|"mix-music"|"concat-video"|"concat-audio"|"image-to-video", "params": {...}}`

**`edit`** — params: `video`, `audio`, `output` (bắt buộc), `subtitles`, `subtitle_mode`
(`none`/`soft`/`burn`), `crf`, `audio_bitrate` (tùy chọn):

```bash
curl -s -X POST http://localhost:8103/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "edit",
  "params": {"video": "/source/video.mp4", "audio": "/source/track.wav",
             "output": "/outputs/final.mp4", "subtitles": "/outputs/final.srt",
             "subtitle_mode": "burn"}
}'
```

**`srt`** — params: `input_path`, `output_path` (bắt buộc), `field`, `max_line_chars` (tùy chọn):

```bash
curl -s -X POST http://localhost:8103/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "srt",
  "params": {"input_path": "/source/translated.json", "output_path": "/outputs/final.srt"}
}'
```

**`mix-dialogue`** — params: `original`, `instrumental`, `tts`, `transcript`, `output` (bắt
buộc), `config`, `original_mix_level`, `crossfade_ms`, `speech_only` (tùy chọn):

```bash
curl -s -X POST http://localhost:8103/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "mix-dialogue",
  "params": {"original": "/source/original.wav", "instrumental": "/source/instrumental.wav",
             "tts": "/source/track.wav", "transcript": "/source/translated.json",
             "output": "/outputs/final_track.wav"}
}'
```

**`mix-music`** — params: `tts`, `music`, `output` (bắt buộc), `music_level` (tuỳ chọn, mặc định
`0.15`). Chỉ dùng cho nhánh **review** (dialogue đã có `instrumental` tách từ audio gốc, không
cần thêm nhạc ngoài) — trộn nhạc nền ở mức cố định xuyên suốt (ffmpeg `amix`, không duck theo
speech), lặp lại (`-stream_loop -1`) nếu nhạc ngắn hơn track TTS, cắt đúng theo độ dài TTS:

```bash
curl -s -X POST http://localhost:8103/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "mix-music",
  "params": {"tts": "/source/track.wav", "music": "/music/chill/lofi-1.mp3",
             "output": "/outputs/music_track.wav", "music_level": 0.15}
}'
```

**`concat-video`** — params: `inputs` (list đường dẫn, bắt buộc, ≥1), `output` (bắt buộc), `fps`,
`crf` (tuỳ chọn). Nối N video nối tiếp — LUÔN chuẩn hoá (scale/pad + fps) theo kích thước/fps
video ĐẦU TIÊN trước khi `concat` (tránh lỗi khi nguồn khác resolution/fps/codec, vd 1 video dọc
9:16 nối 1 video ngang 16:9). Dùng cho `mode="mix"` (xem `reup-orchestrator-node/README.md` mục
"Nhánh mix") — không map audio (bước mux cuối đọc audio từ `concat-audio` riêng):

```bash
curl -s -X POST http://localhost:8103/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "concat-video",
  "params": {"inputs": ["/source/v1.mp4", "/source/v2.mp4"], "output": "/outputs/concat_video.mp4"}
}'
```

**`concat-audio`** — params: `inputs` (list đường dẫn, bắt buộc, ≥1), `output` (bắt buộc),
`sample_rate` (tùy chọn, mặc định 44100). Nối N audio nối tiếp — luôn lấy stream `a:0` của mỗi
input nên nhận thẳng cả file video (không cần tách `-vn` riêng), dùng cho `mode="mix"`:

```bash
curl -s -X POST http://localhost:8103/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "concat-audio",
  "params": {"inputs": ["/source/a1.mp3", "/source/a2.wav"], "output": "/outputs/concat_audio.wav"}
}'
```

**`image-to-video`** — params: `image`, `output`, `duration` (giây, bắt buộc), `fps` (mặc định
30), `crf` (tuỳ chọn). Chuyển 1 ảnh tĩnh thành 1 clip video giữ nguyên ảnh trong `duration` giây
(ffmpeg `-loop 1 -t <duration>`) — kích thước clip lấy theo kích thước ảnh gốc (làm tròn xuống số
chẵn, libx264 yêu cầu), `concat-video` sau đó tự chuẩn hoá lại theo item đầu tiên trong nhóm như
mọi input khác. Dùng cho `mode="mix"`, `video_items` type="image" (xem
`reup-orchestrator-node/README.md` mục "Nhánh mix"):

```bash
curl -s -X POST http://localhost:8103/jobs -H 'Content-Type: application/json' -d '{
  "cmd": "image-to-video",
  "params": {"image": "/source/cover.jpg", "output": "/outputs/cover.mp4", "duration": 5.0}
}'
```

Cả 7 subcommand nhận thêm field tuỳ chọn `pipeline_id`/`video_name` (correlation ID cho
structured log — xem mục dưới; `reup-orchestrator-node` tự gắn, không bắt buộc khi gọi tay).

### `GET /jobs/{id}`, `GET /health`

## Thư viện nhạc nền

Node này là **chủ ghi duy nhất** của `data/music` (mount `./data/music:/music` rw ở `api`, ro ở
`worker` — worker chỉ đọc file nhạc để mix, không bao giờ tự ghi). Quản lý qua REST đồng bộ
(`scripts/music_library.py`, thuần filesystem + path-safe, **không qua Redis job queue** — thao
tác nhẹ, ghi vài MB, không cần worker xử lý nền như `edit`/`mix-*`):

- `GET /music/projects` — danh sách project (`slug`, `display_name`, `track_count`). File nhạc
  rời có sẵn ngay gốc `data/music` (trước khi có tính năng này) được gom vào 1 pseudo-project
  chỉ-đọc `_ungrouped`, không ép re-upload.
- `GET /music/projects/{slug}/tracks` — danh sách track trong 1 project.
- `POST /music/projects` — body `{"display_name": "..."}`, tự sinh `slug` (bỏ dấu, thường hoá,
  chống trùng bằng hậu tố `-2`/`-3`...).
- `POST /music/projects/{slug}/tracks` — body `{"filename": "...", "data_b64": "..."}`, chỉ nhận
  đuôi `.mp3`/`.wav`/`.m4a`/`.aac`/`.ogg`/`.flac`, tối đa 30MB/file.
- `DELETE /music/projects/{slug}/tracks/{track}` — xoá 1 track (dùng được cả cho track rời
  trong `_ungrouped`).
- `DELETE /music/projects/{slug}` — xoá cả project (thư mục + mọi track bên trong +
  `project.json`). Từ chối xoá `_ungrouped` (không phải thư mục thật, chỉ là file rời ở gốc
  `data/music` — muốn dọn thì xoá từng track qua endpoint trên).

**Track mặc định cho nhánh review** — con trỏ đơn toàn thư viện (1 track duy nhất, KHÔNG phải
mỗi project 1 default), lưu ở `data/music/_default.json` (`{"project": slug, "track": filename}`,
không bị `GET /music/projects` liệt kê nhầm vào `_ungrouped` vì không phải đuôi nhạc). `reup-ui`
SubmitJob dùng để tự chọn sẵn đúng track này thay vì heuristic "track đầu tiên trong project đầu
tiên" cũ khi chưa ai tick default:

- `GET /music/default` — `{"ok": true, "default": {"project", "track"} | null}`. Tự "quên" nếu
  track trỏ tới đã bị xoá (resolve lại qua `resolve_track_path` mỗi lần đọc, không tin thẳng con
  trỏ cũ) — không cần dọn tay lúc `DELETE /music/projects/{slug}/tracks/{track}`.
- `POST /music/default` — body `{"project": "...", "track": "..."}`, ghi đè default cũ (nếu có).
  Raise nếu track không tồn tại.
- `DELETE /music/default` — bỏ default (quay về heuristic "track đầu tiên" ở `reup-ui`).

`reup-orchestrator-node` proxy lại toàn bộ REST này (xem README riêng) để `reup-ui` (`#/music`)
quản lý được mà không cần biết địa chỉ node này trực tiếp.

## Structured log 2 ngày

`worker` ghi JSONL vào `./data/logs/YYYY-MM-DD.jsonl` (mount `/logs`): `job_start`/`job_done`/
`job_failed` mỗi job (kèm `cmd`), và riêng `mix-dialogue` ghi thêm **1 dòng
`mix_dialogue_config`** (giá trị `original_mix_level`/`crossfade_ms`/`speech_only_replace` đã
dùng cho lần chạy đó) + **1 dòng `mix_window` cho mỗi cửa sổ speech** đã áp mix (`segment_id`,
`start`, `end`) — dùng để tra đoạn nào đã trộn nền khi debug lỗi noise/echo theo mốc thời gian
video. Giữ 2 ngày, tự xoá qua thread nền. Tắt bằng env `EVENT_LOG_ENABLED=false` (mặc định
`true`).

## Chạy CLI tay (debug, không qua queue)

```bash
docker compose run --rm api edit -v /source/video.mp4 -a /source/track.wav -o /outputs/final.mp4
docker compose run --rm api srt -i /source/translated.json -o /outputs/final.srt
docker compose run --rm api mix-dialogue --original ... --instrumental ... --tts ... --transcript ... -o ...
```

## Volume

`./data/source`, `./data/outputs`, `./dialogue.yaml` (đọc-only), `./data/logs` (structured log)
— bind-mount, giống convention gốc.

## Troubleshooting

Xem `reup-translate-node/README.md` (pattern api/worker/queue chung) + `docker_ffmpeg-edit/README.md`
gốc (lỗi ffmpeg/font/mov_text — không đổi gì ở lớp ffmpeg command).
