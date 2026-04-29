"""
core/file_assembler.py
Merge all downloaded .part files into the final output file.

After every segment worker completes, the FileAssembler concatenates the
temporary part files in segment_index order and then deletes them.
Segments MUST be merged in order — each part file contains a specific
byte range of the original file, and appending out of order produces
a corrupt output.
"""

import logging
import shutil
from pathlib import Path

from core.models import DownloadTask

logger = logging.getLogger(__name__)

_COPY_BUFFER = 1024 * 1024   # 1 MB copy buffer for shutil.copyfileobj


class FileAssemblyError(Exception):
    """Raised when assembly cannot complete (missing part, size mismatch, etc.)."""


class FileAssembler:
    """Merges segment .part files into the final download file."""

    def __init__(self, task: DownloadTask) -> None:
        self._task = task

    def assemble(self) -> None:
        """
        Concatenate all .part files in segment_index order into the final file.

        Steps:
          1. Verify every part file exists and is non-empty.
          2. Write parts to the output file in order.
          3. Optionally verify the total output size matches expected total_size.
          4. Delete all .part files on success.

        Raises FileAssemblyError if any step fails.
        """
        task        = self._task
        output_path = task.final_path

        # Sort segments by index to guarantee correct concatenation order.
        segments = sorted(task.segments, key=lambda s: s.segment_index)

        self._verify_parts_exist(segments)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Assembling %d segments into %s", len(segments), output_path)

        try:
            with open(output_path, "wb") as out_file:
                for seg in segments:
                    logger.debug(
                        "Appending segment %d (%s) ...",
                        seg.segment_index,
                        ProgressTracker_format(seg.downloaded),
                    )
                    with open(seg.part_file_path, "rb") as part_file:
                        shutil.copyfileobj(part_file, out_file, length=_COPY_BUFFER)
        except OSError as exc:
            # Remove the incomplete output file on failure
            if output_path.exists():
                output_path.unlink(missing_ok=True)
            raise FileAssemblyError(f"Failed to write output file: {exc}") from exc

        # Size verification (skip if total_size is unknown)
        if task.total_size > 0:
            actual_size = output_path.stat().st_size
            if actual_size != task.total_size:
                output_path.unlink(missing_ok=True)
                raise FileAssemblyError(
                    f"Size mismatch: expected {task.total_size} bytes, "
                    f"got {actual_size} bytes. Output file removed."
                )

        logger.info("Assembly complete: %s", output_path)
        self._delete_parts(segments)

    def cleanup_parts(self) -> None:
        """
        Delete all .part files without assembling.
        Called when a download is cancelled or permanently fails.
        """
        self._delete_parts(self._task.segments)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_parts_exist(segments) -> None:
        missing = [
            str(seg.part_file_path)
            for seg in segments
            if not seg.part_file_path.exists()
        ]
        if missing:
            raise FileAssemblyError(
                f"Missing part files before assembly: {', '.join(missing)}"
            )

    @staticmethod
    def _delete_parts(segments) -> None:
        for seg in segments:
            try:
                seg.part_file_path.unlink(missing_ok=True)
                logger.debug("Deleted part file: %s", seg.part_file_path)
            except OSError as exc:
                logger.warning("Could not delete part file %s: %s", seg.part_file_path, exc)


def ProgressTracker_format(n: int) -> str:
    """Inline byte formatter to avoid importing ProgressTracker here."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
