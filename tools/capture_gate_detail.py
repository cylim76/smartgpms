"""Capture the first gate detail page using the authenticated Edge session."""

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


def js(script: str) -> None:
    activate_browser_window("edge")
    pyautogui.hotkey("ctrl", "l")
    pyautogui.write("javascript:", interval=0)
    pyperclip.copy(script)
    pyautogui.hotkey("ctrl", "v")
    pyautogui.press("enter")


js("document.querySelector('#dgMain a[href*=\"__doPostBack\"]').click()")
time.sleep(8)
js(
    "(()=>{const a=document.createElement('a');a.download='smartgpms_gate_detail.html';a.href=URL.createObjectURL(new Blob([document.documentElement.outerHTML],{type:'text/html'}));a.click()})()"
)
time.sleep(5)
files = list((Path.home() / "Downloads").glob("smartgpms_gate_detail*.html"))
if not files:
    raise RuntimeError("detail DOM download not found")
source = max(files, key=lambda path: path.stat().st_mtime)
target = PROJECT_ROOT / "data" / "captured_gate_detail.html"
shutil.copy2(source, target)
print(target)
