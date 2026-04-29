"""
core/download_manager.py
Central coordinator for all downloads — the "brain" of SDM.

The DownloadManager exposes a clean API that the UI layer calls:
  add_download()    — probe URL, register task, persist to DB
  start_download()  — spawn a coordinator daemon thread
  pause_download()  — clear pause_event so workers block
  resume_download() — set pause_event so workers unblock
  cancel_download() — set cancel_event so workers exit, clean up parts
  get_progress()    — return live metrics snapshot for the UI
  get_all_downloads()  — history view
  restore_incomplete() — reload interrupted downloads on startup

Thread model
------------
  main thread               — UI; calls public API methods
  coordinator daemon thread — one per active download; runs _run_download()
  worker threads            — N per download inside a ThreadPoolExecutor

The coordinator thread never touches Tkinter widgets. The UI polls
get_progress() every 500 ms via Tkinter's after() mechanism.
"""

import logging
import threading
from pathlib import Path
from typing import Callable

from core.file_assembler import FileAssembler, FileAssemblyError
from core.models import DownloadStatus, DownloadTask, SegmentInfo, SegmentStatus
from core.segment_worker import SegmentWorker
from core.thread_controller import ThreadController
from monitoring.progress_tracker import ProgressTracker
from persistence.database import Database
from utils.http_utils import probe_url

logger = logging.getLogger(__name__)


class DownloadManager:
    """
    Coordinates all download tasks. One instance lives for the lifetime of the app.

    Parameters
    ----------
    db            : Database  — persistence layer
    download_dir  : Path      — where final files and .part files are stored
    num_segments  : int       — default segment count (overridable per-download)
    max_retries   : int       — default retry count for each segment
    on_status_change : callable | None
                    — optional callback(download_id, new_status) for the UI
    """

    def __init__(
        self,
        db:              Database,
        download_dir:    Path = Path("downloads"),
        num_segments:    int  = 4,
        max_retries:     int  = 3,
        on_status_change: Callable[[int, DownloadStatus], None] | None = None,
    ) -> None:
        self._db               = db
        self._download_dir     = download_dir
        self._num_segments     = num_segments
        self._max_retries      = max_retries
        self._on_status_change = on_status_change

        # Active tasks keyed by download_id (db_id)
        self._active_tasks:    dict[int, DownloadTask]       = {}
        # ProgressTracker per active download
        self._trackers:        dict[int, ProgressTracker]    = {}
        # ThreadController per active download
        self._controllers:     dict[int, ThreadController]   = {}
        self._lock             = threading.Lock()

        download_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_download(
        self,
        url:          str,
        save_path:    Path | None = None,
        num_segments: int | None  = None,
    ) -> int:
        """
        Probe the URL, create a DownloadTask, persist it to the database,
        and return the download_id. Does NOT start downloading yet.

        Raises ValueError if the URL is unreachable or returns an error.
        """
        info = probe_url(url)

        save_dir     = save_path or self._download_dir
        n_segments   = num_segments or self._num_segments
        total_size   = info["total_size"]
        accepts_ranges = info["accepts_ranges"]

        # Fall back to single segment if the server does not support ranges
        if not accepts_ranges or total_size == 0:
            logger.warning(
                "Server does not support range requests or size is unknown. "
                "Using single-segment download."
            )
            n_segments = 1

        task = DownloadTask(
            url          = url,
            filename     = info["filename"],
            save_path    = save_dir,
            total_size   = total_size,
            num_segments = n_segments,
        )
        task.segments = self._compute_segments(task)

        task.db_id = self._db.insert_download(task)
        self._db.insert_segments(task.db_id, task.segments)

        with self._lock:
            self._active_tasks[task.db_id] = task

        logger.info(
            "Download added: id=%d filename=%s segments=%d size=%d",
            task.db_id, task.filename, n_segments, total_size,
        )
        return task.db_id

    def start_download(self, download_id: int) -> None:
        """
        Launch a daemon coordinator thread for the given download.
        Returns immediately — the download runs in the background.

        If the task is PAUSED and a coordinator thread is already running
        (i.e. the download was paused mid-flight, not restored from DB),
        this delegates to resume_download() instead of spawning a duplicate.

        Raises KeyError if download_id is not tracked.
        """
        task = self._get_task(download_id)
        if task.status not in (DownloadStatus.PENDING, DownloadStatus.PAUSED):
            logger.warning(
                "start_download called on task %d with status %s — ignored.",
                download_id, task.status,
            )
            return

        # Paused mid-flight: coordinator thread is still alive waiting for workers.
        # Just unblock the workers instead of spawning a second coordinator.
        if task.status == DownloadStatus.PAUSED:
            with self._lock:
                has_active_controller = download_id in self._controllers
            if has_active_controller:
                self.resume_download(download_id)
                return

        self._transition(task, DownloadStatus.DOWNLOADING)
        task.pause_event.set()    # ensure the green light is on

        thread = threading.Thread(
            target=self._run_download,
            args=(task,),
            daemon=True,
            name=f"sdm-task-{download_id}",
        )
        thread.start()
        logger.info("Coordinator thread started for download %d.", download_id)

    def pause_download(self, download_id: int) -> None:
        """
        Signal all workers to pause after their next chunk.
        Workers block in pause_event.wait() until resume_download() is called.
        """
        task = self._get_task(download_id)
        if task.status != DownloadStatus.DOWNLOADING:
            return
        task.pause_event.clear()   # workers block here at next chunk
        self._transition(task, DownloadStatus.PAUSED)
        logger.info("Download %d paused.", download_id)

    def resume_download(self, download_id: int) -> None:
        """
        Resume a paused download.
        - If the coordinator thread is still alive (paused mid-flight), unblock its workers.
        - If there is no live coordinator (restored from DB on startup), spawn a new one.
        """
        task = self._get_task(download_id)
        if task.status != DownloadStatus.PAUSED:
            return
        with self._lock:
            has_active_controller = download_id in self._controllers
        if has_active_controller:
            self._transition(task, DownloadStatus.DOWNLOADING)
            task.pause_event.set()
            logger.info("Download %d resumed (workers unblocked).", download_id)
        else:
            # Restored from DB — no live workers; start a fresh coordinator
            self.start_download(download_id)

    def cancel_download(self, download_id: int) -> None:
        """
        Signal workers to exit at their next chunk boundary,
        then clean up all .part files.
        """
        task = self._get_task(download_id)
        if task.status in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
            return

        # Unblock any paused workers first so they can see the cancel signal
        task.pause_event.set()
        task.cancel_event.set()

        self._transition(task, DownloadStatus.CANCELLED)

        # Mark all incomplete segments as cancelled so the DB reflects reality
        for seg in task.segments:
            if seg.status not in (SegmentStatus.COMPLETED,):
                seg.status = SegmentStatus.CANCELLED
                self._db.update_segment_progress(
                    seg.db_id, seg.downloaded, SegmentStatus.CANCELLED
                )

        logger.info("Download %d cancelled.", download_id)

        # Clean up part files in a background thread so the UI stays responsive.
        # Wait for the controller first so all workers have closed their file
        # handles before we attempt to delete on Windows (avoids WinError 32).
        with self._lock:
            controller = self._controllers.get(download_id)

        def _cleanup():
            if controller:
                controller.wait_for_completion()
            FileAssembler(task).cleanup_parts()
        threading.Thread(target=_cleanup, daemon=True).start()

    def get_progress(self, download_id: int) -> dict | None:
        """
        Return a live metrics dict for the given download, or None if not found.
        Also injects 'status' and 'filename' so the UI has everything it needs.
        """
        with self._lock:
            task    = self._active_tasks.get(download_id)
            tracker = self._trackers.get(download_id)

        if task is None:
            return None

        if tracker is None:
            # Task exists but hasn't started yet (pending) or is complete
            return {
                "downloaded":  task.downloaded_bytes,
                "total":       task.total_size,
                "percent":     task.progress_percent,
                "speed_bps":   0.0,
                "speed_human": "-- B/s",
                "eta_seconds": -1.0,
                "eta_human":   "--:--",
                "elapsed":     0.0,
                "status":      task.status.value,
                "filename":    task.filename,
            }

        stats = tracker.get_stats()
        stats["status"]   = task.status.value
        stats["filename"] = task.filename
        return stats

    def get_all_downloads(self) -> list[dict]:
        """Return all download records from the database (for history view)."""
        return self._db.get_all_downloads()

    def delete_download_record(self, download_id: int) -> None:
        """Remove a download from history. Only valid for completed/failed/cancelled."""
        with self._lock:
            task = self._active_tasks.get(download_id)
        if task and task.status not in (
            DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED
        ):
            return
        self._db.delete_download(download_id)
        with self._lock:
            self._active_tasks.pop(download_id, None)
            self._trackers.pop(download_id, None)

    def restore_incomplete(self) -> list[int]:
        """
        On application startup, load interrupted downloads from the database
        and reconstruct DownloadTask objects so the user can resume them.

        Returns list of download_ids that were restored.
        """
        rows = self._db.get_incomplete_downloads()
        restored = []

        for row in rows:
            seg_rows = self._db.get_segments_for_download(row["id"])
            segments = []
            for sr in seg_rows:
                part_path = Path(sr["part_file_path"])
                # Trust the file size over the DB value (crash safety)
                downloaded = part_path.stat().st_size if part_path.exists() else 0

                segments.append(SegmentInfo(
                    segment_index  = sr["segment_index"],
                    start_byte     = sr["start_byte"],
                    end_byte       = sr["end_byte"],
                    part_file_path = part_path,
                    downloaded     = downloaded,
                    status         = SegmentStatus.PENDING,
                    db_id          = sr["id"],
                ))

            task = DownloadTask(
                url          = row["url"],
                filename     = row["filename"],
                save_path    = Path(row["save_path"]),
                total_size   = row["total_size"],
                num_segments = row["num_segments"],
                segments     = segments,
                status       = DownloadStatus.PAUSED,  # always start paused
                db_id        = row["id"],
            )
            # Keep pause_event CLEARED so the task appears paused
            task.pause_event.clear()

            # Update DB to reflect the paused state
            self._db.update_download_status(task.db_id, DownloadStatus.PAUSED)

            with self._lock:
                self._active_tasks[task.db_id] = task

            logger.info("Restored download id=%d (%s) as paused.", task.db_id, task.filename)
            restored.append(task.db_id)

        return restored

    def get_active_task_ids(self) -> list[int]:
        with self._lock:
            return list(self._active_tasks.keys())

    # ------------------------------------------------------------------
    # Internal: download coordinator (runs in daemon thread)
    # ------------------------------------------------------------------

    def _run_download(self, task: DownloadTask) -> None:
        """
        Orchestrates the full download lifecycle for one task.
        Runs in a daemon thread spawned by start_download().
        """
        download_id = task.db_id

        # Seed the tracker with bytes already on disk so progress % is correct from the start
        tracker = ProgressTracker(task.total_size, initial_downloaded=task.downloaded_bytes)
        controller = ThreadController(max_workers=task.num_segments)

        with self._lock:
            self._trackers[download_id]    = tracker
            self._controllers[download_id] = controller

        # Build workers for segments that are not yet complete
        workers = [
            SegmentWorker(
                segment     = seg,
                task        = task,
                tracker     = tracker,
                db          = self._db,
                max_retries = self._max_retries,
            )
            for seg in task.segments
            if not seg.is_complete
        ]

        if not workers:
            # All segments already downloaded (e.g. restored from DB and complete)
            logger.info("All segments already complete for download %d. Assembling.", download_id)
        else:
            controller.start_segments(workers)
            all_ok, failed_indices = controller.wait_for_completion()
            controller.shutdown()

            if task.cancel_event.is_set():
                logger.info("Download %d was cancelled.", download_id)
                with self._lock:
                    self._trackers.pop(download_id, None)
                    self._controllers.pop(download_id, None)
                return

            if not all_ok:
                error_msg = f"Segments failed after retries: {failed_indices}"
                self._transition(task, DownloadStatus.FAILED, error_msg)
                with self._lock:
                    self._trackers.pop(download_id, None)
                    self._controllers.pop(download_id, None)
                return

        # All segments succeeded — assemble the final file
        try:
            assembler = FileAssembler(task)
            assembler.assemble()
        except FileAssemblyError as exc:
            self._transition(task, DownloadStatus.FAILED, str(exc))
            logger.error("Assembly failed for download %d: %s", download_id, exc)
        else:
            self._transition(task, DownloadStatus.COMPLETED)
            self._db.update_download_progress(download_id, task.total_size)
            logger.info("Download %d completed: %s", download_id, task.final_path)

        with self._lock:
            self._trackers.pop(download_id, None)
            self._controllers.pop(download_id, None)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_segments(self, task: DownloadTask) -> list[SegmentInfo]:
        """
        Split [0, total_size-1] into task.num_segments equal byte ranges.
        The last segment absorbs any remainder bytes.

        Example: 100 bytes, 4 segments → [0-24], [25-49], [50-74], [75-99]
        """
        total    = task.total_size
        n        = task.num_segments
        segments = []

        if total == 0 or n == 1:
            # Unknown size or forced single-segment: one range covering everything
            seg = SegmentInfo(
                segment_index  = 0,
                start_byte     = 0,
                end_byte       = max(0, total - 1) if total > 0 else 0,
                part_file_path = self._part_path(task, 0),
            )
            return [seg]

        segment_size = total // n

        for i in range(n):
            start = i * segment_size
            end   = (start + segment_size - 1) if i < n - 1 else (total - 1)
            segments.append(SegmentInfo(
                segment_index  = i,
                start_byte     = start,
                end_byte       = end,
                part_file_path = self._part_path(task, i),
            ))

        return segments

    def _part_path(self, task: DownloadTask, index: int) -> Path:
        return task.save_path / f"{task.filename}.part{index}"

    def _get_task(self, download_id: int) -> DownloadTask:
        with self._lock:
            task = self._active_tasks.get(download_id)
        if task is None:
            raise KeyError(f"No active task with id={download_id}")
        return task

    def _transition(
        self,
        task:      DownloadTask,
        new_status: DownloadStatus,
        error_msg: str = "",
    ) -> None:
        """Update task status and persist to DB. Thread-safe via task.lock."""
        with task.lock:
            task.status   = new_status
            task.error_msg = error_msg
        self._db.update_download_status(task.db_id, new_status, error_msg or None)
        if self._on_status_change:
            try:
                self._on_status_change(task.db_id, new_status)
            except Exception:
                pass  # UI callbacks must never crash the download thread
