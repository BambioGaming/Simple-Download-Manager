"""
benchmark.py
Performance comparison: 1 vs 2 vs 4 vs 8 parallel download segments.

All tests run through the full DownloadManager stack — the same code path
the GUI uses — so the only variable is the number of simultaneous segment
workers.  Timing starts after the URL probe (matching the moment the user
clicks "Start"), and each run uses its own subdirectory so part files never
collide.

Run with:
    python benchmark.py

Output example (100 MB Hetzner file, ~50 Mbps connection):
  Method                      Time      Speed        Speedup
  ──────────────────────────  ────────  ───────────  ───────
  1 segment (baseline)         18.2s    5.49 MB/s     1.00x
  2 segments                   10.4s    9.62 MB/s     1.75x
  4 segments                    6.1s   16.39 MB/s     2.98x
  8 segments                    4.8s   20.83 MB/s     3.79x
"""

import shutil
import sys
import time
from pathlib import Path

# Hetzner NBG1 speed-test endpoint — supports Range requests, no per-IP cap,
# 100 MB file gives segments enough data to ramp past TCP slow-start.
BENCHMARK_URL = "https://nbg1-speed.hetzner.com/100MB.bin"

# Parent directory for per-run subdirectories (cleaned up after each run)
OUTPUT_DIR = Path("benchmark_output")


# ------------------------------------------------------------------
# Single benchmark run (any segment count)
# ------------------------------------------------------------------

def benchmark_download(url: str, run_dir: Path, num_segments: int) -> tuple[float, int]:
    """
    Download *url* via the full DownloadManager stack using *num_segments*
    parallel workers.

    The URL probe (HEAD request) is done inside add_download() and is NOT
    included in the elapsed time — matching the GUI experience where the
    user waits for the probe once when adding a URL, then clicks Start.

    Returns (elapsed_seconds, file_size_bytes).
    Returns (float('inf'), 0) if the download fails or is cancelled.
    """
    from core.download_manager import DownloadManager
    from persistence.database import Database

    run_dir.mkdir(parents=True, exist_ok=True)

    db = Database(":memory:")
    db.initialize()

    manager = DownloadManager(
        db           = db,
        download_dir = run_dir,
        num_segments = num_segments,
        max_retries  = 2,
    )

    # Probe the URL — resolves filename, file size, and range support.
    # Not timed: equivalent to the user clicking "+ Add URL" in the GUI.
    did = manager.add_download(url, save_path=run_dir, num_segments=num_segments)

    # Read file size before the download begins (tracker not yet created).
    pre_stats  = manager.get_progress(did)
    file_bytes = pre_stats["total"] if pre_stats else 0

    # ---- Start timing here — same as clicking "Start" in the GUI ----
    start = time.monotonic()
    manager.start_download(did)

    while True:
        stats = manager.get_progress(did)
        if stats is None:
            break
        status = stats.get("status", "")
        if status in ("completed", "failed", "cancelled"):
            if status != "completed":
                db.close()
                return float("inf"), file_bytes
            break
        time.sleep(0.2)

    elapsed = time.monotonic() - start
    db.close()
    return elapsed, file_bytes


# ------------------------------------------------------------------
# Main benchmark runner
# ------------------------------------------------------------------

def run_benchmark() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\nSDM Benchmark — {BENCHMARK_URL}")
    print("=" * 60)
    print("All runs use the full DownloadManager stack (same as the GUI).")
    print("Timing excludes the URL probe; only download + assembly is measured.\n")

    configs = [
        (1, "1 segment (baseline)"),
        (2, "2 segments"),
        (4, "4 segments"),
        (8, "8 segments"),
    ]

    results: list[tuple[str, float, int]] = []

    for n_segs, label in configs:
        run_dir = OUTPUT_DIR / f"run_{n_segs}seg"
        print(f"Running {label} …")
        try:
            elapsed, file_bytes = benchmark_download(BENCHMARK_URL, run_dir, n_segs)
            results.append((label, elapsed, file_bytes))
            if elapsed == float("inf"):
                print("  FAILED")
            else:
                print(f"  Done in {elapsed:.1f} s")
        except Exception as exc:
            print(f"  FAILED: {exc}")
            results.append((label, float("inf"), 0))
        finally:
            # Delete the run directory so the next run starts with a clean slate.
            shutil.rmtree(run_dir, ignore_errors=True)

    # ---- Print results table ----
    baseline_time = results[0][1] if results else float("inf")
    # Use file size from any successful run for speed calculation
    file_bytes = next((fb for _, _, fb in results if fb > 0), 0)

    print("\n")
    header = f"{'Method':<26}  {'Time':>8}  {'Speed':>12}  {'Speedup':>7}"
    separator = "─" * len(header)
    print(header)
    print(separator)

    for label, elapsed, _ in results:
        if elapsed in (float("inf"),) or elapsed <= 0:
            print(f"{label:<26}  {'FAILED':>8}  {'':>12}  {'':>7}")
            continue
        speed_str = _fmt_speed(file_bytes / elapsed) if file_bytes > 0 else "N/A"
        speedup   = baseline_time / elapsed if baseline_time not in (float("inf"), 0) else 0.0
        print(f"{label:<26}  {elapsed:>7.1f}s  {speed_str:>12}  {speedup:>6.2f}x")

    print()

    # ---- Cleanup output directory ----
    try:
        OUTPUT_DIR.rmdir()
    except OSError:
        pass
    print("Done.\n")


def _fmt_speed(bps: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.2f} {unit}"
        bps /= 1024
    return f"{bps:.2f} TB/s"


if __name__ == "__main__":
    # Add project root to sys.path so imports work when run directly
    sys.path.insert(0, str(Path(__file__).parent))
    run_benchmark()
