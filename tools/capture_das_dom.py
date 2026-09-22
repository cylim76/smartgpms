"""One-time developer helper: reuse gerp-import login and capture authenticated DAS HTML."""

from __future__ import annotations

import getpass
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
from actions.sso_session import ensure_sso_session
from storage import database as gerp_database
from workflows.das_tm_gateout import (
    open_das_login_sso_page,
    open_das_tm_gateout_page,
)


def main() -> int:
    user = gerp_database.get_local_sso_user()
    password_override = getpass.getpass("SSO password (input is not echoed): ")
    runtime = {
        "browser": user.get("browser") or "edge",
        "login_username": user.get("sso_login_id") or "",
        "login_password": password_override or user.get("sso_password") or "",
        "employee_no": user.get("employee_no") or "",
        "personal_id_code": user.get("personal_id_code") or "",
        "sso_session_trust_recent_seconds": 0,
    }
    if not runtime["login_username"] or not runtime["login_password"]:
        raise RuntimeError("gerp-import 尚未保存 SSO 用户名或密码")
    ensure_sso_session(runtime)
    open_das_login_sso_page(runtime)
    open_das_tm_gateout_page(runtime)
    time.sleep(3)
    activate_browser_window(runtime["browser"])
    script = "(()=>{const a=document.createElement('a');a.download='smartgpms_gate_dom.html';a.href=URL.createObjectURL(new Blob([document.documentElement.outerHTML],{type:'text/html'}));a.click();document.title='SMARTGPMS DOM CAPTURED'})()"
    pyautogui.hotkey("ctrl", "l")
    pyautogui.write("javascript:", interval=0)
    pyperclip.copy(script)
    pyautogui.hotkey("ctrl", "v")
    pyautogui.press("enter")
    time.sleep(5)
    candidates = []
    for folder in (Path.home() / "Downloads", Path.home() / "下载"):
        candidates.extend(folder.glob("smartgpms_gate_dom*.html"))
    if not candidates:
        raise RuntimeError("浏览器未生成 DOM 下载文件")
    source = max(candidates, key=lambda path: path.stat().st_mtime)
    target = PROJECT_ROOT / "data" / "captured_gate_dom.html"
    shutil.copy2(source, target)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
