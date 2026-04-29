"""
main.py
Application entry point for SDM — Simple Download Manager.

Initialises all modules in dependency order, restores incomplete downloads
from the previous session, then launches either the Tkinter GUI (default)
or the CLI (--cli flag or headless environment).

Usage:
  python main.py                              # GUI mode
  python main.py --cli --url <URL>            # CLI single download
  python main.py --cli --url <URL> --segments 8 --output ./my_files
  python main.py --cli list                   # print download history
  python main.py --verbose                    # DEBUG logging
"""

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _tkinter_available() -> bool:
    """Return False in headless environments where Tkinter cannot open a display."""
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sdm",
        description="Simple Download Manager — CS404 Distributed Systems Project",
    )
    parser.add_argument("command", nargs="?", default="gui",
                        choices=["gui", "list"],
                        help="'gui' (default) launches the desktop UI; 'list' prints history")
    parser.add_argument("--cli",      action="store_true",
                        help="Force CLI mode even if Tkinter is available")
    parser.add_argument("--url",      type=str,  default=None,
                        help="URL to download (CLI mode)")
    parser.add_argument("--segments", type=int,  default=4,
                        help="Number of parallel segments (default: 4)")
    parser.add_argument("--output",   type=str,  default=None,
                        help="Output directory (default: ./downloads/)")
    parser.add_argument("--db",       type=str,  default="sdm.db",
                        help="Path to the SQLite database file")
    parser.add_argument("--verbose",  action="store_true",
                        help="Enable DEBUG-level logging")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # ---- Logging (must be first) ----
    from utils.logger import setup_logging
    setup_logging(log_level="DEBUG" if args.verbose else "INFO")

    # ---- Database ----
    from persistence.database import Database
    db = Database(args.db)
    db.initialize()

    # ---- Download Manager ----
    from core.download_manager import DownloadManager
    manager = DownloadManager(
        db           = db,
        download_dir = Path(args.output) if args.output else Path("downloads"),
        num_segments = args.segments,
        max_retries  = 3,
    )

    # ---- Restore incomplete downloads from last session ----
    restored = manager.restore_incomplete()
    if restored:
        logger.info("Restored %d incomplete download(s) from previous session.", len(restored))

    # ---- Choose interface ----
    use_cli = args.cli or args.command == "list" or not _tkinter_available()

    if use_cli:
        from ui.cli import CLIInterface
        cli = CLIInterface(manager)

        if args.command == "list":
            cli.run_list()
        elif args.url:
            cli.run_download(
                url      = args.url,
                segments = args.segments,
                output   = args.output,
            )
        else:
            print("CLI mode: specify --url <URL> to download or use 'list' to view history.")
            print("Run with --help for full usage.")
            sys.exit(0)
    else:
        from ui.gui import SDMApplication
        app = SDMApplication(manager)
        app.run()
        db.close()
        # Daemon worker threads blocked in Event.wait() prevent Python's shutdown
        # from completing on Windows. Force-exit now that the UI and DB are closed.
        import os
        os._exit(0)

    db.close()


if __name__ == "__main__":
    main()
