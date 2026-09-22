from __future__ import annotations

import base64
import ctypes
import json
from ctypes import wintypes
from pathlib import Path


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    ), buffer


def protect_text(value: str) -> str:
    if not value:
        return ""
    source, keepalive = _blob(value.encode("utf-8"))
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "smartGPMS", None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del keepalive


def unprotect_text(value: str) -> str:
    if not value:
        return ""
    source, keepalive = _blob(base64.b64decode(value))
    output = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)
        del keepalive


class CredentialStore:
    """Stores only a DPAPI ciphertext tied to the current Windows user."""

    def __init__(self, path: Path):
        self.path = path

    def save(self, username: str, password: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "username": username.strip(),
            "password_dpapi": protect_text(password),
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def load(self) -> tuple[str, str]:
        if not self.path.exists():
            return "", ""
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return str(payload.get("username", "")), unprotect_text(
            str(payload.get("password_dpapi", ""))
        )

    def public(self) -> dict[str, object]:
        username, password = self.load()
        return {"username": username, "has_password": bool(password)}

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
