"""
WU Scraper - Weather Underground PWS to CSV (desktop GUI).

Run with pythonw.exe (double-click the .pyw) for a console-free window.
One-time setup on a new machine:
    pip install -r requirements.txt
    playwright install chromium
"""

import os
import queue
import sys
import threading
import traceback
import webbrowser
from datetime import date, timedelta
from tkinter import Tk, StringVar, filedialog, messagebox
from tkinter import ttk

# When frozen (PyInstaller), Playwright's browser binary is bundled next to
# the exe rather than in the normal pip-managed ms-playwright cache - point
# it there before importing wu_scraper, which imports playwright.sync_api.
if getattr(sys, "frozen", False):
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        os.path.join(os.path.dirname(sys.executable), "ms-playwright"),
    )

from tkcalendar import DateEntry

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wu_scraper

APP_DIR = os.path.dirname(os.path.abspath(__file__))

WUNDERMAP_URL = "https://www.wunderground.com/wundermap"


class App(Tk):
    def __init__(self):
        super().__init__()
        self.title("WUnder CSV")
        self.resizable(False, False)
        try:
            self.iconbitmap(os.path.join(APP_DIR, "wu.ico"))
        except Exception:
            pass

        self.report_callback_exception = self._on_tk_exception

        self._queue = queue.Queue()
        self._worker = None
        self._cancel_event = None

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        outer = ttk.Frame(self, padding=16)
        outer.grid(sticky="nsew")

        ttk.Label(
            outer, text="Weather Underground PWS -> CSV", font=("", 11, "bold")
        ).grid(row=0, column=0, sticky="w", padx=10)
        ttk.Label(
            outer, text="Export table data from any PWS station by date range."
        ).grid(row=1, column=0, sticky="w", padx=10, pady=(0, 10))

        frm = ttk.Frame(outer)
        frm.grid(row=2, column=0, sticky="nsew")
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Station ID").grid(row=0, column=0, sticky="w", **pad)
        self.station_var = StringVar()
        ttk.Entry(frm, textvariable=self.station_var, width=32).grid(row=0, column=1, sticky="ew", **pad)
        ttk.Label(
            frm,
            text="From the station URL: wunderground.com/dashboard/pws/IGOLDC915",
            foreground="#666666",
            font=("", 8),
        ).grid(row=1, column=1, sticky="w", padx=10)

        ttk.Label(frm, text="Start date").grid(row=2, column=0, sticky="w", **pad)
        self.start_picker = DateEntry(
            frm, width=16, date_pattern="dd/mm/yyyy",
            date=date.today() - timedelta(days=7),
        )
        self.start_picker.grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(frm, text="End date").grid(row=3, column=0, sticky="w", **pad)
        self.end_picker = DateEntry(
            frm, width=16, date_pattern="dd/mm/yyyy", date=date.today(),
        )
        self.end_picker.grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Units").grid(row=4, column=0, sticky="w", **pad)
        self.units_var = StringVar(value="metric")
        ttk.Combobox(
            frm, textvariable=self.units_var, values=["metric", "english"],
            state="readonly", width=29,
        ).grid(row=4, column=1, sticky="ew", **pad)

        ttk.Label(frm, text="Time resolution").grid(row=5, column=0, sticky="w", **pad)
        self.res_var = StringVar(value="original")
        ttk.Combobox(
            frm, textvariable=self.res_var,
            values=["original", "15", "30", "60"],
            state="readonly", width=29,
        ).grid(row=5, column=1, sticky="ew", **pad)

        btn_frame = ttk.Frame(frm)
        btn_frame.grid(row=6, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 6))
        self.run_btn = ttk.Button(btn_frame, text="Download CSV", command=self._on_submit)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btn_frame, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(frm, mode="determinate", length=360, maximum=1)
        self.progress.grid(row=7, column=0, columnspan=2, sticky="ew", **pad)

        self.status_var = StringVar(value="")
        ttk.Label(frm, textvariable=self.status_var).grid(row=8, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 6))

        self._build_help(outer, row=3)

    def _build_help(self, parent, row):
        help_frame = ttk.Labelframe(parent, text="How to use", padding=12)
        help_frame.grid(row=row, column=0, sticky="ew", padx=10, pady=(10, 0))
        wrap = 380

        ttk.Label(
            help_frame,
            text="1. Find your station at Weather Underground. The Station ID is "
                 "shown when you click on a station (e.g. IGOLDC915).",
            wraplength=wrap, justify="left",
        ).pack(anchor="w", pady=(0, 4))

        link = ttk.Label(help_frame, text="wunderground.com/wundermap", foreground="#1a5fb4", cursor="hand2")
        link.pack(anchor="w", padx=(14, 0), pady=(0, 8))
        link.bind("<Button-1>", lambda e: webbrowser.open(WUNDERMAP_URL))

        ttk.Label(
            help_frame,
            text="2. Enter Station ID and the start/end dates for the range you want.",
            wraplength=wrap, justify="left",
        ).pack(anchor="w", pady=(0, 4))

        ttk.Label(
            help_frame,
            text='3. Click Download CSV. The file contains the same 5-minute table '
                 'data as the "Table" view on the dashboard, with units in the '
                 "header row only.",
            wraplength=wrap, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            help_frame,
            text="Note: Data is scraped from the website. For long ranges, the "
                 "request can take a few minutes.",
            wraplength=wrap, justify="left", font=("", 8), foreground="#666666",
        ).pack(anchor="w", pady=(0, 8))

        ttk.Label(
            help_frame,
            text="Uses data from Weather Underground. No API key required; data "
                 "is read from the public dashboard.",
            wraplength=wrap, justify="left", font=("", 8), foreground="#666666",
        ).pack(anchor="w")

    # --- submit / worker thread -----------------------------------------

    def _on_submit(self):
        station_id = self.station_var.get().strip()
        units = self.units_var.get()
        resolution = self.res_var.get()

        if not station_id:
            messagebox.showerror("Missing input", "Please fill in Station ID.")
            return
        try:
            start_d = self.start_picker.get_date()
            end_d = self.end_picker.get_date()
        except ValueError:
            messagebox.showerror("Invalid date", "Please enter valid start/end dates.")
            return
        if start_d > end_d:
            messagebox.showerror("Invalid range", "Start date must be on or before end date.")
            return

        start = start_d.isoformat()
        end = end_d.isoformat()

        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress.config(value=0, maximum=1)
        self.status_var.set("Starting...")

        self._cancel_event = threading.Event()
        self._worker = threading.Thread(
            target=self._run_worker,
            args=(station_id, start, end, units, resolution, self._cancel_event),
            daemon=True,
        )
        self._worker.start()
        self.after(100, self._poll_queue)

    def _run_worker(self, station_id, start, end, units, resolution, cancel_event):
        def progress_cb(current, total, day, status):
            self._queue.put(("progress", current, total, day, status))

        try:
            csv_text, filename = wu_scraper.run_scrape(
                station_id, start, end,
                units=units, resolution=resolution,
                progress_callback=progress_cb,
                should_cancel=cancel_event.is_set,
            )
            self._queue.put(("done", csv_text, filename))
        except wu_scraper.ScrapeCancelled:
            self._queue.put(("cancelled",))
        except Exception as e:
            self._queue.put(("error", str(e)))

    def _poll_queue(self):
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "progress":
                    _, current, total, day, status = item
                    self.progress.config(maximum=max(total, 1), value=current)
                    label = f"{current}/{total} days"
                    if day:
                        label += f" - {day}"
                    if status and status not in ("ok",):
                        label += f" ({status})"
                    self.status_var.set(label)
                elif kind == "done":
                    self._finish(item[1], item[2])
                elif kind == "cancelled":
                    self.status_var.set("Cancelled.")
                    self._reset_buttons()
                elif kind == "error":
                    messagebox.showerror("Scrape failed", item[1])
                    self.status_var.set("Failed.")
                    self._reset_buttons()
        except queue.Empty:
            pass
        if self._worker and self._worker.is_alive():
            self.after(100, self._poll_queue)

    def _finish(self, csv_text, filename):
        self._reset_buttons()
        self.status_var.set("Choose where to save...")
        path = filedialog.asksaveasfilename(
            initialfile=filename,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if path:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(csv_text)
            self.status_var.set(f"Saved to {path}")
        else:
            self.status_var.set("Done (not saved).")

    def _reset_buttons(self):
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")

    def _on_cancel(self):
        if self._cancel_event:
            self._cancel_event.set()
        self.status_var.set("Cancelling...")

    def _on_tk_exception(self, exc, val, tb):
        # Without a console (.pyw via pythonw.exe), uncaught callback errors
        # would otherwise vanish silently.
        messagebox.showerror("Unexpected error", "".join(traceback.format_exception(exc, val, tb)))


if __name__ == "__main__":
    App().mainloop()
