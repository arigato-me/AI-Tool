"""Unit test thuần stdlib (unittest) cho music_library.py — không thêm dependency mới, khớp
mức rigor hiện có của repo (chưa có pytest ở node nào). Chạy: python3 -m unittest
test_music_library -v (trong container editor hoặc máy có Python 3.11+, không cần ffmpeg/redis)."""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

import music_library as ml

SILENCE_WAV_B64 = base64.b64encode(b"RIFF....WAVEfmt ").decode("ascii")


class SlugifyTests(unittest.TestCase):
    def test_vietnamese_display_name(self):
        self.assertEqual(ml.slugify("Nhạc buồn"), "nhac-buon")

    def test_only_invalid_chars_returns_none(self):
        self.assertIsNone(ml.slugify("???"))

    def test_truncates_to_max_len(self):
        long_name = "a" * 200
        self.assertLessEqual(len(ml.slugify(long_name)), ml.MAX_SLUG_LEN)


class SafeSlugTests(unittest.TestCase):
    def test_rejects_path_traversal(self):
        for bad in ("../etc", "a/b", "", "..", "."):
            with self.assertRaises(ml.MusicLibraryError):
                ml._safe_slug(bad)

    def test_accepts_normal_name(self):
        self.assertEqual(ml._safe_slug("nhac-buon"), "nhac-buon")


class MusicLibraryFileOpsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_root = ml.MUSIC_ROOT
        ml.MUSIC_ROOT = Path(self._tmpdir.name)

    def tearDown(self):
        ml.MUSIC_ROOT = self._orig_root
        self._tmpdir.cleanup()

    def test_create_project_then_duplicate_gets_suffix(self):
        p1 = ml.create_project("Nhạc buồn")
        self.assertEqual(p1["slug"], "nhac-buon")
        p2 = ml.create_project("Nhạc buồn")
        self.assertEqual(p2["slug"], "nhac-buon-2")

    def test_create_project_empty_name_raises(self):
        with self.assertRaises(ml.MusicLibraryError):
            ml.create_project("   ")

    def test_save_track_rejects_bad_extension(self):
        project = ml.create_project("Theme A")
        with self.assertRaises(ml.MusicLibraryError):
            ml.save_track(project["slug"], "malware.exe", SILENCE_WAV_B64)

    def test_save_track_rejects_oversized_file(self):
        project = ml.create_project("Theme B")
        huge_b64 = base64.b64encode(b"0" * (ml.MAX_TRACK_BYTES + 1)).decode("ascii")
        with self.assertRaises(ml.MusicLibraryError):
            ml.save_track(project["slug"], "big.mp3", huge_b64)

    def test_save_track_duplicate_name_gets_suffix(self):
        project = ml.create_project("Theme C")
        t1 = ml.save_track(project["slug"], "song.mp3", SILENCE_WAV_B64)
        t2 = ml.save_track(project["slug"], "song.mp3", SILENCE_WAV_B64)
        self.assertEqual(t1["track"], "song.mp3")
        self.assertEqual(t2["track"], "song-2.mp3")

    def test_save_track_never_overwrites_project_json(self):
        project = ml.create_project("Theme D")
        with self.assertRaises(ml.MusicLibraryError):
            ml.save_track(project["slug"], "project.json", SILENCE_WAV_B64)  # đuôi .json không nằm trong ALLOWED_EXT

    def test_list_projects_includes_track_count(self):
        project = ml.create_project("Theme E")
        ml.save_track(project["slug"], "a.mp3", SILENCE_WAV_B64)
        ml.save_track(project["slug"], "b.wav", SILENCE_WAV_B64)
        projects = ml.list_projects()
        match = next(p for p in projects if p["slug"] == project["slug"])
        self.assertEqual(match["track_count"], 2)

    def test_list_projects_groups_loose_root_files_as_ungrouped(self):
        (ml.MUSIC_ROOT).mkdir(parents=True, exist_ok=True)
        (ml.MUSIC_ROOT / "legacy.mp3").write_bytes(b"fake")
        projects = ml.list_projects()
        ungrouped = next(p for p in projects if p["slug"] == ml.UNGROUPED_SLUG)
        self.assertEqual(ungrouped["track_count"], 1)

    def test_resolve_track_path_rejects_traversal_in_track_name(self):
        project = ml.create_project("Theme F")
        ml.save_track(project["slug"], "song.mp3", SILENCE_WAV_B64)
        with self.assertRaises(ml.MusicLibraryError):
            ml.resolve_track_path(project["slug"], "../../etc/passwd")

    def test_resolve_track_path_success(self):
        project = ml.create_project("Theme G")
        track = ml.save_track(project["slug"], "song.mp3", SILENCE_WAV_B64)
        path = ml.resolve_track_path(project["slug"], track["track"])
        self.assertTrue(path.is_file())

    def test_get_default_none_when_never_set(self):
        self.assertIsNone(ml.get_default())

    def test_set_then_get_default(self):
        project = ml.create_project("Theme H")
        track = ml.save_track(project["slug"], "song.mp3", SILENCE_WAV_B64)
        ml.set_default(project["slug"], track["track"])
        self.assertEqual(ml.get_default(), {"project": project["slug"], "track": track["track"]})

    def test_set_default_rejects_nonexistent_track(self):
        project = ml.create_project("Theme I")
        with self.assertRaises(ml.MusicLibraryError):
            ml.set_default(project["slug"], "khong-ton-tai.mp3")

    def test_set_default_overwrites_previous_default(self):
        project = ml.create_project("Theme J")
        t1 = ml.save_track(project["slug"], "a.mp3", SILENCE_WAV_B64)
        t2 = ml.save_track(project["slug"], "b.mp3", SILENCE_WAV_B64)
        ml.set_default(project["slug"], t1["track"])
        ml.set_default(project["slug"], t2["track"])
        self.assertEqual(ml.get_default(), {"project": project["slug"], "track": t2["track"]})

    def test_clear_default(self):
        project = ml.create_project("Theme K")
        track = ml.save_track(project["slug"], "song.mp3", SILENCE_WAV_B64)
        ml.set_default(project["slug"], track["track"])
        ml.clear_default()
        self.assertIsNone(ml.get_default())

    def test_clear_default_noop_when_nothing_set(self):
        ml.clear_default()  # không raise dù chưa từng set

    def test_get_default_self_heals_when_track_deleted(self):
        project = ml.create_project("Theme L")
        track = ml.save_track(project["slug"], "song.mp3", SILENCE_WAV_B64)
        ml.set_default(project["slug"], track["track"])
        ml.delete_track(project["slug"], track["track"])
        # Track mặc định đã bị xoá -> get_default() tự "quên", không trả con trỏ chết.
        self.assertIsNone(ml.get_default())

    def test_default_pointer_file_not_listed_as_loose_track(self):
        project = ml.create_project("Theme M")
        track = ml.save_track(project["slug"], "song.mp3", SILENCE_WAV_B64)
        ml.set_default(project["slug"], track["track"])
        # `_default.json` nằm ở gốc MUSIC_ROOT (giống nơi chứa file rời "_ungrouped") — không
        # được lẫn vào danh sách project/track rời.
        projects = ml.list_projects()
        self.assertFalse(any(p["slug"] == ml.UNGROUPED_SLUG for p in projects))


if __name__ == "__main__":
    unittest.main()
