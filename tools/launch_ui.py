"""Open the local smartGPMS UI as a browser app window after the server is ready."""

from __future__ import annotations

import os
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_URL = "http://127.0.0.1:8765"


def _browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    suffixes = (
        Path("Microsoft/Edge/Application/msedge.exe"),
        Path("Google/Chrome/Application/chrome.exe"),
    )
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidates.extend(Path(root) / suffix for suffix in suffixes)
    return candidates


def _wait_until_ready(timeout_seconds: float = 45.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{APP_URL}/api/health", timeout=1.0) as response:
                if response.status == 200:
                    return True
        except (OSError, TimeoutError, urllib.error.URLError):
            time.sleep(0.25)
    return False


def main() -> None:
    if not _wait_until_ready():
        return
    browser = next((path for path in _browser_candidates() if path.is_file()), None)
    if browser is None:
        webbrowser.open(APP_URL)
        return
    subprocess.Popen(
        [str(browser), f"--app={APP_URL}", "--start-maximized", "--no-first-run"],
        close_fds=True,
    )


if __name__ == "__main__":
    main()
