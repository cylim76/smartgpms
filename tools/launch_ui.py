"""Open the local smartGPMS UI as a browser app window after the server is ready."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_URL = os.environ.get(
    "SMARTGPMS_UI_URL",
    f"http://127.0.0.1:{os.environ.get('SMARTGPMS_PORT', '8765')}",
)


def _browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    if os.name == "nt":
        suffixes = (
            Path("Microsoft/Edge/Application/msedge.exe"),
            Path("Google/Chrome/Application/chrome.exe"),
        )
        for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if root:
                candidates.extend(Path(root) / suffix for suffix in suffixes)
    elif sys.platform.startswith("linux"):
        for executable in (
            "microsoft-edge",
            "microsoft-edge-stable",
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ):
            if resolved := shutil.which(executable):
                candidates.append(Path(resolved))
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            candidates.append(Path(playwright.chromium.executable_path))
    except (ImportError, OSError, PlaywrightError):
        # setup may be incomplete; webbrowser.open remains the final fallback.
        pass
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
