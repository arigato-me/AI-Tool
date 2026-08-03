# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A collection of standalone Docker service definitions ("scaffolds"), not a single application. Each top-level `docker_*/` directory wraps one third-party tool into its own `docker-compose.yml` + `Dockerfile`. The application source for the two VieNeu-TTS variants and yt-dlp is vendored (via clone, not git submodule) under `repo_github/`. `docker_n8n` builds a thin custom image (`n8n-orchestrator:local`) on top of upstream n8n, adding a static Docker CLI + Compose plugin so it can drive the other services itself (Docker-out-of-Docker) — see its own "Cơ chế orchestrate" section in `docker_n8n/README.md`. `docker_translate`, `docker_ffmpeg-edit` are lightweight one-shot CLI containers (no vendored source) that together with yt-dlp/vieneu-tts form a full video-reup pipeline — see "Reup pipeline" below. `docker_transcribe` used to be one-shot too, but is now the **only persistent-service node** in the repo (FastAPI serve mode, GPU pipeline) — see its own specifics section for why and how it's called differently.

```
docker_build/
├── docker_n8n/           # n8n workflow automation — custom image: upstream n8n + docker CLI/compose (DooD orchestrator)
├── docker_yt-dlp/        # yt-dlp downloader, builds from repo_github/yt-dlp
├── docker_vieneu-tts/    # VieNeu-TTS Vietnamese TTS (CPU/ONNX), builds from repo_github/VieNeu-TTS
├── docker_vieneu-tts_gpu/# VieNeu-TTS on GPU/CUDA (PyTorch, voice cloning), builds from repo_github/VieNeu-TTS
├── docker_transcribe/    # video/audio -> timestamped transcript JSON; self-hosted GPU pipeline, serve mode (not one-shot)
├── docker_translate/     # transcript JSON -> translated transcript JSON, via Deepseek API
├── docker_ffmpeg-edit/   # mux/burn new audio + subtitles onto source video, local ffmpeg only
├── repo_github/
│   ├── yt-dlp/            # vendored yt-dlp source, patched for Douyin support
│   └── VieNeu-TTS/        # vendored VieNeu-TTS source (has its own .git — pull separately; shared by CPU + GPU variants)
└── images/                # exported `docker save` tarballs + save-images.sh / load-images.sh
```

Documentation (READMEs) throughout this repo is written in Vietnamese; keep that convention when editing them.

## Critical convention: build context

Every custom-built service (`docker_yt-dlp`, `docker_vieneu-tts`, `docker_vieneu-tts_gpu`, `docker_transcribe`, `docker_translate`, `docker_ffmpeg-edit`) sets `build.context: ..` in its `docker-compose.yml` — the build context is the **parent** `docker_build/` directory, not the service's own folder. This is required because the Dockerfiles `COPY` from `repo_github/<project>` as well as from their own `docker_<service>/` folder. Always run `docker compose` commands from inside the specific `docker_<service>/` directory (not from the repo root) so the `context: ..` and relative `dockerfile:` path resolve correctly.

## Local vs remote execution

This checkout is edited from a machine with no GPU and no Docker runtime intended for real use — treat it as the source-of-truth for code changes only. Every `docker compose build`/`up`/`run` invocation, and anything that needs a GPU (`reup-transcribe-node`, `reup-tts-gpu-node`, `docker_transcribe`, `docker_vieneu-tts_gpu`), must be executed on the remote host instead: `ssh hmtran@100.99.150.90`. Sync the edited files to that host first (however that's currently done — rsync/scp/shared mount), then run the `docker compose`/CLI commands documented below there, not on this machine.

## Common commands

Each service is operated independently, from within its own directory:

```bash
cd docker_yt-dlp/          # or docker_vieneu-tts/, docker_n8n/
docker compose build             # build image (context is parent dir)
docker compose build --no-cache  # clean rebuild
docker compose up -d             # start (n8n, vieneu-tts web UI)
docker compose run --rm <svc> ... # one-shot task (yt-dlp downloads, vieneu-tts CLI)
docker compose logs -f
docker compose down
```

Image export/import (from repo root, operates on all known images):

```bash
./images/save-images.sh    # docker save yt-dlp/vieneu-tts/vieneu-tts-gpu/n8n/transcribe/translate/ffmpeg-edit -> images/*.tar
./images/load-images.sh    # docker load each tar back in
```

Health-check nhanh, không tốn phí API, cho cả 6 service (`--version`/`--help`/`--list-voices` + `docker version` qua n8n; `transcribe` là ngoại lệ — check qua `curl .../health` vì đây là service thường trực, cần đang `up` sẵn):

```bash
./healthcheck.sh
```

### docker_yt-dlp specifics

- One-shot container: `entrypoint.sh` just `exec yt-dlp "$@"` — every invocation is `docker compose run --rm yt-dlp <yt-dlp args>`.
- Douyin support is a patch on top of stock yt-dlp living in `repo_github/yt-dlp/yt_dlp/` (extractor + `abogus.py` / `douyin_cookies.py` — auto-generates `a_bogus` and anti-bot cookies, no browser/cookies file needed for public videos).
- After editing vendored source, rebuild then sanity-check: `docker compose build && docker compose run --rm yt-dlp --version`.
- Mounts: `data/downloads` (output), `data/config` (cookies.txt, urls.txt).

### docker_vieneu-tts specifics

- Dual-mode entrypoint (`entrypoint.sh`): `web` (default) launches the Gradio UI via `uv run vieneu-web` on port 7860; `tts` runs `docker_vieneu-tts/scripts/tts_cli.py` for one-shot file-to-wav synthesis (used by n8n's Execute Command node — CLI prints a JSON result line to stdout on success).
- CPU-only / ONNX v3 Turbo stack — no voice cloning or GPU models (that's `docker_vieneu-tts_gpu`'s job, see below).
- Uses `uv` for Python dependency management (`uv sync --no-dev` at build time), not pip.
- Mounts: `data/cache` (HF model cache — persist to avoid re-downloading), `data/source` (input text), `data/outputs` (output wav).
- Voice/style defaults come from env vars `VIENEU_VOICE` / `VIENEU_STYLE`, overridable per-CLI-call via `--voice` / `--style`.

### docker_vieneu-tts_gpu specifics

- GPU/CUDA counterpart to `docker_vieneu-tts`, sharing the same vendored source (`repo_github/VieNeu-TTS`) but its own Dockerfile/dependency group and its own port (`7861` vs CPU's `7860`) so both can run side by side. Runs PyTorch on GPU (v3 Turbo, plus v1/v2 and voice cloning/denoise — unavailable on the CPU/ONNX build).
- Needs NVIDIA Container Toolkit + `deploy.reservations.devices` on the host to `up`/`run` (GPU not required to `build`).
- Triple-mode entrypoint (`web` default / `tts` / `segments`), same `--voice`/`--style` flags:
  - `tts` (`scripts/tts_cli.py`): 1 text file -> 1 wav. JSON stdout uses `synthesis_time_s` (compute wall time) + `audio_duration_s` (real audio length, `len(audio) / tts.sample_rate`) — do not confuse the two.
  - `segments` (`scripts/segments_cli.py`): transcript JSON (`{"segments":[{"start","end","text"}]}`) -> 1 silence-padded wav with each segment placed at its real timestamp — closes the gap `docker_ffmpeg-edit/README.md` used to flag as "Thiếu". Loads the model once, writes each segment into a numpy buffer at `round(start * sample_rate)`. If a segment's synthesized audio outruns its `end - start` slot, it overwrites the start of the next segment (timeline isn't shifted) — a warning is logged to stderr when this happens, but it isn't auto-corrected.
- **CPU variant still has the old bug**: `docker_vieneu-tts/scripts/tts_cli.py` reports `duration_s` = compute time (mislabeled as audio length) — same bug already fixed here, not yet ported to CPU.
- Mounts: `data/cache`, `data/source`, `data/outputs` — same layout as CPU service.

### docker_transcribe specifics

- **The only persistent-service node in the repo** — every other custom-built service is one-shot (`docker compose run --rm`); `docker_transcribe` is `docker compose up -d` and stays running (`restart: unless-stopped`). Reason: its pipeline loads 4-5 GPU models (VAD + STT + punc + optional align) — reloading all of them per invocation would dominate runtime when batch-processing many videos back to back, so the container keeps models warm between jobs instead.
- Self-hosted STT pipeline (`TRANSCRIBE_BACKEND=local`, default): `ffmpeg -vn` extract 16k mono wav -> VAD (`fsmn-vad`, FunASR) cuts silence -> lang-id (faster-whisper, ~30s of first voiced segment) -> route: `zh*` -> Paraformer-large/FunASR + `ct-punc` (Paraformer returns unpunctuated text); anything else -> faster-whisper `large-v3` (`compute_type=int8`), with optional WhisperX word-align (`align: true` in the request, whisper branch only). VAD-segment-relative timestamps are re-offset to absolute time before being written — a place that's easy to get wrong and would desync the whole downstream timeline if missed. Deepgram (`TRANSCRIBE_BACKEND=deepgram`) is kept as a fallback/accuracy baseline, unchanged from the original one-shot logic (now in `scripts/deepgram_backend.py`).
- Model VRAM lifecycle matters here because the host GPU (RTX 3050, 4GB) is shared with `docker_vieneu-tts_gpu`: VAD loads once at startup and stays resident (cheap); the heavier STT/punc/align models are lazy-loaded on first use and freed after `MODEL_IDLE_TIMEOUT_SECONDS` (default 120s) of inactivity via a background task, and `GPU_MEMORY_FRACTION` (default 0.4) caps this node's VRAM share — both tunable via env. Bring the service down (`docker compose down`) after a batch run to fully release the GPU for the TTS step.
- **Called differently from every other node**: n8n's Execute Command doesn't do `docker compose run --rm transcribe ...` here — it does `docker exec transcribe python3 /opt/scripts/transcribe_client.py -i ... -o ...` (client script POSTs to the server's own `localhost:$TRANSCRIBE_PORT`, prints the same JSON-summary-line contract every other node uses). This reuses the DooD/`docker.sock` access n8n already has instead of opening a shared Docker network between compose projects, keeping every service's compose file independent per the "standalone scaffold" design of this repo.
- `scripts/transcribe_cli.py` (the original one-shot Deepgram CLI) is untouched and still reachable via `entrypoint.sh cli ...` for manual testing without the server.
- Output JSON schema is unchanged (`language`, `duration_s`, `segments:[{id,start,end,text}]`) — downstream nodes need no changes. The only schema additions are opt-in: `words` per segment when `align: true`, `residual_risk` per segment when `mode: "dialogue"`.
- Mounts: `data/source` (input), `data/outputs` (transcript JSON), `data/cache` (FunASR/HF/faster-whisper model cache — persist to avoid re-downloading), `dialogue.yaml` (config for `mode=dialogue`, mounted read-only at `/config/dialogue.yaml`).
- **"Video thoại" branch (`mode: "dialogue"` in the `/transcribe` request body, default is `"review"` — unchanged behavior)**: added on top of the original single-narrator pipeline to preserve background sound (traffic, ambient noise, music) instead of muting it entirely. Inserts a `separate` stage (`scripts/separator.py`, package `audio-separator`) right after audio extraction, before VAD: splits audio into `vocals.wav` (fed into VAD/STT instead of the raw mixed audio — cleaner signal for sentence boundaries) and two candidate `instrumental` tracks from two ensemble models (MDX-Net Inst HQ 3 primary, `htdemucs_ft` for cross-checking, not a fail-over — both run every dialogue-mode job). The two separator models are never resident on the GPU at the same time (`model_registry.unload()` right after each run) to avoid repeating the CUDA OOM already hit once when stacking too many models on the shared 4GB card. `scripts/residual_qc.py` picks the better instrumental candidate per VAD-speech segment by re-running the already-loaded VAD model on that instrumental slice — a segment that VAD still flags as "voiced" there is scored as likely residual vocal leakage (`residual_risk`, written per-segment into the transcript JSON). Writes two extra artifacts next to the transcript output: `<output>_instrumental.wav` and `<output>_original.wav`, both consumed by `docker_ffmpeg-edit mix-dialogue` (see that service's specifics) before the final `edit` mux. **Only supported with `TRANSCRIBE_BACKEND=local`** — combining `mode=dialogue` with the Deepgram backend is rejected by the server rather than silently ignored.

### docker_translate specifics

- One-shot: translates a transcript JSON (from `docker_transcribe`) segment-by-segment via Deepseek API (`deepseek-chat`, OpenAI-compatible), preserving each segment's `start`/`end` so downstream TTS/subtitle timing stays aligned.
- API key via `.env` (`DEEPSEEK_API_KEY`).
- If the API returns a different segment count than sent, the CLI errors out rather than risk misaligned timestamps.
- Mounts: `data/source` (input transcript JSON), `data/outputs` (translated transcript JSON).

### docker_ffmpeg-edit specifics

- One-shot, local FFmpeg only (no external API) — the final pipeline step. Three subcommands: `edit` (mux video + new audio + optional subtitles), `srt` (transcript JSON -> `.srt`), and `mix-dialogue` (video-thoại branch, see below).
- `edit` always drops the original audio track entirely (`-map 0:v -map 1:a`); `--subtitle-mode` is `none` (default), `soft` (mux, fast, keeps `-c:v copy`), or `burn` (hardcoded into video, forces re-encode). **`edit_cli.py` itself is untouched by the video-thoại branch** — that branch only changes which file gets passed as `-a`.
- Now has a `pyproject.toml`/`uv sync` build step (numpy/soundfile/pyyaml) purely for `mix_dialogue_cli.py` — `edit`/`srt` remain stdlib-only subprocess wrappers around the `ffmpeg` binary, unaffected.
- **"Video thoại" branch (`mix-dialogue` subcommand)**: takes `--original`/`--instrumental` (from `docker_transcribe` `mode=dialogue`), `--tts` (from `docker_vieneu-tts_gpu segments`) and `--transcript` (`translated.json`, reused purely for its `start`/`end` timestamps as the speech-window map), and produces one `final_track.wav` meant to be fed straight into `edit -a` unchanged. Builds the output as a numpy buffer (`scripts/mix_dialogue_cli.py`) rather than a giant ffmpeg `filter_complex` — same style as `docker_vieneu-tts_gpu/scripts/segments_cli.py`'s own buffer-writing approach, which scales far better than per-segment `enable=` filter graph nodes once there are 100+ segments. Outside every speech window the original audio plays back at 100%; inside a speech window it's replaced by `instrumental*1.0 + original*original_mix_level` (default 0.15, restores some of the thickness/high end that source-separation loses) with the TTS track summed on top, and a short cosine crossfade (default 100ms) at each segment boundary to avoid an audible cut. Config lives in `dialogue.yaml` (`mix_dialogue.*`, mounted at `/config/dialogue.yaml`), overridable per-call via CLI flags.
- Mounts: `data/source` (video/audio/subtitle inputs), `data/outputs` (final video + generated `.srt`), `dialogue.yaml` (video-thoại config, read-only).

### docker_n8n specifics

**Legacy — superseded by `reup-orchestrator-node` + `reup-ui` (see "Reup pipeline" below) as the primary way to run the pipeline.** Kept fully intact as a working backup/fallback (still builds and runs independently), but new work should target the `reup-*-node` job-queue architecture instead. The weaknesses below (`NODES_EXCLUDE`, per-host gid/path coupling, no async job state) are exactly what the job-queue rewrite was designed to remove.

- Custom `Dockerfile`: upstream `docker.n8n.io/n8nio/n8n:latest` (Alpine-based Docker Hardened Image, no package manager) plus a static `docker` CLI + `docker-compose` plugin copied in from the `docker:cli` image — build with `docker compose build` before first `up`.
- **Required env var: `NODES_EXCLUDE=[]`**. n8n v2+ disables the `Execute Command` node (and `Local File Trigger`) **by default** for security (`@n8n/config` `NodesConfig.exclude` hardcodes both). Since this whole project's orchestration design depends on Execute Command nodes, without this env var every Execute Command node fails to load with `Unrecognized node type: n8n-nodes-base.executeCommand` — silently breaks orchestration on any n8n upgrade unless explicitly set. Confirmed/fixed on the reference deployment; verify present in `docker-compose.yml` environment after any n8n version bump.
- **Required env var when `N8N_PROTOCOL=http` (no TLS): `N8N_SECURE_COOKIE=false`**. Without it, n8n sets a `Secure` cookie the browser refuses to send back over plain http, so UI login silently loops back to the login page. Only affects UI access, not CLI/Execute Command.
- Orchestrates sibling services via Docker-out-of-Docker: `/var/run/docker.sock` is bind-mounted in, and `group_add` adds the host's `docker` group gid (check with `getent group docker`, currently `986` on this checkout) so the container's non-root `node` user can use the socket without running as root (n8n's entrypoint disallows root). **Gid + bind-mount path are per-host** — a deployment on another machine needs both values updated to match that host (confirmed: a remote deployment uses a different gid and a different absolute path than this checkout, correctly diverged from what's committed here).
- The whole `docker_build/` directory is bind-mounted into the container **at the same absolute host path** (`/u01/reup_tool/docker_build`). This is required because Execute Command nodes call the host Docker daemon, which resolves each sibling service's relative `./data/...` bind-mounts against the working directory path — that path must match the real host path or mounts land in the wrong place. Moving the repo means updating this mount and every Execute Command node's `cd` path.
- `data/` bind-mounts to `/home/node/.n8n` and is the only n8n-state persistence.

## Reup pipeline

**Cách vận hành hiện tại (khuyến nghị): `reup-orchestrator-node` + `reup-ui`.** Bộ 6 node
job-queue (`reup-broker` + `reup-ytdlp-node`/`reup-transcribe-node`/`reup-translate-node`/
`reup-tts-gpu-node`/`reup-editor-node`, mỗi node gồm `api`+`worker` dùng chung Redis, xem
README riêng từng node) port lại đúng 5 service `docker_*` bên dưới nhưng qua job-queue (`POST
/jobs`/`GET /jobs/{id}` mỗi node) thay vì `docker compose run --rm`. `reup-orchestrator-node`
(`POST /pipelines`/`GET /pipelines/{id}`) tự động hoá đúng 2 nhánh review/dialogue mô tả bên
dưới bằng cách gọi HTTP API các node theo thứ tự, tự copy file giữa `data/source`/`data/
outputs` riêng của từng node (mount trực tiếp, xem `reup-orchestrator-node/README.md`) — thay
hẳn vai trò sequencing của n8n, không dùng `docker.sock`/DooD. `reup-ui` (React/Vite SPA,
`nginx` reverse-proxy `/api/*`) là giao diện vận hành thay n8n UI. Tất cả `docker_*` bên dưới
**vẫn giữ nguyên làm backup**, các node `reup-*-node` là bản song song độc lập, không import
code chéo (standalone scaffold, đúng convention của repo).

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
  `JOB_TTL_S` (Redis job hash) cũng tăng 24h→48h để khớp cửa sổ 2 ngày này.
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

Mô tả dưới đây (5 node `docker_*`) là kiến trúc gốc — vẫn đúng 100% cho cả 2 cách vận hành, chỉ
khác ai gọi API theo thứ tự (n8n cũ / orchestrator mới / tay qua curl).

5 node nối tiếp, output của node trước là input của node sau (mỗi service chỉ thấy `data/source`/`data/outputs` của chính nó — phải tự copy file qua giữa các bước, trừ khi n8n orchestrate). Có 2 nhánh, chọn bằng `mode` khi gọi `docker_transcribe` (`"review"` mặc định, `"dialogue"` là bổ sung — không đổi gì hành vi nhánh review):

**Nhánh review** (mặc định, `mode=review` — 1 người nói, mute toàn bộ audio gốc):

```
[URL]
  │ docker_yt-dlp          tải video (patch Douyin)
  ▼
source.mp4
  │ docker_transcribe      ffmpeg trích audio -> VAD+Paraformer/whisper (serve mode, GPU) hoặc Deepgram
  ▼
transcript.json
  │ docker_translate       dịch từng segment -> Deepseek
  ▼
translated.json ─────────────────────────────┐
  │ docker_vieneu-tts(_gpu) segments          │ docker_ffmpeg-edit srt
  ▼ TTS ghép theo timestamp (silence-padded)  ▼
track.wav ────────────────────────────► final.srt
              docker_ffmpeg-edit edit  │
     (source.mp4 hình + track.wav + final.srt) 
                     ▼
                 final.mp4
```

| Node | Chức năng | Input | Output |
|---|---|---|---|
| `docker_yt-dlp` | Tải video từ URL (patch Douyin: abogus/cookie tự sinh) | URL | `source.mp4` (video+audio gốc) |
| `docker_transcribe` | Trích audio (ffmpeg) → VAD + route Paraformer(zh)/faster-whisper(khác) + punc/align (serve mode, GPU) hoặc Deepgram fallback → transcript có timestamp | `source.mp4`/audio | `transcript.json` `{language, duration_s, segments:[{id,start,end,text}]}` |
| `docker_translate` | Dịch từng segment qua Deepseek (batch 20/lần), giữ nguyên `start`/`end` | `transcript.json` | `translated.json` — mỗi segment thêm `text_original` (gốc), `text` = bản dịch |
| `docker_vieneu-tts(_gpu)` | 2 subcommand: `tts` (1 file text → 1 wav) hoặc `segments` (transcript JSON → 1 wav ghép theo timestamp, silence-padded) | `translated.json` (hoặc file text thô cho `tts`) | `track.wav` |
| `docker_ffmpeg-edit` | `srt` (JSON → `.srt`) và `edit` (mux video hình gốc + audio mới, sub `none`/`soft`/`burn`, luôn drop audio gốc) | `source.mp4` (chỉ hình) + `track.wav` + `translated.json`/`final.srt` | `final.srt`, `final.mp4` |

**Nhánh video thoại** (`mode=dialogue` ở `docker_transcribe` — giữ nền âm thanh gốc, chỉ mute đúng tiếng Trung, hợp với video nhiều tiếng môi trường/nhân vật thoại; chỉ hỗ trợ `TRANSCRIBE_BACKEND=local`):

```
source.mp4
  │ docker_transcribe (mode=dialogue)
  │   ffmpeg trích audio -> separate (MDX-Net Inst HQ 3 + htdemucs_ft ensemble)
  │   -> vocals.wav (VAD+STT) + instrumental.wav (chọn theo residual_risk) + original.wav
  ▼
transcript.json (segments có thêm residual_risk) + instrumental.wav + original.wav
  │ docker_translate       (không đổi)
  ▼
translated.json
  │ docker_vieneu-tts(_gpu) segments   (không đổi)
  ▼
track.wav
  │ docker_ffmpeg-edit mix-dialogue
  │   instrumental.wav + original.wav + track.wav, theo timestamp translated.json
  │   (đoạn có thoại: instrumental + original*15% + TTS, crossfade ~100ms; đoạn còn lại: original 100%)
  ▼
final_track.wav
  │ docker_ffmpeg-edit edit   (KHÔNG đổi — chỉ đổi -a sang final_track.wav)
  ▼
final.mp4
```

Designed to be orchestrated from `docker_n8n` via Execute Command nodes — each CLI prints a JSON result line to stdout for N8N to parse.

**Quy ước tag image theo version**: `:local` bị ghi đè mỗi lần `docker compose build`, không có đường lùi. Trước khi build đè 1 image dự định giữ lại lâu dài, tag thêm bản cụ thể trước: `docker tag <svc>:local <svc>:vX.Y.Z` (khi image có version nội tại rõ ràng, vd `vieneu-tts-gpu` theo version VieNeu-TTS) hoặc `docker tag <svc>:local <svc>:YYYY-MM-DD` (khi không có, vd yt-dlp/transcribe/translate/ffmpeg-edit/n8n-orchestrator).

## Updating vendored sources

`repo_github/yt-dlp` and `repo_github/VieNeu-TTS` are independent git checkouts of their upstream repos (VieNeu-TTS still has its own `.git`). To pick up upstream changes, `git pull` inside the specific `repo_github/<project>` directory, then rebuild the corresponding `docker_<service>` image(s) — `repo_github/VieNeu-TTS` is shared by both `docker_vieneu-tts` and `docker_vieneu-tts_gpu`, so a pull there means rebuilding both. This repo itself is not (currently) a git repository, so there's no submodule tracking to worry about.
