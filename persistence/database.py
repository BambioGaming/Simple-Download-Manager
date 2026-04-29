"""
persistence/database.py
SQLite persistence layer for SDM.

Stores download records and per-segment byte offsets so downloads can be
resumed after the application restarts.

Thread safety: a single threading.Lock serialises all SQL writes.
SQLite is opened with check_same_thread=False and WAL journal mode,
which allows safe concurrent reads with serialised writes.
"""

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from core.models import DownloadStatus, DownloadTask, SegmentInfo, SegmentStatus

logger = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    url           TEXT    NOT NULL,
    filename      TEXT    NOT NULL,
    save_path     TEXT    NOT NULL,
    total_size    INTEGER DEFAULT 0,
    downloaded    INTEGER DEFAULT 0,
    num_segments  INTEGER DEFAULT 4,
    status        TEXT    DEFAULT 'pending',
    created_at    TEXT    DEFAULT (datetime('now')),
    updated_at    TEXT    DEFAULT (datetime('now')),
    completed_at  TEXT    DEFAULT NULL,
    error_message TEXT    DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    download_id    INTEGER NOT NULL,
    segment_index  INTEGER NOT NULL,
    start_byte     INTEGER NOT NULL,
    end_byte       INTEGER NOT NULL,
    downloaded     INTEGER DEFAULT 0,
    status         TEXT    DEFAULT 'pending',
    part_file_path TEXT    NOT NULL,
    FOREIGN KEY (download_id) REFERENCES downloads(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_segments_download_id ON segments(download_id);
"""


class Database:
    """
    All SQL operations for SDM. The rest of the application never writes raw SQL.

    Usage:
        db = Database("sdm.db")
        db.initialize()
        did = db.insert_download(task)
    """

    def __init__(self, db_path: str = "sdm.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Open the database connection and create tables if they do not exist."""
        self._conn = sqlite3.connect(
            self._db_path,
            check_same_thread=False,  # we manage thread safety ourselves via _lock
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        logger.info("Database initialised at %s", self._db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Download CRUD
    # ------------------------------------------------------------------

    def insert_download(self, task: DownloadTask) -> int:
        """Insert a new download row. Returns the auto-increment id."""
        sql = """
            INSERT INTO downloads (url, filename, save_path, total_size, num_segments, status)
            VALUES (?, ?, ?, ?, ?, ?)
        """
        with self._lock:
            cur = self._conn.execute(
                sql,
                (
                    task.url,
                    task.filename,
                    str(task.save_path),
                    task.total_size,
                    task.num_segments,
                    task.status.value,
                ),
            )
            self._conn.commit()
            row_id = cur.lastrowid
        logger.debug("Inserted download id=%d url=%s", row_id, task.url)
        return row_id

    def update_download_status(
        self,
        download_id: int,
        status: DownloadStatus,
        error_message: str | None = None,
    ) -> None:
        completed_at_expr = "datetime('now')" if status == DownloadStatus.COMPLETED else "NULL"
        sql = f"""
            UPDATE downloads
            SET status        = ?,
                error_message = ?,
                updated_at    = datetime('now'),
                completed_at  = {completed_at_expr}
            WHERE id = ?
        """
        with self._lock:
            self._conn.execute(sql, (status.value, error_message, download_id))
            self._conn.commit()

    def update_download_progress(self, download_id: int, downloaded: int) -> None:
        sql = """
            UPDATE downloads
            SET downloaded = ?, updated_at = datetime('now')
            WHERE id = ?
        """
        with self._lock:
            self._conn.execute(sql, (downloaded, download_id))
            self._conn.commit()

    def get_all_downloads(self) -> list[dict[str, Any]]:
        """Return all download rows as a list of plain dicts (for history view)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM downloads ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_download_by_id(self, download_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (download_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_download(self, download_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))
            self._conn.commit()

    def get_incomplete_downloads(self) -> list[dict[str, Any]]:
        """Return downloads that were interrupted (status: downloading or paused)."""
        sql = "SELECT * FROM downloads WHERE status IN ('downloading', 'paused')"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Segment CRUD
    # ------------------------------------------------------------------

    def insert_segments(self, download_id: int, segments: list[SegmentInfo]) -> None:
        """Bulk-insert all segments for a download and populate db_id on each."""
        sql = """
            INSERT INTO segments
                (download_id, segment_index, start_byte, end_byte, downloaded, status, part_file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        with self._lock:
            for seg in segments:
                cur = self._conn.execute(
                    sql,
                    (
                        download_id,
                        seg.segment_index,
                        seg.start_byte,
                        seg.end_byte,
                        seg.downloaded,
                        seg.status.value,
                        str(seg.part_file_path),
                    ),
                )
                seg.db_id = cur.lastrowid
            self._conn.commit()
        logger.debug("Inserted %d segments for download id=%d", len(segments), download_id)

    def update_segment_progress(
        self, segment_db_id: int, downloaded: int, status: SegmentStatus
    ) -> None:
        sql = "UPDATE segments SET downloaded = ?, status = ? WHERE id = ?"
        with self._lock:
            self._conn.execute(sql, (downloaded, status.value, segment_db_id))
            self._conn.commit()

    def get_segments_for_download(self, download_id: int) -> list[dict[str, Any]]:
        sql = "SELECT * FROM segments WHERE download_id = ? ORDER BY segment_index"
        with self._lock:
            rows = self._conn.execute(sql, (download_id,)).fetchall()
        return [dict(r) for r in rows]
