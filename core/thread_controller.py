"""
core/thread_controller.py
ThreadPoolExecutor manager for parallel segment downloads.

Creates one executor per download, submits all segment workers as futures,
and waits for all of them to complete. Uses as_completed() so results are
processed as workers finish (not all at the end), allowing early detection
of failures.

Design note: max_workers is set to the number of segments (one thread per
segment). This is intentional for a download manager — more threads than
segments provide no benefit because each thread is blocked on network I/O,
not competing for CPU.
"""

import logging
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from core.segment_worker import SegmentWorker

logger = logging.getLogger(__name__)


class ThreadController:
    """
    Manages the lifecycle of a ThreadPoolExecutor for one download task.

    Usage:
        controller = ThreadController(max_workers=4)
        controller.start_segments(workers)
        all_ok, failed_indices = controller.wait_for_completion()
        controller.shutdown()
    """

    def __init__(self, max_workers: int = 4) -> None:
        self._max_workers = max_workers
        self._executor: ThreadPoolExecutor | None = None
        # Maps each Future to the SegmentWorker that produced it,
        # so we can identify which segment failed.
        self._futures: dict[Future, SegmentWorker] = {}

    def start_segments(self, workers: list[SegmentWorker]) -> None:
        """
        Create the executor and submit all segment workers.
        Each worker's .run() method is the callable submitted to the pool.
        """
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="sdm-seg",
        )
        self._futures = {
            self._executor.submit(worker.run): worker
            for worker in workers
        }
        logger.info("Submitted %d segment workers to ThreadPoolExecutor.", len(workers))

    def wait_for_completion(self) -> tuple[bool, list[int]]:
        """
        Block until every submitted future finishes.

        Returns:
            (all_succeeded, failed_segment_indices)
            all_succeeded is True only if every segment worker returned True.
        """
        if not self._futures:
            return True, []

        failed_indices: list[int] = []

        # as_completed yields futures in the order they finish, not submission order.
        # This lets us log individual completions progressively.
        for future in as_completed(self._futures):
            worker = self._futures[future]
            seg    = worker._segment

            try:
                success = future.result()
            except Exception as exc:
                # An unhandled exception inside the worker counts as failure.
                logger.exception(
                    "Segment %d raised an unexpected exception: %s",
                    seg.segment_index, exc,
                )
                success = False

            if success:
                logger.debug("Segment %d finished successfully.", seg.segment_index)
            else:
                logger.warning("Segment %d finished with failure.", seg.segment_index)
                failed_indices.append(seg.segment_index)

        all_succeeded = len(failed_indices) == 0
        logger.info(
            "All segments done. succeeded=%s failed=%s",
            len(self._futures) - len(failed_indices),
            len(failed_indices),
        )
        return all_succeeded, failed_indices

    def shutdown(self, wait: bool = True) -> None:
        """Shut down the executor, optionally waiting for in-flight threads."""
        if self._executor:
            self._executor.shutdown(wait=wait)
            self._executor = None
            logger.debug("ThreadPoolExecutor shut down.")

    @property
    def active_count(self) -> int:
        """Return the number of futures that have not yet completed."""
        return sum(1 for f in self._futures if not f.done())
