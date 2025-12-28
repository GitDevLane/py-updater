#!/usr/bin/env python3
"""
Tkinter GUI wrapper for updater.py

What this file does:
- Builds a small Tkinter window that lets a user:
    1) Check for updates (dry-run)
    2) Install an update (real run)
- It does NOT contain update logic itself.
  Instead, it launches "updater.py" as a *separate process* (subprocess)
  and streams updater.py's printed output into the GUI log.

How it can be used:
- Standalone: run this file by itself to open an updater window.
- Embedded: import show_updater_window(...) from another Tkinter app
  to open the updater as a Toplevel window.
"""

import os          # paths, environment variables, file checks (cross-platform)
import sys         # gives sys.executable (current python interpreter path)
import queue       # thread-safe queue to move text from worker thread -> GUI thread
import threading   # run a background thread to read subprocess stdout (prevents UI freeze)
import subprocess  # start updater.py as a separate process and capture its output
import tkinter as tk
from tkinter import ttk, messagebox  # ttk = modern themed widgets; messagebox = pop-up dialogs

# -------------------------------------------------------------------
# Release asset naming convention (ties GUI to your GitHub Release assets)
# -------------------------------------------------------------------
# updater.py expects the release zip file to be named in some predictable way.
# This pattern is a format-string used to create the exact asset filename.
#
# Placeholders:
#   {app}  -> app_name you pass in (ex: "myapp")
#   {os}   -> normalized OS from updater.py (ex: "windows", "macos", "linux")
#   {arch} -> normalized CPU arch from updater.py (ex: "x64", "arm64")
#
# Example final filename might be:
#   myapp-windows-x64.zip
DEFAULT_ASSET_PATTERN = "{app}-{os}-{arch}.zip"

# -------------------------------------------------------------------
# Helper: center the window on the screen
# -------------------------------------------------------------------
def _center_on_screen(window, w=640, h=420):
    """
    Positions a Tkinter window in the center of the user's screen.

    Why call update_idletasks()?
    - Tkinter geometry can be "pending" until idle tasks run.
    - Calling update_idletasks() makes sure width/height measurements are accurate.
    """
    window.update_idletasks()

    # Screen width/height in pixels for the user's monitor.
    sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()

    # Compute top-left corner (x,y) so that a w x h window is centered.
    x, y = (sw - w) // 2, (sh - h) // 2

    # geometry format: "{width}x{height}+{x}+{y}"
    window.geometry(f"{w}x{h}+{x}+{y}")

# -------------------------------------------------------------------
# Helper: determine which Python interpreter to use
# -------------------------------------------------------------------
def _python_exe():
    """
    Returns the path to the Python executable currently running this GUI.

    Why sys.executable?
    - If you're in a venv, it points to the venv's python.
    - If you have multiple Pythons installed, it avoids calling the wrong one.
    - This makes updater.py run under the same environment as the GUI.
    """
    return sys.executable or "python"


class UpdaterUI:
    """
    Main GUI controller class.

    It owns:
    - UI widgets (buttons, progress bar, log box)
    - subprocess handle (self.proc)
    - reader thread that captures subprocess output
    - queue that safely transfers output -> GUI

    IMPORTANT Tkinter rule:
    - Only the main thread should touch Tkinter widgets.
    - So we read subprocess output in a background thread,
      then push lines into a Queue, then the GUI thread polls the Queue.
    """
    def __init__(self, master, repo, app_name,
                 restart_cmd=None, app_dir=None, version_file=None,
                 asset_pattern=DEFAULT_ASSET_PATTERN, include_prereleases=False):
        """
        master:
            - tk.Tk() when standalone
            - tk.Toplevel() when embedded in another Tk app

        repo:
            - GitHub "owner/repo" string (ex: "GitDevLane/py-updater")

        app_name:
            - logical name used to build release asset filename
              via DEFAULT_ASSET_PATTERN (ex: "myapp")

        restart_cmd:
            - optional command passed to updater.py to restart your app after update

        app_dir / version_file:
            - optional paths passed to updater.py
            - app_dir: directory that will be swapped with new version
            - version_file: JSON file containing installed version

        asset_pattern:
            - override naming convention if your release asset uses a different name

        include_prereleases:
            - if True, passes --include-prereleases to updater.py
        """
        # Basic configuration/state
        self.master = master
        self.repo = repo
        self.app_name = app_name
        self.restart_cmd = restart_cmd
        self.app_dir = app_dir
        self.version_file = version_file
        self.asset_pattern = asset_pattern
        self.include_prereleases = include_prereleases

        # Runtime process/thread state:
        self.proc = None                 # will hold subprocess.Popen(...) instance
        self.reader_thread = None        # background thread that reads proc.stdout
        self.stdout_q = queue.Queue()    # thread-safe pipe: reader thread -> GUI thread
        self.running = False             # True while an update/check is in progress

        # Build the widgets and start the stdout polling loop
        self._build_ui()
        self._pump_stdout()

    def _build_ui(self):
        """
        Creates the window layout and widgets.
        """
        # Window title and minimum size (prevents too-small resizing)
        self.master.title("Updater")
        self.master.minsize(520, 320)

        # Center the window on the screen for nicer UX
        _center_on_screen(self.master)

        # --- Top row: status text + action buttons ---
        top = ttk.Frame(self.master, padding=12)
        top.pack(fill="x")

        # status_var drives the label text (StringVar lets you update label live)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", anchor="w")

        # Button container on the right
        btns = ttk.Frame(top)
        btns.pack(side="right", anchor="e")

        # Button: dry-run check (doesn't install)
        self.btn_check = ttk.Button(btns, text="Check for updates", command=self._on_check)
        self.btn_check.pack(side="left", padx=(0, 6))

        # Button: real install (downloads + swaps)
        self.btn_install = ttk.Button(btns, text="Install update", command=self._on_install)
        self.btn_install.pack(side="left", padx=(0, 6))

        # Button: cancel running subprocess (disabled until something runs)
        self.btn_cancel = ttk.Button(btns, text="Cancel", command=self._on_cancel, state="disabled")
        self.btn_cancel.pack(side="left")

        # --- Progress bar row ---
        pf = ttk.Frame(self.master, padding=(12, 0, 12, 6))
        pf.pack(fill="x")

        # Indeterminate = "busy spinner" style (we don't know exact progress)
        self.pb = ttk.Progressbar(pf, mode="indeterminate")
        self.pb.pack(fill="x")

        # --- Log output area ---
        logf = ttk.Frame(self.master, padding=12)
        logf.pack(fill="both", expand=True)

        # Text widget is perfect for streaming logs
        # state="disabled" prevents user editing; we temporarily enable to insert text.
        self.log = tk.Text(logf, height=12, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)

        # Scrollbar wired to the Text widget yview
        yscroll = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        yscroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=yscroll.set)

        # --- Bottom row: Close button ---
        bf = ttk.Frame(self.master, padding=(12, 0, 12, 12))
        bf.pack(fill="x")

        # Close window button (disabled while running to avoid weird half-state)
        self.btn_close = ttk.Button(bf, text="Close", command=self._on_close)
        self.btn_close.pack(side="right")

    def _append_log(self, text):
        """
        Append text to the log Text widget (and auto-scroll to bottom).
        Must only be called from the Tkinter main thread.
        """
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")  # scroll to bottom
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool):
        """
        Toggle UI control states depending on whether an operation is running.

        busy=True:
          - disable check/install/close
          - enable cancel
        busy=False:
          - enable check/install/close
          - disable cancel
        """
        self.btn_check.configure(state="disabled" if busy else "normal")
        self.btn_install.configure(state="disabled" if busy else "normal")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        self.btn_close.configure(state="disabled" if busy else "normal")

    def _cmd(self, dry_run=False):
        """
        Build the command-line argument list for launching updater.py.

        Returns:
            list[str] command suitable for subprocess.Popen
            or None if updater.py is missing.
        """
        # Find updater.py next to this GUI file (same folder).
        updater_py = os.path.join(os.path.dirname(__file__), "updater.py")

        # If it isn't there, we can't update, so show an error and abort.
        if not os.path.isfile(updater_py):
            messagebox.showerror("Missing updater.py", f"Not found:\n{updater_py}")
            return None

        # Base command:
        # - Uses current Python interpreter
        # - Runs updater.py
        # - Supplies required args
        cmd = [
            _python_exe(), updater_py,
            "--repo", self.repo,
            "--app-name", self.app_name,
            "--asset-pattern", self.asset_pattern,
            "--timeout", "120",
        ]

        # Optional flags/params appended if configured
        if self.include_prereleases:
            cmd.append("--include-prereleases")
        if self.restart_cmd:
            cmd += ["--restart-cmd", self.restart_cmd]
        if self.app_dir:
            cmd += ["--app-dir", self.app_dir]
        if self.version_file:
            cmd += ["--version-file", self.version_file]

        # dry_run means: do all checks and print what would happen,
        # but do NOT download/install anything.
        if dry_run:
            cmd.append("--dry-run")

        return cmd

    def _run_subprocess(self, cmd):
        """
        Start updater.py as a subprocess and begin streaming its output.

        Key design:
        - Popen captures stdout so we can show it in the GUI.
        - A background thread reads stdout and pushes lines into a Queue.
        - Tkinter main thread polls the Queue and updates the Text widget.
        """
        # Copy environment so GH_TOKEN (if set in the parent process)
        # is available to updater.py (needed for private repos / rate limits).
        env = os.environ.copy()

        try:
            # Start the updater subprocess.
            # stdout=PIPE captures output.
            # stderr=STDOUT merges stderr into stdout so everything is shown in one stream.
            # bufsize=1 + universal_newlines=True gives line-buffered text mode output.
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                env=env,
            )
        except Exception as e:
            # If starting the process fails, push the message into the queue
            # so it shows up in the GUI log.
            self.stdout_q.put(f"[gui] failed to start: {e}\n")
            self.running = False
            return

        def _reader():
            """
            Runs in a background thread.

            Reads each line of updater.py output and enqueues it.
            When the subprocess exits, it records the exit code and marks running False.
            """
            for line in self.proc.stdout:
                self.stdout_q.put(line)

            # Wait for process to fully exit and get exit code
            rc = self.proc.wait()
            self.stdout_q.put(f"[gui] updater exited with code {rc}\n")
            self.running = False

        # Launch reader thread as daemon so it won't block app shutdown
        self.reader_thread = threading.Thread(target=_reader, daemon=True)
        self.reader_thread.start()

    def _pump_stdout(self):
        """
        Poll the Queue for new stdout lines and append them to the log widget.

        Why do this with after()?
        - Tkinter is single-threaded; widget updates must happen on the main thread.
        - after() schedules this function to run periodically without blocking the UI.

        This is the "consumer" side of the producer/consumer design:
          producer = reader thread pushing lines into stdout_q
          consumer = GUI thread pulling lines and updating widgets
        """
        try:
            # Drain ALL currently-available lines without blocking.
            while True:
                line = self.stdout_q.get_nowait()
                self._append_log(line)

        except queue.Empty:
            # No more lines right now — that's normal.
            pass

        finally:
            # If operation finished, reset UI.
            if self.running:
                # If still running, schedule another near-term pump.
                self.master.after(60, self._pump_stdout)
            else:
                # If not running, stop spinner and re-enable buttons.
                self.pb.stop()
                self._set_busy(False)
                self.status_var.set("Idle.")

        # Keep scheduling periodic pumps to ensure UI stays responsive.
        #
        # NOTE (small improvement idea):
        # You currently schedule _pump_stdout() twice when running=True
        # (once at 60ms and also always at 200ms).
        # It still works, but it causes extra calls.
        self.master.after(200, self._pump_stdout)

    def _on_check(self):
        """
        Check button handler.
        Uses --dry-run so updater.py only *checks* and prints what it would do.
        """
        cmd = self._cmd(dry_run=True)
        if not cmd:
            return

        self._append_log("\n[gui] Checking for updates...\n")
        self._start(cmd)

    def _on_install(self):
        """
        Install button handler.
        Runs updater.py for real (download + swap).
        Asks for confirmation first.
        """
        cmd = self._cmd(dry_run=False)
        if not cmd:
            return

        # Confirm install (avoids accidental updates)
        if not messagebox.askyesno(
            "Install update",
            "Download and install the latest release for this system?",
            default="yes"
        ):
            return

        self._append_log("\n[gui] Installing update...\n")
        self._start(cmd)

    def _on_cancel(self):
        """
        Cancel button handler.
        Attempts to terminate the updater subprocess.
        """
        # poll() returns None if still running
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

        self._append_log("[gui] Cancel requested.\n")

    def _on_close(self):
        """
        Close button handler.
        If updater is still running, warn the user.
        """
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(
                "Updater is running",
                "The updater is still running. Close anyway?",
                default="no"
            ):
                return

        self.master.destroy()

    def _start(self, cmd):
        """
        Starts an operation (check/install) if one isn't already running.

        This function:
        - prevents starting two subprocesses at once
        - updates the UI to busy mode
        - launches the subprocess
        """
        if self.running:
            messagebox.showinfo("Busy", "An operation is already running.")
            return

        self.status_var.set("Working...")
        self._set_busy(True)
        self.pb.start(10)   # start indeterminate spinner
        self.running = True
        self._run_subprocess(cmd)


def show_updater_window(parent, repo, app_name, restart_cmd=None,
                        app_dir=None, version_file=None,
                        asset_pattern=DEFAULT_ASSET_PATTERN,
                        include_prereleases=False):
    """
    Embedding helper:
    Opens the updater as a modal-like Toplevel window inside an existing Tk app.

    - transient(parent): keeps it on top of the parent window (platform dependent)
    - grab_set(): makes it modal (user must close updater before interacting with parent)
    """
    win = tk.Toplevel(parent)
    win.transient(parent)
    win.grab_set()

    UpdaterUI(
        win, repo, app_name,
        restart_cmd, app_dir, version_file,
        asset_pattern, include_prereleases
    )
    return win


def run_standalone(repo, app_name, restart_cmd=None,
                   app_dir=None, version_file=None,
                   asset_pattern=DEFAULT_ASSET_PATTERN,
                   include_prereleases=False):
    """
    Standalone helper:
    Creates a Tk root window and runs the updater UI.
    """
    root = tk.Tk()
    UpdaterUI(
        root, repo, app_name,
        restart_cmd, app_dir, version_file,
        asset_pattern, include_prereleases
    )
    root.mainloop()


if __name__ == "__main__":
    # Quick testing defaults.
    # In real use, you'd typically call:
    #   python updater_gui.py --repo owner/repo --app-name myapp
    # (Your docstring mentions CLI, but this file currently uses hardcoded test values.)
    run_standalone(
        repo="GitDevLane/py-updater",
        app_name="myapp",
        restart_cmd=None,
        app_dir="app",
        version_file="version.json",
        asset_pattern=DEFAULT_ASSET_PATTERN,
        include_prereleases=False
    )