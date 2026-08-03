FROM python:3.11-slim-bookworm

ENV TZ=Asia/Ho_Chi_Minh \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY reup-orchestrator-node/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN mkdir -p /outputs
WORKDIR /work

COPY reup-orchestrator-node/scripts/ /opt/scripts/
COPY reup-orchestrator-node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
