"""Open links and the board window, from WSL or plain Linux."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

# Chromium browsers that can open a page as its own window (--app), Windows side first.
APP_BROWSERS = (
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "microsoft-edge",
    "google-chrome",
    "chromium",
)


def open_url(url: str) -> None:
    """Open `url` in your default browser: $BROWSER, else wsl-open, else xdg-open."""
    browser = os.environ.get("BROWSER", "").split(":")[0].strip()
    if browser:
        cmd = shlex.split(browser)
        cmd = [part.replace("%s", url) for part in cmd] if any("%s" in p for p in cmd) else [*cmd, url]
    else:
        tool = shutil.which("wsl-open") or shutil.which("xdg-open")
        if not tool:
            raise OSError("No way to open a browser here. Set $BROWSER.")
        cmd = [tool, url]
    _spawn(cmd)


def open_window(url: str, mode: str) -> str:
    """Open the board. In "app" mode use a separate Edge/Chrome window when one is installed.
    Returns a short name of what was used."""
    if mode == "app":
        for candidate in APP_BROWSERS:
            exe = candidate if candidate.startswith("/") else shutil.which(candidate)
            if exe and Path(exe).is_file():
                # Run Windows programs from a Windows directory, not from \\wsl.localhost\...
                cwd = "/mnt/c" if exe.startswith("/mnt/") else None
                _spawn([exe, f"--app={url}"], cwd=cwd)
                return Path(exe).stem
    open_url(url)
    return "your browser"


def _spawn(cmd: list[str], cwd: str | None = None) -> None:
    subprocess.Popen(
        cmd,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
