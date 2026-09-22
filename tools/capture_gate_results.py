"""Capture authenticated gate query results from the currently open Edge session."""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

GERP_ROOT = Path(r"D:\RPA\gerp-import")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(GERP_ROOT))

import pyautogui
import pyperclip
from actions.browser import activate_browser_window


def run_script(script: str) -> None:
    activate_browser_window("edge")
    pyautogui.hotkey("ctrl", "l")
    pyautogui.write("javascript:", interval=0)
    pyperclip.copy(script)
    pyautogui.hotkey("ctrl", "v")
    pyautogui.press("enter")


def newest(name: str) -> Path:
    files = list((Path.home() / "Downloads").glob(name.replace(".html", "*.html")))
    if not files:
        raise RuntimeError(f"download not found: {name}")
    return max(files, key=lambda path: path.stat().st_mtime)


run_script("document.getElementById('btnSearch').click()")
time.sleep(8)
run_script(
    "(()=>{const a=document.createElement('a');a.download='smartgpms_gate_results.html';a.href=URL.createObjectURL(new Blob([document.documentElement.outerHTML],{type:'text/html'}));a.click()})()"
)
time.sleep(5)
target = PROJECT_ROOT / "data" / "captured_gate_results.html"
shutil.copy2(newest("smartgpms_gate_results.html"), target)
print(target)
