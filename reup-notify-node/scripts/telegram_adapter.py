"""Telegram Bot API — chính chủ, HTTP thuần (sendMessage + sendDocument), không cookie/session
gì cả. Credential đọc từ /config/telegram.yaml (bot_token, default_chat_id), không phải env —
để 1 config file duy nhất chứa hết bí mật của node này (đồng bộ pattern cookies.txt của
reup-ytdlp-node), không rải secret qua nhiều biến môi trường compose."""
from __future__ import annotations

import os

import requests
import yaml

CONFIG_PATH = "/config/telegram.yaml"


def _load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise RuntimeError(f"chưa cấu hình Telegram — tạo {CONFIG_PATH} với bot_token/default_chat_id")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("bot_token"):
        raise RuntimeError(f"thiếu bot_token trong {CONFIG_PATH}")
    return cfg


def send(message: str, file_path: str | None = None, chat_id: str | None = None) -> dict:
    cfg = _load_config()
    token = cfg["bot_token"]
    chat_id = chat_id or cfg.get("default_chat_id")
    if not chat_id:
        raise RuntimeError("thiếu chat_id — truyền trong job hoặc đặt default_chat_id trong telegram.yaml")

    base = f"https://api.telegram.org/bot{token}"
    r = requests.post(f"{base}/sendMessage", json={"chat_id": chat_id, "text": message}, timeout=30)
    r.raise_for_status()

    if file_path:
        if not os.path.exists(file_path):
            raise RuntimeError(f"file_path không tồn tại trong container: {file_path}")
        with open(file_path, "rb") as f:
            r = requests.post(
                f"{base}/sendDocument",
                data={"chat_id": chat_id},
                files={"document": f},
                timeout=120,
            )
        r.raise_for_status()

    return {"ok": True}
