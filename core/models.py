"""
core/models.py
Shared data structures for the entire SDM application.
No business logic lives here — only dataclasses and enums.
Every other module imports from here to avoid circular dependencies.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class DownloadStatus(Enum):
    """Finite-state machine states for a download task."""
    PENDING      = "pending"
    DOWNLOADING  = "downloading"
    PAUSED       = "paused"
    COMPLETED    = "completed"
    FAILED       = "failed"
    CANCELLED    = "cancelled"


class SegmentStatus(Enum):
    """States for an individual byte-range segment."""
    PENDING    = "pending"
    ACTIVE     = "active"
    COMPLETED  = "completed"
    FAILED     = "failed"
    CANCELLED  = "cancelled"


@dataclass
class SegmentInfo:
    """
    Represents one downloadable chunk of a file.

    A file is split into N segments; each segment is assigned to one worker thread.
    The worker downloads bytes [start_byte, end_byte] and writes them to part_file_path.

    Resume invariant: actual_start = start_byte + downloaded
    The part file already contains `downloaded` bytes written from previous sessions.
    """
    segment_index:  int
    start_byte:     int
    end_byte:       int
    part_file_path: Path
    downloaded:     int           = 0
    status:         SegmentStatus = SegmentStatus.PENDING
    db_id:          int           = -1   # Row ID in the `segments` table; -1 until persisted

    @property
    def total_bytes(self) -> int:
        """Total bytes this segment is responsible for downloading."""
        return self.end_byte - self.start_byte + 1

    @property
    def remaining_bytes(self) -> int:
        return self.total_bytes - self.downloaded

    @property
    def is_complete(self) -> bool:
        return self.downloaded >= self.total_bytes


@dataclass
class DownloadTask:
    """
    Full state of one download, shared across all layers.

    Threading controls:
      pause_event  — set=running, clear=paused. Workers call pause_event.wait().
      cancel_event — set=cancelled. Workers check is_set() each chunk and exit.
      lock         — protects status transitions and segment list mutations.

    pause_event is initialised as SET (i.e. not paused) so workers run immediately.
    """
    url:          str
    filename:     str
    save_path:    Path
    total_size:   int
    num_segments: int
    segments:     list[SegmentInfo]    = field(default_factory=list)
    status:       DownloadStatus       = DownloadStatus.PENDING
    db_id:        int                  = -1
    error_msg:    str                  = ""

    # Threading primitives — recreated fresh per download session
    pause_event:  threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock:         threading.Lock  = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        # Green-light by default: workers run unless explicitly paused.
        self.pause_event.set()

    @property
    def final_path(self) -> Path:
        return self.save_path / self.filename

    @property
    def downloaded_bytes(self) -> int:
        return sum(s.downloaded for s in self.segments)

    @property
    def progress_percent(self) -> float:
        if self.total_size <= 0:
            return 0.0
        return min(100.0, self.downloaded_bytes / self.total_size * 100)
