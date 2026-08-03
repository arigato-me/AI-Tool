"""Quản lý thư viện nhạc nền theo project/theme — sở hữu bởi editor-node (chủ ghi duy nhất
của `data/music`). Logic thuần path-safe + filesystem, không phụ thuộc FastAPI/Redis, để test
được bằng unittest thường (xem test_music_library.py)."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path

MUSIC_ROOT = Path(os.environ.get("MUSIC_DIR", "/music"))
PROJECT_META_FILE = "project.json"
UNGROUPED_SLUG = "_ungrouped"
ALLOWED_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
MAX_TRACK_BYTES = 30 * 1024 * 1024
MAX_SLUG_LEN = 60


class MusicLibraryError(ValueError):
    pass


def slugify(name: str) -> str | None:
    """"Nhạc buồn" -> "nhac-buon". Trả None nếu sau khi lọc không còn ký tự hợp lệ nào."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")[:MAX_SLUG_LEN]
    return slug or None


def _safe_slug(seg: str) -> str:
    """Chặn path traversal cho 1 segment tên (project slug hoặc tên track) tới từ HTTP body/path
    — cùng tinh thần `Path(...).name` đã dùng ở pipeline_runner.py, nhưng raise rõ ràng thay vì
    âm thầm cắt còn "passwd" như `Path("../../etc/passwd").name`."""
    seg = (seg or "").strip()
    if not seg or seg in (".", "..") or seg != Path(seg).name:
        raise MusicLibraryError(f"tên không hợp lệ: {seg!r}")
    return seg


def _project_dir(slug: str) -> Path:
    return MUSIC_ROOT / _safe_slug(slug)


def _unique_dir_name(parent: Path, base: str) -> str:
    """Chống trùng: "nhac-buon" đã tồn tại -> "nhac-buon-2", "nhac-buon-3", ..."""
    if not (parent / base).exists():
        return base
    n = 2
    while (parent / f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


def _unique_file_name(parent: Path, stem: str, ext: str) -> str:
    if not (parent / f"{stem}{ext}").exists():
        return f"{stem}{ext}"
    n = 2
    while (parent / f"{stem}-{n}{ext}").exists():
        n += 1
    return f"{stem}-{n}{ext}"


def _read_project_meta(project_path: Path) -> dict:
    meta_path = project_path / PROJECT_META_FILE
    if meta_path.is_file():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"slug": project_path.name, "display_name": project_path.name}


def _track_count(project_path: Path) -> int:
    return sum(1 for p in project_path.iterdir() if p.is_file() and p.suffix.lower() in ALLOWED_EXT)


def list_projects() -> list[dict]:
    """Danh sách project + số track mỗi project. Gom file rời nằm ngay gốc `data/music` (nếu
    deployment thật đã thả file trước khi có tính năng này) thành pseudo-project chỉ-đọc
    `_ungrouped` — không ép re-upload, xem plan."""
    if not MUSIC_ROOT.is_dir():
        return []
    projects = []
    root_loose_files = []
    for entry in sorted(MUSIC_ROOT.iterdir()):
        if entry.is_dir() and (entry / PROJECT_META_FILE).is_file():
            meta = _read_project_meta(entry)
            projects.append({
                "slug": entry.name,
                "display_name": meta.get("display_name", entry.name),
                "track_count": _track_count(entry),
            })
        elif entry.is_file() and entry.suffix.lower() in ALLOWED_EXT:
            root_loose_files.append(entry)
    if root_loose_files:
        projects.append({
            "slug": UNGROUPED_SLUG,
            "display_name": "(chưa phân nhóm)",
            "track_count": len(root_loose_files),
        })
    return projects


def list_tracks(slug: str) -> list[dict]:
    slug = _safe_slug(slug)
    project_path = MUSIC_ROOT if slug == UNGROUPED_SLUG else _project_dir(slug)
    if not project_path.is_dir():
        raise MusicLibraryError(f"project không tồn tại: {slug}")
    tracks = []
    for p in sorted(project_path.iterdir()):
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT:
            tracks.append({
                "track": p.name,
                "display_name": p.stem,
                "ext": p.suffix.lower().lstrip("."),
                "size": p.stat().st_size,
            })
    return tracks


def create_project(display_name: str) -> dict:
    display_name = (display_name or "").strip()
    if not display_name:
        raise MusicLibraryError("display_name không được để trống")
    base_slug = slugify(display_name)
    if not base_slug:
        raise MusicLibraryError(f"không tạo được tên thư mục hợp lệ từ: {display_name!r}")
    MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
    slug = _unique_dir_name(MUSIC_ROOT, base_slug)
    project_path = MUSIC_ROOT / slug
    project_path.mkdir(parents=True)
    meta = {"slug": slug, "display_name": display_name}
    (project_path / PROJECT_META_FILE).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"slug": slug, "display_name": display_name, "track_count": 0}


def save_track(slug: str, filename: str, data_b64: str) -> dict:
    project_path = _project_dir(slug)
    if not project_path.is_dir():
        raise MusicLibraryError(f"project không tồn tại: {slug}")

    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise MusicLibraryError(f"đuôi file không hỗ trợ: {ext!r} (chỉ nhận {sorted(ALLOWED_EXT)})")

    try:
        raw = base64.b64decode(data_b64, validate=True)
    except (ValueError, TypeError) as e:
        raise MusicLibraryError(f"data_b64 không hợp lệ: {e}") from e
    if len(raw) > MAX_TRACK_BYTES:
        raise MusicLibraryError(f"file vượt giới hạn {MAX_TRACK_BYTES // (1024*1024)}MB")
    if len(raw) == 0:
        raise MusicLibraryError("file rỗng")

    stem_source = Path(filename).stem.strip() or "track"
    base_stem = slugify(stem_source) or "track"
    track_name = _unique_file_name(project_path, base_stem, ext)
    (project_path / track_name).write_bytes(raw)
    return {
        "track": track_name,
        "display_name": Path(track_name).stem,
        "ext": ext.lstrip("."),
        "size": len(raw),
    }


DEFAULT_POINTER_FILE = "_default.json"


def _default_pointer_path() -> Path:
    return MUSIC_ROOT / DEFAULT_POINTER_FILE


def get_default() -> dict | None:
    """Track nhạc nền mặc định cho nhánh review (`reup-ui` SubmitJob tự chọn sẵn đúng track
    này thay vì heuristic "track đầu tiên" cũ) — con trỏ đơn (1 track duy nhất toàn thư viện,
    không phải mỗi project 1 default). Tự "quên" nếu track trỏ tới đã bị xoá (resolve lại qua
    `resolve_track_path` mỗi lần đọc, không tin thẳng con trỏ cũ) — tránh SubmitJob tiền điền
    vào 1 track không còn tồn tại."""
    path = _default_pointer_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        slug, track = data["project"], data["track"]
    except (json.JSONDecodeError, OSError, KeyError, TypeError):
        return None
    try:
        resolve_track_path(slug, track)
    except MusicLibraryError:
        return None
    return {"project": slug, "track": track}


def set_default(slug: str, track: str) -> dict:
    resolve_track_path(slug, track)  # raise nếu track không tồn tại — validate trước khi ghi
    slug, track = _safe_slug(slug), _safe_slug(track)
    MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
    _default_pointer_path().write_text(
        json.dumps({"project": slug, "track": track}, ensure_ascii=False), encoding="utf-8",
    )
    return {"project": slug, "track": track}


def clear_default() -> None:
    path = _default_pointer_path()
    if path.is_file():
        path.unlink()


def resolve_track_path(slug: str, track: str) -> Path:
    slug = _safe_slug(slug)
    track = _safe_slug(track)
    project_path = MUSIC_ROOT if slug == UNGROUPED_SLUG else _project_dir(slug)
    track_path = project_path / track
    if not track_path.is_file() or track_path.suffix.lower() not in ALLOWED_EXT:
        raise MusicLibraryError(f"track không tồn tại: {slug}/{track}")
    return track_path


def delete_track(slug: str, track: str) -> None:
    """Xoá 1 track — dùng chung `resolve_track_path()` nên hỗ trợ cả track rời trong
    `_ungrouped` (nằm thẳng ở `MUSIC_ROOT`, không phải project thật) lẫn track trong project."""
    track_path = resolve_track_path(slug, track)
    track_path.unlink()


def delete_project(slug: str) -> None:
    """Xoá cả project (thư mục + mọi track bên trong + `project.json`) — KHÔNG cho xoá
    `_ungrouped` vì đó không phải 1 thư mục thật (chỉ là file rời nằm thẳng ở gốc
    `MUSIC_ROOT`), xoá "project" đó sẽ phải xoá từng file rời chứ không phải rmtree 1 thư mục,
    dễ hiểu lầm/xoá nhầm việc khác — muốn dọn file rời thì xoá từng track qua `delete_track()`."""
    if slug == UNGROUPED_SLUG:
        raise MusicLibraryError("không xoá được nhóm \"(chưa phân nhóm)\" — đây là file rời ở gốc, không phải project thật; xoá từng track riêng")
    project_path = _project_dir(slug)
    if not project_path.is_dir():
        raise MusicLibraryError(f"project không tồn tại: {slug}")
    shutil.rmtree(project_path)
