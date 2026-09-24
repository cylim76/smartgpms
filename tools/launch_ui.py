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

ROOT = Path(__file__).resolve().parents[1]
APP_URL = os.environ.get(
    "SMARTGPMS_UI_URL",
    f"http://127.0.0.1:{os.environ.get('SMARTGPMS_PORT', '8765')}",
)
DATA_DIR = Path(os.environ.get("SMARTGPMS_DATA_DIR") or ROOT / "data").resolve()
UI_PROFILE_DIR = DATA_DIR / "ui-browser-profile"
RUN_MODE = os.environ.get("SMARTGPMS_RUN_MODE", "server").strip().lower()


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


def _screen_size() -> tuple[int, int] | None:
    if os.name == "nt":
        try:
            import ctypes

            user32 = ctypes.windll.user32
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except (AttributeError, OSError):
            return None
    if sys.platform.startswith("linux") and os.environ.get("DISPLAY"):
        try:
            result = subprocess.run(
                ["xdpyinfo"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return None
        for line in result.stdout.splitlines():
            if "dimensions:" not in line:
                continue
            dimensions = line.split("dimensions:", 1)[1].strip().split()[0]
            width, separator, height = dimensions.partition("x")
            if separator and width.isdigit() and height.isdigit():
                return int(width), int(height)
    return None


def _centered_window_args(
    screen_size: tuple[int, int] | None,
) -> list[str]:
    if not screen_size:
        return ["--start-maximized"]
    screen_width, screen_height = screen_size
    width = max(1024, round(screen_width * 0.9))
    height = max(720, round(screen_height * 0.9))
    width = min(width, screen_width)
    height = min(height, screen_height)
    left = max(0, (screen_width - width) // 2)
    top = max(0, (screen_height - height) // 2)
    return [f"--window-size={width},{height}", f"--window-position={left},{top}"]


def _launch_arguments(browser: Path) -> list[str]:
    arguments = [
        str(browser),
        f"--user-data-dir={UI_PROFILE_DIR}",
        f"--app={APP_URL}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    arguments.extend(_centered_window_args(_screen_size()))
    return arguments


def _should_monitor_window() -> bool:
    return os.name == "nt" and RUN_MODE == "desktop"


def _notify_desktop_closed() -> None:
    request = urllib.request.Request(
        f"{APP_URL.rstrip('/')}/api/desktop/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5.0):
            pass
    except (OSError, TimeoutError, urllib.error.URLError):
        # The server may already be stopping or may have been closed manually.
        pass


def main() -> None:
    if not _wait_until_ready():
        return
    browser = next((path for path in _browser_candidates() if path.is_file()), None)
    if browser is None:
        webbrowser.open(APP_URL)
        return
    UI_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(_launch_arguments(browser), close_fds=True)
    if _should_monitor_window():
        process.wait()
        _notify_desktop_closed()


if __name__ == "__main__":
    main()
