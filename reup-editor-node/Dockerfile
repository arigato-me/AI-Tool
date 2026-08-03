FROM python:3.11-slim-bookworm

ENV TZ=Asia/Ho_Chi_Minh \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# numpy/soundfile/pyyaml/scipy — nhánh video thoại (mix-dialogue); fastapi/uvicorn/redis —
# api+worker; nhánh review (edit/srt) vẫn thuần stdlib + gọi ffmpeg qua subprocess, không đổi.
COPY reup-editor-node/pyproject.toml /workspace/pyproject.toml
WORKDIR /workspace
RUN uv sync --no-dev

RUN mkdir -p /source /outputs
WORKDIR /work

COPY reup-editor-node/scripts/ /opt/scripts/
COPY reup-editor-node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/scripts/edit_cli.py /opt/scripts/json_to_srt.py /opt/scripts/mix_dialogue_cli.py

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
