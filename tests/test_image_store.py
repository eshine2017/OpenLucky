"""Tests for app.image_store — image persistence and cleanup."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app import image_store


class TestSaveTelegramPhoto:
    def test_saves_bytes_and_returns_absolute_path(self, tmp_path):
        images_dir = str(tmp_path / "images")
        data = b"\xff\xd8\xff\xe0fake-jpeg-bytes"

        path = image_store.save_telegram_photo(data, images_dir)

        assert os.path.isabs(path)
        assert os.path.exists(path)

    def test_creates_images_dir_if_missing(self, tmp_path):
        images_dir = str(tmp_path / "new" / "images")
        assert not os.path.exists(images_dir)

        image_store.save_telegram_photo(b"data", images_dir)

        assert os.path.isdir(images_dir)

    def test_filename_has_jpg_extension(self, tmp_path):
        images_dir = str(tmp_path / "images")
        path = image_store.save_telegram_photo(b"data", images_dir)
        assert path.endswith(".jpg")

    def test_file_content_matches_input(self, tmp_path):
        images_dir = str(tmp_path / "images")
        data = b"\x00\x01\x02\x03test-content"

        path = image_store.save_telegram_photo(data, images_dir)

        assert Path(path).read_bytes() == data

    def test_successive_calls_produce_unique_paths(self, tmp_path):
        images_dir = str(tmp_path / "images")

        path1 = image_store.save_telegram_photo(b"a", images_dir)
        path2 = image_store.save_telegram_photo(b"b", images_dir)

        assert path1 != path2

    def test_filename_has_timestamp_prefix(self, tmp_path):
        images_dir = str(tmp_path / "images")
        path = image_store.save_telegram_photo(b"data", images_dir)
        fname = os.path.basename(path)
        # Expect YYYYMMDD-HHMMSS-<uuid8>.jpg — first segment is 8 digits
        parts = fname.split("-")
        assert len(parts) >= 3
        assert parts[0].isdigit() and len(parts[0]) == 8


class TestCleanupOld:
    def test_deletes_stale_files(self, tmp_path):
        images_dir = str(tmp_path / "images")
        os.makedirs(images_dir)
        stale = Path(images_dir) / "old.jpg"
        stale.write_bytes(b"old")
        # Set mtime 25 hours ago
        old_mtime = time.time() - 25 * 3600
        os.utime(stale, (old_mtime, old_mtime))

        count = image_store.cleanup_old(images_dir, retention_hours=24)

        assert count == 1
        assert not stale.exists()

    def test_keeps_fresh_files(self, tmp_path):
        images_dir = str(tmp_path / "images")
        os.makedirs(images_dir)
        fresh = Path(images_dir) / "new.jpg"
        fresh.write_bytes(b"new")
        # mtime is just now — well within 24 hours

        count = image_store.cleanup_old(images_dir, retention_hours=24)

        assert count == 0
        assert fresh.exists()

    def test_tolerates_missing_directory(self):
        count = image_store.cleanup_old("/nonexistent/images/path", retention_hours=24)
        assert count == 0

    def test_returns_count_of_deleted_files(self, tmp_path):
        images_dir = str(tmp_path / "images")
        os.makedirs(images_dir)
        old_mtime = time.time() - 25 * 3600
        for i in range(3):
            f = Path(images_dir) / f"old{i}.jpg"
            f.write_bytes(b"x")
            os.utime(f, (old_mtime, old_mtime))

        count = image_store.cleanup_old(images_dir, retention_hours=24)

        assert count == 3

    def test_mixed_files_only_deletes_stale(self, tmp_path):
        images_dir = str(tmp_path / "images")
        os.makedirs(images_dir)
        old_mtime = time.time() - 25 * 3600

        stale = Path(images_dir) / "stale.jpg"
        stale.write_bytes(b"s")
        os.utime(stale, (old_mtime, old_mtime))

        fresh = Path(images_dir) / "fresh.jpg"
        fresh.write_bytes(b"f")

        image_store.cleanup_old(images_dir, retention_hours=24)

        assert not stale.exists()
        assert fresh.exists()
