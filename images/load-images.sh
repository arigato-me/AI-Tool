#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IN="$ROOT/images"

load() {
  local file="$1"
  echo "==> Loading $file"
  docker load -i "$IN/$file"
}

for f in yt-dlp_local.tar vieneu-tts_local.tar n8n-orchestrator_local.tar transcribe_local.tar translate_local.tar ffmpeg-edit_local.tar vieneu-tts-gpu_local.tar; do
  if [[ -f "$IN/$f" ]]; then
    load "$f"
  else
    echo "Skip (not found): $f"
  fi
done

echo "Done."
docker images | grep -E 'yt-dlp|vieneu-tts|n8n|transcribe|translate|ffmpeg-edit' || true
