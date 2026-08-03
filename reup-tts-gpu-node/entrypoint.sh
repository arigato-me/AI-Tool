#!/bin/sh
set -eu
case "${1:-api}" in
  api)
    exec uvicorn api:app --app-dir /opt/scripts --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec python3 /opt/scripts/worker.py
    ;;
  tts)
    shift
    exec python3 /opt/scripts/tts_cli.py "$@"
    ;;
  segments)
    shift
    exec python3 /opt/scripts/segments_cli.py "$@"
    ;;
  samples)
    shift
    exec python3 /opt/scripts/generate_voice_samples.py "$@"
    ;;
  *)
    exec uvicorn api:app --app-dir /opt/scripts --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
esac
