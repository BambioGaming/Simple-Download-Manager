"""
core/segment_worker.py
HTTP Range download worker — one instance per file segment.

Each worker is submitted to a ThreadPoolExecutor by ThreadController.
It downloads bytes [segment.start_byte, segment.end_byte] using an HTTP
Range request, writes them to a temporary .part file, and retries on failure.

Pause/Cancel protocol
---------------------
After every chunk written, the worker:
  1. Checks task.cancel_event — exits immediately if set.
  2. Calls task.pause_event.wait() — blocks (indefinitely) if the event is
     cleared (paused), and returns instantly when it is set again (resumed).

This means pausing costs zero CPU: blocked threads sleep inside the kernel
until the event fires.

Partial retry
-------------
On retry, actual_start = segment.start_byte + segment.downloaded,
so only the un-downloaded bytes are re-requested, not the whole segment.
Retries use linear backoff: sleep(retry_delay * attempt_number).
"""

import logging
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

from core.models import DownloadTask, SegmentInfo, SegmentStatus
from monitoring.progress_tracker import ProgressTracker
from persistence.database import Database
from utils.http_utils import build_range_header

logger = logging.getLogger(__name__)

_CHUNK_SIZE      = 256 * 1024    # 256 KB — balances throughput and progress granularity
_DB_WRITE_INTERVAL = 1.0         # seconds between DB progress flushes per segment


class SegmentWorker:
    """Downloads one byte-range segment of a file."""

    def __init__(
        self,
        segment:     SegmentInfo,
        task:        DownloadTask,
        tracker:     ProgressTracker,
        db:          Database,
        max_retries: int   = 3,
        retry_delay: float = 2.0,
    ) -> None:
        self._segment     = segment
        self._task        = task
        self._tracker     = tracker
        self._db          = db
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        # One dedicated session per worker keeps the TCP connection alive across
        # retry attempts, avoiding repeated handshake + slow-start overhead.
        self._session = requests.Session()
        _adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1)
        self._session.mount("https://", _adapter)
        self._session.mount("http://",  _adapter)

    # ------------------------------------------------------------------
    # Public entry point — called by ThreadPoolExecutor
    # ------------------------------------------------------------------

    def run(self) -> bool:
        """
        Attempt to download the segment, retrying up to max_retries times.
        Returns True on success, False if all retries are exhausted.
        """
        seg = self._segment
        logger.info(
            "Segment %d starting: bytes %d–%d (%s total)",
            seg.segment_index, seg.start_byte, seg.end_byte,
            ProgressTracker.format_bytes(seg.total_bytes),
        )

        try:
            for attempt in range(1, self._max_retries + 1):
                if self._task.cancel_event.is_set():
                    logger.debug("Segment %d: cancel detected before attempt %d", seg.segment_index, attempt)
                    return False

                try:
                    success = self._download_attempt()
                    if success:
                        seg.status = SegmentStatus.COMPLETED
                        self._db.update_segment_progress(seg.db_id, seg.downloaded, SegmentStatus.COMPLETED)
                        logger.info("Segment %d completed successfully.", seg.segment_index)
                        return True
                    # success=False means cancel was requested mid-download
                    return False

                except requests.RequestException as exc:
                    logger.warning(
                        "Segment %d attempt %d/%d failed: %s",
                        seg.segment_index, attempt, self._max_retries, exc,
                    )
                    if attempt < self._max_retries:
                        sleep_time = self._retry_delay * attempt
                        logger.info("Segment %d retrying in %.1f s...", seg.segment_index, sleep_time)
                        time.sleep(sleep_time)

            seg.status = SegmentStatus.FAILED
            self._db.update_segment_progress(seg.db_id, seg.downloaded, SegmentStatus.FAILED)
            logger.error("Segment %d failed after %d attempts.", seg.segment_index, self._max_retries)
            return False
        finally:
            self._session.close()

    # ------------------------------------------------------------------
    # Single download attempt
    # ------------------------------------------------------------------

    def _download_attempt(self) -> bool:
        """
        Download the un-downloaded portion of the segment.

        actual_start = start_byte + downloaded
        This allows the worker to resume from where it left off after a retry
        or an application restart, without re-downloading already-saved bytes.

        Returns True when the segment is fully downloaded.
        Returns False if a cancel was requested.
        Raises requests.RequestException on network errors (caller retries).
        """
        seg          = self._segment
        actual_start = seg.start_byte + seg.downloaded
        actual_end   = seg.end_byte

        if actual_start > actual_end:
            # Already complete (e.g. resumed from a fully-downloaded segment)
            return True

        headers   = build_range_header(actual_start, actual_end)
        part_path = seg.part_file_path

        # Open in append-binary mode: existing bytes from previous attempts
        # are preserved; new bytes are appended.
        part_path.parent.mkdir(parents=True, exist_ok=True)

        seg.status = SegmentStatus.ACTIVE
        self._db.update_segment_progress(seg.db_id, seg.downloaded, SegmentStatus.ACTIVE)

        with self._session.get(
            self._task.url,
            headers=headers,
            stream=True,
            timeout=(10, 30),   # (connect, read)
        ) as response:
            response.raise_for_status()

            # A 200 response means the server ignored our Range header and is
            # sending the full file. We can still handle it for segment 0 only.
            if response.status_code == 200 and seg.segment_index != 0:
                raise requests.RequestException(
                    f"Server returned 200 instead of 206 for segment {seg.segment_index}; "
                    "range requests may not be supported."
                )

            with open(part_path, "ab") as part_file:
                last_db_write = time.monotonic()

                for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                    if not chunk:
                        continue

                    # ---- Cancel check ----
                    if self._task.cancel_event.is_set():
                        logger.debug("Segment %d: cancel during download.", seg.segment_index)
                        return False

                    # ---- Pause check ----
                    self._task.pause_event.wait()

                    # ---- Cancel check after unblocking from pause ----
                    if self._task.cancel_event.is_set():
                        return False

                    part_file.write(chunk)
                    chunk_len = len(chunk)
                    seg.downloaded += chunk_len
                    self._tracker.update(chunk_len)

                    # Write to DB at most once per second per segment.
                    # Every-chunk writes with 4 parallel workers caused heavy
                    # lock contention on the DB and cut throughput significantly.
                    now = time.monotonic()
                    if now - last_db_write >= _DB_WRITE_INTERVAL:
                        self._db.update_segment_progress(
                            seg.db_id, seg.downloaded, SegmentStatus.ACTIVE
                        )
                        last_db_write = now

            # Always flush final progress so resume starts from the right byte
            self._db.update_segment_progress(
                seg.db_id, seg.downloaded, SegmentStatus.ACTIVE
            )

        return True
