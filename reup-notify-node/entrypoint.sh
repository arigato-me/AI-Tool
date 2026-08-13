#!/bin/sh
set -eu
case "${1:-api}" in
  api)
    exec uvicorn api:app --app-dir /opt/scripts --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  worker)
    exec python3 /opt/scripts/worker.py
    ;;
  cli)
    shift
    exec python3 /opt/scripts/notify_cli.py "$@"
    ;;
  *)
    exec uvicorn api:app --app-dir /opt/scripts --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
esac
