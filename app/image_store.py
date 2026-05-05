"""
image_store.py — Persist Telegram photos to disk and clean up stale ones.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


def save_telegram_photo(file_bytes: bytes, images_dir: str) -> str:
    """
    Write *file_bytes* to *images_dir* and return the absolute path.

    Filename format: YYYYMMDD-HHMMSS-<uuid8>.jpg
    Creates *images_dir* if it does not exist.
    """
    os.makedirs(images_dir, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    uid = uuid.uuid4().hex[:8]
    filename = f"{ts}-{uid}.jpg"
    path = os.path.join(images_dir, filename)
    with open(path, "wb") as fh:
        fh.write(file_bytes)
    logger.debug("Saved photo to %s (%d bytes)", path, len(file_bytes))
    return os.path.abspath(path)


def cleanup_old(images_dir: str, retention_hours: int = 24) -> int:
    """
    Delete files in *images_dir* whose mtime is older than *retention_hours*.

    Returns the number of files deleted.
    Tolerates a missing directory (returns 0).
    """
    if not os.path.isdir(images_dir):
        return 0

    cutoff = time.time() - retention_hours * 3600
    deleted = 0
    for fname in os.listdir(images_dir):
        fpath = os.path.join(images_dir, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                deleted += 1
                logger.debug("Deleted stale image: %s", fpath)
        except OSError as exc:
            logger.warning("Could not delete %s: %s", fpath, exc)

    if deleted:
        logger.info("Cleaned up %d stale image(s) from %s", deleted, images_dir)
    return deleted
