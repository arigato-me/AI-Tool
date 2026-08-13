"""Adapter chung cho MỌI webapp tự phát triển của user — 1 module xử lý mọi target, khác nhau
chỉ ở entry trong webhooks.yaml (name/url/header) chứ không viết riêng module cho từng webapp.
File phải upload multipart thật (không truyền path suông) vì webapp đích chạy container/host
khác — container path của reup-notify-node không có nghĩa gì với nó."""
from __future__ import annotations

import os

import requests
import yaml

CONFIG_PATH = "/config/webhooks.yaml"


def _load_targets() -> list[dict]:
    if not os.path.exists(CONFIG_PATH):
        return []
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def target_exists(name: str) -> bool:
    return any(t.get("name") == name for t in _load_targets())


def send(name: str, message: str, file_path: str | None = None) -> dict:
    target = next((t for t in _load_targets() if t.get("name") == name), None)
    if target is None:
        raise RuntimeError(f"webhook '{name}' không có trong {CONFIG_PATH}")

    headers = {}
    if target.get("header_name") and target.get("header_value"):
        headers[target["header_name"]] = target["header_value"]

    files = None
    fh = None
    if file_path:
        if not os.path.exists(file_path):
            raise RuntimeError(f"file_path không tồn tại trong container: {file_path}")
        fh = open(file_path, "rb")
        files = {"file": fh}
    try:
        r = requests.post(target["url"], headers=headers, data={"message": message}, files=files, timeout=30)
        r.raise_for_status()
    finally:
        if fh:
            fh.close()

    return {"ok": True}
