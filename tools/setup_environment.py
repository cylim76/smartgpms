"""Create the virtual environment and install platform-specific dependencies."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"


def _venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def _run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def _windows_edge_available() -> bool:
    suffix = Path("Microsoft/Edge/Application/msedge.exe")
    return any(
        (Path(root) / suffix).is_file()
        for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
        if (root := os.environ.get(variable))
    )


def _install_browser(python: Path, skip_browser: bool) -> None:
    if skip_browser:
        print("已跳过 Playwright 浏览器安装。")
        return
    if os.name == "nt" and _windows_edge_available():
        print("已检测到 Microsoft Edge，将复用本机 Edge。")
        return
    if sys.platform.startswith("linux"):
        _run(
            [
                str(python),
                "-m",
                "playwright",
                "install",
                "--with-deps",
                "chromium",
            ]
        )
        return
    _run([str(python), "-m", "playwright", "install", "chromium"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="Install Python packages without downloading a Playwright browser.",
    )
    args = parser.parse_args()
    if not (sys.platform.startswith("win") or sys.platform.startswith("linux")):
        raise SystemExit(f"暂不支持当前系统：{platform.system()}")
    if not _venv_python().is_file():
        _run([sys.executable, "-m", "venv", str(VENV_DIR)])
    python = _venv_python()
    _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
    _run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")])
    _install_browser(python, args.skip_browser)
    print(f"smartGPMS 安装完成：{platform.system()} / {platform.machine()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
