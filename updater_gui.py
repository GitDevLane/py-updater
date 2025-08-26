#!/usr/bin/env python3
"""
Tkinter GUI wrapper for updater.py

- Works standalone:  python updater_gui.py --repo ... --app-name ...
- Or import show_updater_window(...) from your Tk app to open as a Toplevel.
"""

import os
import sys
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

DEFAULT_ASSET_PATTERN = "{app}-{os}-{arch}.zip"

def _center_on_screen(window, w=640, h=420):
    window.update_idletasks()
    sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
    x, y = (sw - w) // 2, (sh - h) // 2
    window.geometry(f"{w}x{h}+{x}+{y}")

def _python_exe():
    return sys.executable or "python"

class UpdaterUI:
    def __init__(self, master, repo, app_name,
                 restart_cmd=None, app_dir=None, version_file=None,
                 asset_pattern=DEFAULT_ASSET_PATTERN, include_prereleases=False):
        self.master = master
        self.repo = repo
        self.app_name = app_name
        self.restart_cmd = restart_cmd
        self.app_dir = app_dir
        self.version_file = version_file
        self.asset_pattern = asset_pattern
        self.include_prereleases = include_prereleases

        self.proc = None
        self.reader_thread = None
        self.stdout_q = queue.Queue()
        self.running = False

        self._build_ui()
        self._pump_stdout()

    def _build_ui(self):
        self.master.title("Updater")
        self.master.minsize(520, 320)
        _center_on_screen(self.master)

        top = ttk.Frame(self.master, padding=12)
        top.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", anchor="w")

        btns = ttk.Frame(top)
        btns.pack(side="right", anchor="e")
        self.btn_check = ttk.Button(btns, text="Check for updates", command=self._on_check)
        self.btn_check.pack(side="left", padx=(0, 6))
        self.btn_install = ttk.Button(btns, text="Install update", command=self._on_install)
        self.btn_install.pack(side="left", padx=(0, 6))
        self.btn_cancel = ttk.Button(btns, text="Cancel", command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left")

        pf = ttk.Frame(self.master, padding=(12, 0, 12, 6))
        pf.pack(fill="x")
        self.pb = ttk.Progressbar(pf, mode="indeterminate")
        self.pb.pack(fill="x")

        logf = ttk.Frame(self.master, padding=12)
        logf.pack(fill="both", expand=True)
        self.log = tk.Text(logf, height=12, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        yscroll = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        yscroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=yscroll.set)

        bf = ttk.Frame(self.master, padding=(12, 0, 12, 12))
        bf.pack(fill="x")
        self.btn_close = ttk.Button(bf, text="Close", command=self._on_close)
        self.btn_close.pack(side="right")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool):
        self.btn_check.configure(state="disabled" if busy else "normal")
        self.btn_install.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self.btn_close.configure(state="disabled" if busy else "normal")

    def _cmd(self, dry_run=False):
        updater_py = os.path.join(os.path.dirname(__file__), "updater.py")
        if not os.path.isfile(updater_py):
            messagebox.showerror("Missing updater.py", f"Not found:\n{updater_py}")
            return None
        cmd = [
            _python_exe(), updater_py,
            "--repo", self.repo,
            "--app-name", self.app_name,
            "--asset-pattern", self.asset_pattern,
            "--timeout", "120",
        ]
        if self.include_prereleases:
            cmd.append("--include-prereleases")
        if self.restart_cmd:
            cmd += ["--restart-cmd", self.restart_cmd]
        if self.app_dir:
            cmd += ["--app-dir", self.app_dir]
        if self.version_file:
            cmd += ["--version-file", self.version_file]
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _run_subprocess(self, cmd):
        env = os.environ.copy()  # carries GH_TOKEN if set
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                env=env,
            )
        except Exception as e:
            self.stdout_q.put(f"[gui] failed to start: {e}\n")
            self.running = False
            return

        def _reader():
            for line in self.proc.stdout:
                self.stdout_q.put(line)
            rc = self.proc.wait()
            self.stdout_q.put(f"[gui] updater exited with code {rc}\n")
            self.running = False

        self.reader_thread = threading.Thread(target=_reader, daemon=True)
        self.reader_thread.start()

    def _pump_stdout(self):
        try:
            while True:
                line = self.stdout_q.get_nowait()
                self._append_log(line)
        except queue.Empty:
            pass
        finally:
            if self.running:
                self.master.after(60, self._pump_stdout)
            else:
                self.pb.stop()
                self._set_busy(False)
                self.status_var.set("Idle.")
        self.master.after(200, self._pump_stdout)  # keep UI responsive

    def _on_check(self):
        cmd = self._cmd(dry_run=True)
        if not cmd: return
        self._append_log("\n[gui] Checking for updates...\n")
        self._start(cmd)

    def _on_install(self):
        cmd = self._cmd(dry_run=False)
        if not cmd: return
        if not messagebox.askyesno("Install update",
                                   "Download and install the latest release for this system?",
                                   default="yes"):
            return
        self._append_log("\n[gui] Installing update...\n")
        self._start(cmd)

    def _on_cancel(self):
        if self.proc and self.proc.poll() is None:
            try: self.proc.terminate()
            except Exception: pass
        self._append_log("[gui] Cancel requested.\n")

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Updater is running",
                                       "The updater is still running. Close anyway?",
                                       default="no"):
                return
        self.master.destroy()

    def _start(self, cmd):
        if self.running:
            messagebox.showinfo("Busy", "An operation is already running.")
            return
        self.status_var.set("Working...")
        self._set_busy(True)
        self.pb.start(10)
        self.running = True
        self._run_subprocess(cmd)

def show_updater_window(parent, repo, app_name, restart_cmd=None,
                        app_dir=None, version_file=None,
                        asset_pattern=DEFAULT_ASSET_PATTERN,
                        include_prereleases=False):
    win = tk.Toplevel(parent)
    win.transient(parent)
    win.grab_set()
    UpdaterUI(win, repo, app_name, restart_cmd, app_dir, version_file,
              asset_pattern, include_prereleases)
    return win

def run_standalone(repo, app_name, restart_cmd=None,
                   app_dir=None, version_file=None,
                   asset_pattern=DEFAULT_ASSET_PATTERN,
                   include_prereleases=False):
    root = tk.Tk()
    UpdaterUI(root, repo, app_name, restart_cmd, app_dir, version_file,
              asset_pattern, include_prereleases)
    root.mainloop()

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="GUI wrapper for updater.py")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--app-name", required=True)
    p.add_argument("--restart-cmd", default=None)
    p.add_argument("--app-dir", default=None)
    p.add_argument("--version-file", default=None)
    p.add_argument("--asset-pattern", default=DEFAULT_ASSET_PATTERN)
    p.add_argument("--include-prereleases", action="store_true")
    args = p.parse_args()
    run_standalone(args.repo, args.app_name, args.restart_cmd, args.app_dir,
                   args.version_file, args.asset_pattern, args.include_prereleases)
