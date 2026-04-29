"""
monitoring/progress_tracker.py
Real-time download metrics: speed, ETA, and progress percentage.

Speed algorithm (FDM / browser style)
--------------------------------------
1. Workers call update() after each chunk; each call stores
   (timestamp, cumulative_session_bytes) in a deque.
2. Old samples beyond SPEED_WINDOW_S seconds are trimmed on every
   update(), so the window always covers the most recent N seconds
   regardless of download speed.
3. Raw speed = (newest_bytes - oldest_bytes) / (newest_t - oldest_t)
   over that N-second window.
4. An Exponential Weighted Moving Average (EWMA) is applied each time
   get_stats() is called, producing a smooth display value that
   reacts to real speed changes without jumping on every packet.
"""

import threading
import time
from collections import deque

_SPEED_WINDOW_S = 5.0   # seconds of history kept for raw speed
_EWMA_ALPHA     = 0.15  # blending factor: 0 = never changes, 1 = raw


class ProgressTracker:
    """
    Thread-safe metrics aggregator for a single download.

    Workers call update(chunk_size) after each chunk write.
    The UI calls get_stats() on the main thread (typically every 200 ms).
    """

    def __init__(self, total_size: int, initial_downloaded: int = 0) -> None:
        self._total_size         = total_size
        self._downloaded         = initial_downloaded
        self._initial_downloaded = initial_downloaded
        self._lock               = threading.Lock()
        self._start_time         = time.monotonic()
        # (timestamp, session_bytes) — 0-based bytes downloaded this session.
        # No maxlen: size is bounded by trimming in update().
        self._samples: deque[tuple[float, int]] = deque()
        # EWMA state — only touched by the UI thread (get_stats), no lock needed.
        self._smoothed_speed: float = 0.0

    # ------------------------------------------------------------------
    # Called by worker threads (hot path)
    # ------------------------------------------------------------------

    def update(self, chunk_size: int) -> None:
        """Record chunk_size new bytes. Thread-safe."""
        with self._lock:
            self._downloaded += chunk_size
            now          = time.monotonic()
            session_bytes = self._downloaded - self._initial_downloaded
            self._samples.append((now, session_bytes))
            # Drop samples that have fallen outside the speed window.
            cutoff = now - _SPEED_WINDOW_S
            while len(self._samples) > 2 and self._samples[0][0] < cutoff:
                self._samples.popleft()

    # ------------------------------------------------------------------
    # Called by the UI thread
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """
        Return a snapshot of all metrics. Safe to call from any thread.

        Keys: downloaded, total, percent, speed_bps, speed_human,
              eta_seconds, eta_human, elapsed
        """
        with self._lock:
            downloaded = self._downloaded
            samples    = list(self._samples)

        elapsed = time.monotonic() - self._start_time
        percent = (
            min(100.0, downloaded / self._total_size * 100)
            if self._total_size > 0 else 0.0
        )

        raw_speed = self._calc_speed(samples)

        # EWMA smoothing: blend the new raw measurement into the running average.
        if raw_speed > 0:
            if self._smoothed_speed == 0.0:
                self._smoothed_speed = raw_speed          # cold start
            else:
                self._smoothed_speed = (
                    _EWMA_ALPHA * raw_speed
                    + (1.0 - _EWMA_ALPHA) * self._smoothed_speed
                )
        # If raw_speed == 0 (stall / no data yet) keep last smoothed value so
        # the display doesn't snap to 0 on a brief network hiccup.

        speed_bps = self._smoothed_speed
        if speed_bps > 0 and self._total_size > 0:
            remaining   = max(0, self._total_size - downloaded)
            eta_seconds = remaining / speed_bps
        else:
            eta_seconds = -1.0

        return {
            "downloaded":  downloaded,
            "total":       self._total_size,
            "percent":     percent,
            "speed_bps":   speed_bps,
            "speed_human": self._format_speed(speed_bps),
            "eta_seconds": eta_seconds,
            "eta_human":   self._format_eta(eta_seconds),
            "elapsed":     elapsed,
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_speed(samples: list[tuple[float, int]]) -> float:
        """Average bytes/second over the stored time window."""
        if len(samples) < 2:
            return 0.0
        t_old, b_old = samples[0]
        t_new, b_new = samples[-1]
        dt = t_new - t_old
        if dt <= 0.0:
            return 0.0
        return (b_new - b_old) / dt

    @staticmethod
    def _format_speed(bps: float) -> str:
        if bps <= 0:
            return "-- B/s"
        for unit in ("B", "KB", "MB", "GB"):
            if bps < 1024:
                return f"{bps:.1f} {unit}/s"
            bps /= 1024
        return f"{bps:.1f} TB/s"

    @staticmethod
    def _format_eta(seconds: float) -> str:
        if seconds < 0:
            return "--:--"
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, s   = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"

    @staticmethod
    def format_bytes(n: int) -> str:
        """Human-readable byte count, e.g. '1.23 MB'."""
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} PB"
