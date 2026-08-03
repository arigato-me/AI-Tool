# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A video-reup pipeline: download a Douyin/short-video URL, transcribe, translate (Deepseek),
TTS-dub into Vietnamese, mux back onto the source video. Built as a set of standalone,
independently-deployable Docker services ("scaffolds") wired together through a Redis job
queue, not a single application. Each `reup-*-node/` directory is one pipeline stage with its
own `api` (FastAPI, no GPU needed) + `worker` (does the real work) pair, both built from the
same image via `docker-compose.yml`. `reup-broker/` runs the shared Redis. `reup-orchestrator-node`
sequences the 5 stage-nodes into full pipeline runs; `reup-ui` (React/Vite SPA, nginx) is the
operator UI. See "Reup pipeline" below for the full flow and how each node talks to the others.

The application source for yt-dlp and VieNeu-TTS (the two vendored third-party tools) lives
under `repo_github/`, cloned (not git submodule) and patched in place.

An earlier iteration of this project (7 standalone one-shot `docker_*/` service folders +
`docker_n8n` for orchestration via Execute Command nodes) has been superseded by the
`reup-*-node` job-queue architecture and deleted from the working tree — see
`legacy/*.tar.gz` if you need to recover something from it. Nothing in `reup-*-node`/`reup-ui`
imports code from that era; it was independent scaffolding from the start.

```
docker_build/
├── reup-broker/            # shared Redis (queue + job state) every other node connects to
├── reup-ytdlp-node/        # download stage — job-queue port of the old yt-dlp scaffold, builds from repo_github/yt-dlp
├── reup-transcribe-node/   # transcribe stage — VAD+Paraformer/whisper (or Deepgram), GPU
├── reup-translate-node/    # translate stage — Deepseek API
├── reup-tts-gpu-node/      # TTS stage — VieNeu-TTS on GPU, builds from repo_github/VieNeu-TTS
├── reup-editor-node/       # final mux/edit stage — ffmpeg (edit/srt/mix-dialogue/mix-music)
├── reup-orchestrator-node/ # sequences the 5 nodes above into full pipeline runs (POST /pipelines)
├── reup-ui/                # operator web UI (React/Vite + nginx), talks to the orchestrator
├── repo_github/
│   ├── yt-dlp/              # vendored yt-dlp source, patched for Douyin support
│   └── VieNeu-TTS/          # vendored VieNeu-TTS source (has its own .git — pull separately; shared with reup-tts-gpu-node)
├── legacy/                  # tar.gz archives of the pre-job-queue docker_* scaffolds (deleted from the tree)
└── images/                  # exported `docker save` tarballs + save-images.sh / load-images.sh
```

Documentation (READMEs) throughout this repo is written in Vietnamese; keep that convention when editing them.

## Critical convention: build context

Every `reup-*-node` service (and `reup-ui`) sets `build.context: ..` in its `docker-compose.yml`
— the build context is the **parent** `docker_build/` directory, not the service's own folder.
This is required because the Dockerfiles `COPY` from `repo_github/<project>` (for
`reup-ytdlp-node`/`reup-tts-gpu-node`) as well as from their own `reup-<service>-node/` folder.
Always run `docker compose` commands from inside the specific `reup-<service>-node/` directory
(not from the repo root) so the `context: ..` and relative `dockerfile:` path resolve correctly.

## Local vs remote execution

This checkout is edited from a machine with no GPU and no Docker runtime intended for real use
— treat it as the source-of-truth for code changes only. Every `docker compose build`/`up`/`run`
invocation, and anything that needs a GPU (`reup-transcribe-node`, `reup-tts-gpu-node`), must be
executed on the remote host instead: `ssh hmtran@100.99.150.90` (checkout there lives at
`/home/hmtran/Projects/docker_build` — a different absolute path than this checkout, that's
expected). Sync the edited files to that host first (rsync individual changed files — the two
checkouts are not a shared mount), then run the `docker compose`/CLI commands there, not on this
machine.

## Common commands

Each node is a persistent service (`api` + `worker`, both `restart: unless-stopped`) — unlike
the old one-shot scaffolds, there's no `docker compose run --rm` step; work is submitted as a
job over HTTP and polled:

```bash
cd reup-broker && docker compose up -d          # shared Redis — bring up first, once
cd ../reup-<service>-node
docker compose build
docker compose up -d
curl -s -X POST http://localhost:<port>/jobs -H 'Content-Type: application/json' -d '{...}'
curl -s http://localhost:<port>/jobs/<job_id>   # poll status
docker compose logs -f worker
docker compose down
```

Ports: `reup-translate-node` 8101, `reup-ytdlp-node` 8102, `reup-editor-node` 8103,
`reup-transcribe-node` 8104, `reup-tts-gpu-node` 8105, `reup-orchestrator-node` 8106,
`reup-ui` 8107 (`reup-broker`'s Redis has no host-exposed port, only reachable on the internal
`reup-net` network).

Every node's own README documents its exact `POST /jobs` body shape and any node-specific
behavior — read that first rather than assuming a shared contract beyond `POST /jobs` +
`GET /jobs/{id}` + `GET /health`.

Health/monitoring for the whole pipeline is `reup-ui`'s `#/monitor` page (worker up/down,
pending/processing counts, recent job table) — there's no separate `healthcheck.sh` script.

Image export/import (from repo root, operates on already-built Docker images by tag):

```bash
./images/save-images.sh
./images/load-images.sh
```

## Reup pipeline

**`reup-orchestrator-node` + `reup-ui`.** Bộ 6 node job-queue (`reup-broker` +
`reup-ytdlp-node`/`reup-transcribe-node`/`reup-translate-node`/`reup-tts-gpu-node`/
`reup-editor-node`, mỗi node gồm `api`+`worker` dùng chung Redis, xem README riêng từng node)
là 5 bước pipeline nối tiếp qua job-queue (`POST /jobs`/`GET /jobs/{id}` mỗi node).
`reup-orchestrator-node` (`POST /pipelines`/`GET /pipelines/{id}`) tự động hoá đúng 2 nhánh
review/dialogue mô tả bên dưới bằng cách gọi HTTP API các node theo thứ tự, tự copy file giữa
`data/source`/`data/outputs` riêng của từng node (mount trực tiếp, xem
`reup-orchestrator-node/README.md`). `reup-ui` (React/Vite SPA, `nginx` reverse-proxy `/api/*`)
là giao diện vận hành.

**Các tính năng bổ sung trên nền orchestrator+UI** (thêm sau khi 2 node trên đã hoàn thiện,
không đổi kiến trúc gốc):
- **`video_name`** (field tuỳ chọn trong `POST /pipelines`, ô "Tên video" trên `reup-ui`): làm
  tên chuẩn xuyên suốt mọi file trung gian của 1 lần chạy; file xuất cuối luôn có tag nhánh
  (`review_<tên>.mp4`/`dialogue_<tên>.mp4`, hoặc `review_final.mp4`/`dialogue_final.mp4` nếu
  không đặt tên).
- **Clone giọng** (`reup-tts-gpu-node`, field `ref_audio`/`ref_audio_b64`): upload 1 file WAV
  mẫu 3-5 giây thay vì chọn preset voice — dùng thẳng khả năng voice cloning có sẵn của engine
  v3turbo (`infer(text, ref_audio=...)`), không cần enroll giọng trước.
- **Nghe thử giọng** (`reup-ui` form submit, `<audio>` cạnh dropdown Voice; orchestrator
  `GET /voices/{id}/sample`): phát sample wav 15-20s tạo sẵn offline bằng `reup-tts-gpu-node/
  scripts/generate_voice_samples.py` (`docker compose run --rm worker samples`, cần GPU, chạy
  thủ công 1 lần hoặc lại mỗi khi thêm/đổi preset) — mount read-only sang orchestrator giống
  pattern nhạc nền bên dưới, không qua job queue.
- **Import CSV hàng loạt** (`reup-ui` trang `#/import`): nạp file CSV (`STT,video_name,link`),
  áp 1 bộ cài đặt chung (nhánh/voice/style/subtitle) cho cả danh sách, submit tuần tự qua đúng
  `POST /pipelines` — hàng đợi Redis sẵn có của `reup-orchestrator-node` tự xử lý lần lượt,
  không cần đợi xong video này mới tạo video kia.
- **Monitor dashboard** (`reup-ui` trang `#/monitor`, `GET /nodes/status` + `GET /health` +
  `GET /voices`/`GET /styles` trên orchestrator): đèn xanh/đỏ 6 worker, pending/processing/tổng
  job, số video xử lý xong hôm nay/tuần/tháng, bảng job phân trang (tối đa 200 gần nhất).
- **Structured log 2 ngày** (mọi `reup-*-node`, JSONL tại `data/logs/YYYY-MM-DD.jsonl`, mount
  `/logs`, toggle `EVENT_LOG_ENABLED`): mỗi request orchestrator gửi 5 node con đều kèm
  `pipeline_id`/`video_name` — cho phép `grep` xuyên log cả 6 node theo đúng 1 pipeline để tra
  lỗi chất lượng theo mốc thời gian video (dịch sai câu nào, TTS đoạn nào tràn slot,
  `residual_risk`/mix window đoạn nào — xem README từng node mục "Structured log 2 ngày").
  `JOB_TTL_S` (Redis job hash) = 48h để khớp cửa sổ 2 ngày này — job cũ hơn 48h tự mất khỏi
  `GET /pipelines` (hash Redis hết hạn), không phải bug.
- **Thư viện nhạc nền** (`reup-editor-node` `scripts/music_library.py` + cmd `mix-music`, trang
  `#/music` trên `reup-ui`): quản lý nhạc nền theo project/theme (tạo project, upload track qua
  REST đồng bộ, không qua job queue) thay vì chỉ 1 thư mục preset phẳng. Chỉ áp dụng nhánh
  **review** (dialogue đã có `instrumental` tách từ audio gốc, không cần thêm nhạc ngoài) — trộn
  ở mức cố định xuyên suốt (ffmpeg `amix`, không duck theo speech). `POST /pipelines` nhận 3 cách
  chọn nhạc, ưu tiên theo thứ tự: `music_b64` (upload tức thời) > `music_project`+`music_track`
  (chọn từ thư viện) > `music_preset` (tên file phẳng, cách cũ) — xem
  `reup-orchestrator-node/README.md` mục "Thư viện nhạc nền".

**Lưu ý phần cứng đã gặp thật**: `reup-tts-gpu-node-worker` giữ model resident vĩnh viễn (đúng
thiết kế), nên trên GPU nhỏ (vd 4GB, dùng chung với tenant khác) `reup-transcribe-node` có thể
CUDA OOM lúc load model dù code không có lỗi gì — xử lý bằng cách giải phóng VRAM từ các
container/service không cần thiết khác trên host trước khi chạy batch nặng, không phải bug ở
node.

5 node nối tiếp, output của node trước là input của node sau (mỗi service chỉ thấy
`data/source`/`data/outputs` của chính nó — `reup-orchestrator-node` tự copy file qua giữa các
bước). Có 2 nhánh, chọn bằng `mode` khi gọi `reup-transcribe-node` (`"review"` mặc định,
`"dialogue"` là bổ sung — không đổi gì hành vi nhánh review):

**Nhánh review** (mặc định, `mode=review` — 1 người nói, mute toàn bộ audio gốc):

```
[URL]
  │ reup-ytdlp-node        tải video (patch Douyin)
  ▼
source.mp4
  │ reup-transcribe-node   ffmpeg trích audio -> VAD+Paraformer/whisper (GPU) hoặc Deepgram
  ▼
transcript.json
  │ reup-translate-node    dịch từng segment -> Deepseek
  ▼
translated.json ─────────────────────────────┐
  │ reup-tts-gpu-node segments                │ reup-editor-node srt
  ▼ TTS ghép theo timestamp (silence-padded)  ▼
track.wav ────────────────────────────► final.srt
              reup-editor-node edit    │
     (source.mp4 hình + track.wav + final.srt)
                     ▼
                 final.mp4
```

| Node | Chức năng | Input | Output |
|---|---|---|---|
| `reup-ytdlp-node` | Tải video từ URL (patch Douyin: abogus/cookie tự sinh) | URL | `source.mp4` (video+audio gốc) |
| `reup-transcribe-node` | Trích audio (ffmpeg) → VAD + route Paraformer(zh)/faster-whisper(khác) + punc/align (GPU) hoặc Deepgram fallback → transcript có timestamp | `source.mp4`/audio | `transcript.json` `{language, duration_s, segments:[{id,start,end,text}]}` |
| `reup-translate-node` | Dịch từng segment qua Deepseek (batch 20/lần), giữ nguyên `start`/`end` | `transcript.json` | `translated.json` — mỗi segment thêm `text_original` (gốc), `text` = bản dịch |
| `reup-tts-gpu-node` | 2 subcommand: `tts` (1 file text → 1 wav) hoặc `segments` (transcript JSON → 1 wav ghép theo timestamp, silence-padded) | `translated.json` (hoặc file text thô cho `tts`) | `track.wav` |
| `reup-editor-node` | `srt` (JSON → `.srt`) và `edit` (mux video hình gốc + audio mới, sub `none`/`soft`/`burn`, luôn drop audio gốc) | `source.mp4` (chỉ hình) + `track.wav` + `translated.json`/`final.srt` | `final.srt`, `final.mp4` |

**Nhánh video thoại** (`mode=dialogue` ở `reup-transcribe-node` — giữ nền âm thanh gốc, chỉ mute đúng tiếng Trung, hợp với video nhiều tiếng môi trường/nhân vật thoại; chỉ hỗ trợ `TRANSCRIBE_BACKEND=local`):

```
source.mp4
  │ reup-transcribe-node (mode=dialogue)
  │   ffmpeg trích audio -> separate (MDX-Net Inst HQ 3 + htdemucs_ft ensemble)
  │   -> vocals.wav (VAD+STT) + instrumental.wav (chọn theo residual_risk) + original.wav
  ▼
transcript.json (segments có thêm residual_risk) + instrumental.wav + original.wav
  │ reup-translate-node    (không đổi)
  ▼
translated.json
  │ reup-tts-gpu-node segments   (không đổi)
  ▼
track.wav
  │ reup-editor-node mix-dialogue
  │   instrumental.wav + original.wav + track.wav, theo timestamp translated.json
  │   (đoạn có thoại: instrumental + original*15% + TTS, crossfade ~100ms; đoạn còn lại: original 100%)
  ▼
final_track.wav
  │ reup-editor-node edit   (KHÔNG đổi — chỉ đổi -a sang final_track.wav)
  ▼
final.mp4
```

**Quy ước tag image theo version**: `:local` bị ghi đè mỗi lần `docker compose build`, không có
đường lùi. Trước khi build đè 1 image dự định giữ lại lâu dài, tag thêm bản cụ thể trước:
`docker tag <svc>:local <svc>:vX.Y.Z` (khi image có version nội tại rõ ràng, vd
`reup-tts-gpu-node` theo version VieNeu-TTS) hoặc `docker tag <svc>:local <svc>:YYYY-MM-DD` (khi
không có).

## Updating vendored sources

`repo_github/yt-dlp` and `repo_github/VieNeu-TTS` are independent git checkouts of their
upstream repos (VieNeu-TTS still has its own `.git`). To pick up upstream changes, `git pull`
inside the specific `repo_github/<project>` directory, then rebuild the corresponding
`reup-*-node` image(s) — `repo_github/VieNeu-TTS` is used only by `reup-tts-gpu-node`.
