"""
runner.py - runs your scripts (mponline.py, result_scraper.py, db_sync.py ...) in the background for the
Streamlit app, one at a time, with a live log. No Streamlit in here.

Why one at a time: the portal lets one login be active, and every script opens the same browser profile.
"""
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


class Runner:
    def __init__(self, logdir):
        self.logdir = Path(logdir)
        self.logdir.mkdir(parents=True, exist_ok=True)
        self.proc = None
        self.log = None
        self.title = ""
        self.started = None
        self._fh = None

    # -- state ---------------------------------------------------------------
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def exit_code(self):
        return None if self.proc is None else self.proc.poll()

    def status(self):
        if self.proc is None:
            return "idle"
        if self.running():
            return "running"
        return "finished OK" if self.exit_code() == 0 else f"finished with exit code {self.exit_code()}"

    # -- start / stop ----------------------------------------------------------
    def start(self, cmd, cwd, title="", stdin_text=None):
        """cmd = list of arguments; stdin_text = text typed into the script (e.g. the class name)."""
        if self.running():
            raise RuntimeError(f"'{self.title}' is still running.")
        self.close()
        slug = re.sub(r"[^A-Za-z0-9]+", "_", title or "run").strip("_")[:40]
        self.log = self.logdir / f"{datetime.now():%Y%m%d_%H%M%S}_{slug}.log"
        self._fh = open(self.log, "w", encoding="utf-8", errors="replace")
        self._fh.write(f"$ {' '.join(str(c) for c in cmd)}\n")
        self._fh.flush()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=str(cwd), stdout=self._fh, stderr=subprocess.STDOUT, env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL, text=True, encoding="utf-8")
        if stdin_text is not None:
            try:
                self.proc.stdin.write(stdin_text + "\n")
                self.proc.stdin.close()
            except OSError:
                pass
        self.title, self.started = title, time.time()

    def stop(self):
        if not self.running():
            return
        if os.name == "nt":                                        # also closes the browser it opened
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True)
        else:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    # -- log ----------------------------------------------------------------------
    def tail(self, lines=60):
        if not self.log or not Path(self.log).exists():
            return ""
        with open(self.log, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:])

    def elapsed(self):
        return 0 if self.started is None else int(time.time() - self.started)

    def history(self, n=10):
        logs = sorted(self.logdir.glob("*.log"), reverse=True)[:n]
        return [(p.name, datetime.fromtimestamp(p.stat().st_mtime).strftime("%d-%m-%Y %H:%M")) for p in logs]


def python_cmd(script, *args):
    """The same Python that runs the app, so the scripts see the same installed packages."""
    return [sys.executable, str(script), *args]
