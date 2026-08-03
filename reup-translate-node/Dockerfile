FROM python:3.11-slim-bookworm

ENV TZ=Asia/Ho_Chi_Minh \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY reup-translate-node/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN mkdir -p /source /outputs
WORKDIR /work

COPY reup-translate-node/scripts/ /opt/scripts/
COPY reup-translate-node/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /opt/scripts/translate_cli.py

ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
