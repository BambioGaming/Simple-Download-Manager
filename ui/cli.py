"""
ui/cli.py
Command-line interface for SDM — a fallback when Tkinter is unavailable
(e.g. headless servers, SSH sessions, automated tests) or when the user
explicitly passes --cli.

Provides:
  sdm download <url> [--segments N] [--output DIR]
  sdm list
"""

import logging
import sys
import time

from core.download_manager import DownloadManager
from core.models import DownloadStatus
from monitoring.progress_tracker import ProgressTracker

logger = logging.getLogger(__name__)

# ANSI escape to overwrite the current line (progress bar animation)
_CR = "\r"
_BAR_WIDTH = 30


class CLIInterface:
    """Simple text-mode frontend for DownloadManager."""

    def __init__(self, manager: DownloadManager) -> None:
        self._manager = manager

    # ------------------------------------------------------------------
    # Entry point — called from main.py with parsed args
    # ------------------------------------------------------------------

    def run_download(
        self,
        url:      str,
        segments: int  = 4,
        output:   str | None = None,
    ) -> None:
        """Download a URL, printing a live progress line to stdout."""
        from pathlib import Path
        save_path = Path(output) if output else None

        print(f"Adding download: {url}")
        try:
            download_id = self._manager.add_download(url, save_path=save_path, num_segments=segments)
        except Exception as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"Starting download (id={download_id}) with {segments} segments …\n")
        self._manager.start_download(download_id)

        # Poll until the download reaches a terminal state
        try:
            self._poll_until_done(download_id)
        except KeyboardInterrupt:
            print("\nInterrupted — pausing download.")
            self._manager.pause_download(download_id)

    def run_list(self) -> None:
        """Print all download records from history."""
        records = self._manager.get_all_downloads()
        if not records:
            print("No downloads in history.")
            return

        header = f"{'ID':>4}  {'Filename':<30}  {'Size':>10}  {'Status':<12}  Created"
        print(header)
        print("-" * len(header))
        for r in records:
            size = ProgressTracker.format_bytes(r["total_size"]) if r["total_size"] else "unknown"
            print(
                f"{r['id']:>4}  {r['filename']:<30}  {size:>10}  "
                f"{r['status']:<12}  {r['created_at']}"
            )

    # ------------------------------------------------------------------
    # Progress polling
    # ------------------------------------------------------------------

    def _poll_until_done(self, download_id: int) -> None:
        """Block, printing a progress line every 0.5 s until done."""
        terminal_states = {
            DownloadStatus.COMPLETED.value,
            DownloadStatus.FAILED.value,
            DownloadStatus.CANCELLED.value,
        }

        while True:
            stats = self._manager.get_progress(download_id)
            if stats is None:
                break

            self._print_progress(stats)

            if stats.get("status") in terminal_states:
                print()   # final newline after the \r progress line
                status = stats["status"]
                if status == DownloadStatus.COMPLETED.value:
                    print(f"\nDownload complete!")
                elif status == DownloadStatus.FAILED.value:
                    print(f"\nDownload FAILED.", file=sys.stderr)
                else:
                    print(f"\nDownload {status}.")
                break

            time.sleep(0.5)

    @staticmethod
    def _print_progress(stats: dict) -> None:
        """
        Print a single-line progress bar using carriage return so it
        overwrites the previous line in the terminal.

        Example:
          [=================>          ] 62%  1.23 MB/s  ETA 0:02:15  75.0 MB / 120.0 MB
        """
        pct      = stats.get("percent", 0.0)
        speed    = stats.get("speed_human", "-- B/s")
        eta      = stats.get("eta_human", "--:--")
        done     = ProgressTracker.format_bytes(stats.get("downloaded", 0))
        total    = ProgressTracker.format_bytes(stats.get("total", 0)) if stats.get("total") else "?"
        status   = stats.get("status", "")

        filled   = int(_BAR_WIDTH * pct / 100)
        bar      = "=" * filled + (">" if filled < _BAR_WIDTH else "") + " " * (_BAR_WIDTH - filled - 1)

        line = (
            f"{_CR}[{bar}] {pct:5.1f}%  {speed:>12}  ETA {eta}  "
            f"{done} / {total}  [{status}]"
        )
        sys.stdout.write(line)
        sys.stdout.flush()
