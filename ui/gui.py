"""
ui/gui.py
Tkinter desktop GUI for SDM — Simple Download Manager.

Layout:
  ┌─────────────────────────────────────────────────────────────┐
  │  Toolbar: [+ Add] [▶ Start] [⏸ Pause] [▶ Resume] [✕ Cancel] │
  ├─────────────────────────────────────────────────────────────┤
  │  Notebook:  [Active Downloads]  [History]                   │
  │                                                             │
  │  Active tab — Treeview with live rows                       │
  │  Progress bar + speed / ETA panel for selected download     │
  │                                                             │
  │  History tab — Treeview of all completed/failed records     │
  │  [Delete Selected]  [Clear All History]                     │
  └─────────────────────────────────────────────────────────────┘
  │  Status bar (bottom)                                        │
  └─────────────────────────────────────────────────────────────┘

Thread safety
-------------
Tkinter is NOT thread-safe.  Worker threads must never touch widgets.
All widget updates happen on the main thread via self.after(500, _poll).
Worker threads only call DownloadManager methods which are themselves
thread-safe via locks.
"""

import logging
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from pathlib import Path
from typing import Optional

from core.download_manager import DownloadManager
from core.models import DownloadStatus
from monitoring.progress_tracker import ProgressTracker

logger = logging.getLogger(__name__)

# Colours for status badges in the Treeview
_STATUS_COLOURS = {
    "pending":     "#888888",
    "downloading": "#1a73e8",
    "paused":      "#f4a900",
    "completed":   "#0f9d58",
    "failed":      "#d93025",
    "cancelled":   "#5f6368",
}

_POLL_INTERVAL_MS = 200   # 5 fps — smooth progress bar like a browser


class SDMApplication(tk.Tk):
    """Main application window."""

    def __init__(self, manager: DownloadManager) -> None:
        super().__init__()
        self._manager  = manager
        self._poll_job: Optional[str] = None   # after() job ID

        # Per-row colour tags are set on the active Treeview
        # Map download_id → Treeview item ID for fast updates
        self._active_item_ids:  dict[int, str] = {}
        self._history_item_ids: dict[int, str] = {}
        # Downloads moved to history; excluded from the poll loop so
        # _refresh_history() isn't called every 500 ms and history selections stick.
        self._moved_to_history: set[int] = set()

        self._build_window()
        self._build_toolbar()
        self._build_notebook()
        self._build_status_bar()

        # Populate history tab with records already in the database
        self._refresh_history()

        # Populate active tab with any restored (paused) downloads
        for did in manager.get_active_task_ids():
            self._ensure_active_row(did)

        # Start the polling loop
        self._schedule_poll()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Window construction
    # ------------------------------------------------------------------

    def _build_window(self) -> None:
        self.title("SDM — Simple Download Manager")
        self.geometry("900x560")
        self.minsize(700, 420)
        self.configure(bg="#f1f3f4")
        try:
            self.iconbitmap(default="")
        except Exception:
            pass

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self, bg="#ffffff", pady=6, padx=8, relief="flat",
                       borderwidth=1)
        bar.pack(side="top", fill="x")

        btn_cfg = {"relief": "flat", "cursor": "hand2", "padx": 12, "pady": 4,
                   "font": ("Segoe UI", 9)}

        tk.Button(bar, text="+ Add URL",  bg="#1a73e8", fg="white",
                  command=self._on_add_url, **btn_cfg).pack(side="left", padx=2)
        self._btn_start = tk.Button(bar, text="▶  Start",   bg="#0f9d58", fg="white",
                  command=self._on_start,   **btn_cfg)
        self._btn_start.pack(side="left", padx=2)
        self._btn_pause = tk.Button(bar, text="⏸  Pause",   bg="#f4a900", fg="white",
                  command=self._on_pause,   **btn_cfg)
        self._btn_pause.pack(side="left", padx=2)
        self._btn_resume = tk.Button(bar, text="▶  Resume",  bg="#1a73e8", fg="white",
                  command=self._on_resume,  **btn_cfg)
        self._btn_resume.pack(side="left", padx=2)
        self._btn_cancel = tk.Button(bar, text="✕  Cancel",  bg="#d93025", fg="white",
                  command=self._on_cancel,  **btn_cfg)
        self._btn_cancel.pack(side="left", padx=2)

    def _build_notebook(self) -> None:
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True, padx=8, pady=(4, 0))

        self._active_frame  = self._build_active_tab()
        self._history_frame = self._build_history_tab()

        self._notebook.add(self._active_frame,  text="  Active Downloads  ")
        self._notebook.add(self._history_frame, text="  History  ")

    def _build_active_tab(self) -> tk.Frame:
        frame = tk.Frame(self._notebook, bg="#f1f3f4")

        # ---- Treeview ----
        cols = ("id", "filename", "size", "downloaded", "progress", "speed", "eta", "status")
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse",
                             height=8)
        self._active_tree = tree

        headings = {
            "id":         ("#",        50),
            "filename":   ("Filename", 220),
            "size":       ("Size",     90),
            "downloaded": ("Downloaded", 100),
            "progress":   ("Progress", 80),
            "speed":      ("Speed",    100),
            "eta":        ("ETA",      70),
            "status":     ("Status",   110),
        }
        for col, (text, width) in headings.items():
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="center" if col != "filename" else "w",
                        stretch=col == "filename")

        scroll_y = ttk.Scrollbar(frame, orient="vertical",   command=tree.yview)
        scroll_x = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)

        tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")

        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        # ---- Status colour tags ----
        for status, colour in _STATUS_COLOURS.items():
            tree.tag_configure(status, foreground=colour)

        # ---- Progress detail panel ----
        detail = tk.Frame(frame, bg="#ffffff", relief="groove", borderwidth=1, pady=6, padx=10)
        detail.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        self._progress_bar = ttk.Progressbar(detail, orient="horizontal",
                                              length=100, mode="determinate")
        self._progress_bar.pack(fill="x", padx=4, pady=(0, 4))

        info_row = tk.Frame(detail, bg="#ffffff")
        info_row.pack(fill="x")

        lbl_cfg = {"bg": "#ffffff", "font": ("Segoe UI", 9)}
        self._lbl_speed   = tk.Label(info_row, text="Speed: --",        **lbl_cfg)
        self._lbl_eta     = tk.Label(info_row, text="ETA: --",          **lbl_cfg)
        self._lbl_bytes   = tk.Label(info_row, text="0 B / ?",          **lbl_cfg)
        self._lbl_pct     = tk.Label(info_row, text="0.0%",             **{**lbl_cfg, "font": ("Segoe UI", 9, "bold")})

        self._lbl_speed.pack(side="left",  padx=8)
        self._lbl_eta.pack(  side="left",  padx=8)
        self._lbl_bytes.pack(side="left",  padx=8)
        self._lbl_pct.pack(  side="right", padx=8)

        tree.bind("<<TreeviewSelect>>", self._on_active_select)
        return frame

    def _build_history_tab(self) -> tk.Frame:
        frame = tk.Frame(self._notebook, bg="#f1f3f4")

        cols = ("id", "filename", "size", "status", "created", "completed")
        tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse",
                             height=12)
        self._history_tree = tree

        headings = {
            "id":        ("#",          50),
            "filename":  ("Filename",  250),
            "size":      ("Size",       90),
            "status":    ("Status",    110),
            "created":   ("Started",   150),
            "completed": ("Completed", 150),
        }
        for col, (text, width) in headings.items():
            tree.heading(col, text=text, command=lambda c=col: self._sort_history(c))
            tree.column(col, width=width, anchor="center" if col != "filename" else "w",
                        stretch=col == "filename")

        for status, colour in _STATUS_COLOURS.items():
            tree.tag_configure(status, foreground=colour)

        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)

        tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        btn_frame = tk.Frame(frame, bg="#f1f3f4", pady=4)
        btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew")

        tk.Button(btn_frame, text="Delete Selected", command=self._on_delete_history,
                  relief="flat", bg="#d93025", fg="white", padx=8, pady=3,
                  font=("Segoe UI", 9)).pack(side="left", padx=4)
        tk.Button(btn_frame, text="Clear All", command=self._on_clear_history,
                  relief="flat", bg="#5f6368", fg="white", padx=8, pady=3,
                  font=("Segoe UI", 9)).pack(side="left", padx=4)
        tk.Button(btn_frame, text="↻  Refresh", command=self._refresh_history,
                  relief="flat", bg="#1a73e8", fg="white", padx=8, pady=3,
                  font=("Segoe UI", 9)).pack(side="right", padx=4)

        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return frame

    def _build_status_bar(self) -> None:
        bar = tk.Frame(self, bg="#e8eaed", pady=3, padx=8)
        bar.pack(side="bottom", fill="x")
        self._status_label = tk.Label(bar, text="Ready", bg="#e8eaed",
                                       font=("Segoe UI", 8), anchor="w")
        self._status_label.pack(side="left")

    # ------------------------------------------------------------------
    # Toolbar callbacks
    # ------------------------------------------------------------------

    def _on_add_url(self) -> None:
        url = simpledialog.askstring(
            "Add Download",
            "Enter URL to download:",
            parent=self,
        )
        if not url or not url.strip():
            return
        url = url.strip()

        seg_str = simpledialog.askstring(
            "Segments",
            "Number of parallel segments (default: 4):",
            parent=self,
            initialvalue="4",
        )
        try:
            segments = int(seg_str) if seg_str else 4
            segments = max(1, min(16, segments))
        except (ValueError, TypeError):
            segments = 4

        self._status("Adding download …")
        try:
            did = self._manager.add_download(url, num_segments=segments)
        except Exception as exc:
            messagebox.showerror("Error", f"Could not add download:\n{exc}", parent=self)
            self._status("Error adding download.")
            return

        self._ensure_active_row(did)
        self._manager.start_download(did)
        self._status(f"Download {did} started.")
        self._notebook.select(0)   # switch to active tab

    def _on_start(self) -> None:
        did = self._selected_active_id()
        if did is None:
            return
        try:
            self._manager.start_download(did)
            self._status(f"Download {did} started.")
        except Exception as exc:
            messagebox.showerror("Error", str(exc), parent=self)

    def _on_pause(self) -> None:
        did = self._selected_active_id()
        if did is None:
            return
        self._manager.pause_download(did)
        self._status(f"Download {did} paused.")

    def _on_resume(self) -> None:
        did = self._selected_active_id()
        if did is None:
            return
        self._manager.resume_download(did)
        self._status(f"Download {did} resumed.")

    def _on_cancel(self) -> None:
        did = self._selected_active_id()
        if did is None:
            return
        if not messagebox.askyesno("Cancel Download",
                                    "Cancel this download and delete part files?",
                                    parent=self):
            return
        self._manager.cancel_download(did)
        self._status(f"Download {did} cancelled.")

    def _on_active_select(self, _event=None) -> None:
        """Update the progress panel and toolbar when user selects a different row."""
        did = self._selected_active_id()
        self._update_toolbar_state()
        if did is None:
            return
        stats = self._manager.get_progress(did)
        if stats:
            self._update_detail_panel(stats)

    def _on_delete_history(self) -> None:
        sel = self._history_tree.selection()
        if not sel:
            return
        did = int(self._history_tree.item(sel[0], "values")[0])
        self._manager.delete_download_record(did)
        self._moved_to_history.discard(did)
        self._refresh_history()

    def _on_clear_history(self) -> None:
        if not messagebox.askyesno("Clear History",
                                    "Delete all completed/failed/cancelled records?",
                                    parent=self):
            return
        for record in self._manager.get_all_downloads():
            if record["status"] in ("completed", "failed", "cancelled"):
                self._manager.delete_download_record(record["id"])
                self._moved_to_history.discard(record["id"])
        self._refresh_history()

    def _on_close(self) -> None:
        """Prompt if downloads are in progress before closing."""
        active_statuses = {
            did: self._manager.get_progress(did)
            for did in self._manager.get_active_task_ids()
        }
        in_progress = [
            did for did, s in active_statuses.items()
            if s and s.get("status") == "downloading"
        ]
        if in_progress:
            if not messagebox.askyesno(
                "Active Downloads",
                f"{len(in_progress)} download(s) are active.\n"
                "They will be paused and can be resumed next time.\n\n"
                "Close anyway?",
                parent=self,
            ):
                return
            for did in in_progress:
                self._manager.pause_download(did)

        if self._poll_job:
            self.after_cancel(self._poll_job)
        self.destroy()

    # ------------------------------------------------------------------
    # Polling loop (main thread, every 500 ms)
    # ------------------------------------------------------------------

    def _schedule_poll(self) -> None:
        self._poll_job = self.after(_POLL_INTERVAL_MS, self._poll_progress)

    def _poll_progress(self) -> None:
        """
        Called every 500 ms by Tkinter's event loop.
        Updates all active-download Treeview rows with fresh progress data.
        Tkinter is not thread-safe — this is the ONLY place widgets are updated.
        """
        task_ids = self._manager.get_active_task_ids()
        downloading_count = 0

        for did in task_ids:
            # Once moved to history, never re-process in the poll loop.
            # Without this guard _ensure_active_row re-adds the row every cycle
            # and _refresh_history() fires every 500 ms, destroying any selection.
            if did in self._moved_to_history:
                continue

            stats = self._manager.get_progress(did)
            if stats is None:
                continue

            status = stats.get("status", "pending")
            if status == "downloading":
                downloading_count += 1

            self._ensure_active_row(did)
            self._update_active_row(did, stats)

            if status in ("completed", "failed", "cancelled"):
                self._on_terminal_status(did, stats)

        # Update the detail panel and toolbar for the currently selected row
        sel_id = self._selected_active_id()
        if sel_id is not None:
            s = self._manager.get_progress(sel_id)
            if s:
                self._update_detail_panel(s)
        self._update_toolbar_state()

        # Global status bar
        if downloading_count > 0:
            self._status(f"Downloading {downloading_count} file(s) …")
        elif task_ids:
            self._status("Ready")
        else:
            self._status("Ready — no active downloads")

        # Reschedule
        self._schedule_poll()

    def _on_terminal_status(self, did: int, stats: dict) -> None:
        """Move a finished download out of the active tab and into history (once)."""
        self._moved_to_history.add(did)
        iid = self._active_item_ids.pop(did, None)
        if iid and self._active_tree.exists(iid):
            if iid in self._active_tree.selection():
                self._reset_detail_panel()
            self._active_tree.delete(iid)
        self._refresh_history()

    def _reset_detail_panel(self) -> None:
        self._progress_bar["value"] = 0
        self._lbl_speed["text"] = "Speed: --"
        self._lbl_eta["text"]   = "ETA: --"
        self._lbl_pct["text"]   = "0.0%"
        self._lbl_bytes["text"] = "0 B / ?"

    # ------------------------------------------------------------------
    # Treeview row helpers
    # ------------------------------------------------------------------

    def _ensure_active_row(self, did: int) -> None:
        """Insert a placeholder row if this download has no row yet."""
        if did not in self._active_item_ids:
            iid = self._active_tree.insert(
                "", "end",
                values=(did, "Loading …", "", "", "0%", "--", "--", "pending"),
                tags=("pending",),
            )
            self._active_item_ids[did] = iid

    def _update_active_row(self, did: int, stats: dict) -> None:
        iid = self._active_item_ids.get(did)
        if iid is None or not self._active_tree.exists(iid):
            return

        pct      = stats.get("percent", 0.0)
        speed    = stats.get("speed_human", "-- B/s")
        eta      = stats.get("eta_human", "--:--")
        status   = stats.get("status", "pending")
        filename = stats.get("filename", "")
        total    = ProgressTracker.format_bytes(stats.get("total", 0))
        done     = ProgressTracker.format_bytes(stats.get("downloaded", 0))
        bar      = self._ascii_bar(pct, width=10)

        self._active_tree.item(
            iid,
            values=(did, filename, total, done, f"{bar} {pct:.0f}%", speed, eta, status),
            tags=(status,),
        )

    def _update_detail_panel(self, stats: dict) -> None:
        pct    = stats.get("percent", 0.0)
        status = stats.get("status", "")
        self._progress_bar["value"] = pct
        if status == "paused":
            self._lbl_speed["text"] = "Speed: 0 B/s"
            self._lbl_eta["text"]   = "ETA: --"
        else:
            self._lbl_speed["text"] = f"Speed: {stats.get('speed_human', '--')}"
            self._lbl_eta["text"]   = f"ETA: {stats.get('eta_human', '--')}"
        self._lbl_pct["text"]    = f"{pct:.1f}%"
        done  = ProgressTracker.format_bytes(stats.get("downloaded", 0))
        total = ProgressTracker.format_bytes(stats.get("total", 0)) if stats.get("total") else "?"
        self._lbl_bytes["text"]  = f"{done} / {total}"

    # ------------------------------------------------------------------
    # History tab helpers
    # ------------------------------------------------------------------

    def _refresh_history(self) -> None:
        """Clear and repopulate the history Treeview from the database."""
        for iid in self._history_tree.get_children():
            self._history_tree.delete(iid)
        self._history_item_ids.clear()

        for rec in self._manager.get_all_downloads():
            size      = ProgressTracker.format_bytes(rec["total_size"]) if rec["total_size"] else "?"
            created   = (rec.get("created_at")   or "")[:16]
            completed = (rec.get("completed_at") or "")[:16]
            status    = rec["status"]

            iid = self._history_tree.insert(
                "", "end",
                values=(rec["id"], rec["filename"], size, status, created, completed),
                tags=(status,),
            )
            self._history_item_ids[rec["id"]] = iid

    def _sort_history(self, col: str) -> None:
        """Sort history Treeview by column (toggle asc/desc)."""
        items = [
            (self._history_tree.set(iid, col), iid)
            for iid in self._history_tree.get_children()
        ]
        items.sort(reverse=getattr(self, f"_sort_{col}_desc", False))
        setattr(self, f"_sort_{col}_desc", not getattr(self, f"_sort_{col}_desc", False))
        for idx, (_, iid) in enumerate(items):
            self._history_tree.move(iid, "", idx)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _update_toolbar_state(self) -> None:
        """Enable/disable toolbar buttons based on the selected download's status."""
        did = self._selected_active_id()
        if did is None:
            self._btn_start["state"]  = "normal"
            self._btn_pause["state"]  = "disabled"
            self._btn_resume["state"] = "disabled"
            self._btn_cancel["state"] = "disabled"
            return
        stats  = self._manager.get_progress(did)
        status = stats.get("status", "pending") if stats else "pending"
        self._btn_start["state"]  = "normal"   if status == "pending"                      else "disabled"
        self._btn_pause["state"]  = "normal"   if status == "downloading"                  else "disabled"
        self._btn_resume["state"] = "normal"   if status == "paused"                       else "disabled"
        self._btn_cancel["state"] = "normal"   if status in ("downloading", "paused")      else "disabled"

    def _selected_active_id(self) -> Optional[int]:
        sel = self._active_tree.selection()
        if not sel:
            return None
        vals = self._active_tree.item(sel[0], "values")
        try:
            return int(vals[0])
        except (IndexError, ValueError):
            return None

    def _status(self, msg: str) -> None:
        self._status_label["text"] = msg

    @staticmethod
    def _ascii_bar(pct: float, width: int = 10) -> str:
        filled = int(width * pct / 100)
        return "[" + "=" * filled + " " * (width - filled) + "]"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the Tkinter event loop (blocks until window is closed)."""
        self.mainloop()
