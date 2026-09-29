from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def _windows_protect(value: str) -> str:
    if not value:
        return ""
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI 只能在 Windows 上使用")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    buffer = ctypes.create_string_buffer(value.encode("utf-8"))
    source = DataBlob(
        len(buffer) - 1, ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    )
    output = DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), "smartGPMS", None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def _windows_unprotect(value: str) -> str:
    if not value:
        return ""
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI 只能在 Windows 上使用")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    raw = base64.b64decode(value)
    buffer = ctypes.create_string_buffer(raw)
    source = DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


class CredentialStore:
    """Encrypt saved credentials using DPAPI on Windows and Fernet on Linux."""

    def __init__(self, path: Path, *, platform_name: str | None = None):
        self.path = path
        self.platform_name = platform_name or os.name
        self.key_path = path.with_name("credential.key")
        self._lock = threading.RLock()

    @property
    def backend(self) -> str:
        return "Windows DPAPI" if self.platform_name == "nt" else "Linux 本机密钥"

    def _fernet(self):
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            key = self.key_path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(key)
        if self.platform_name != "nt":
            self.key_path.chmod(0o600)
        return Fernet(key)

    def save(self, username: str, password: str) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.platform_name == "nt":
                payload = {
                    "username": username.strip(),
                    "scheme": "windows-dpapi",
                    "password_dpapi": _windows_protect(password),
                }
            else:
                encrypted = self._fernet().encrypt(password.encode("utf-8")).decode(
                    "ascii"
                )
                payload = {
                    "username": username.strip(),
                    "scheme": "fernet-v1",
                    "password_fernet": encrypted,
                }
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            if self.platform_name != "nt":
                temporary.chmod(0o600)
            temporary.replace(self.path)

    def load(self) -> tuple[str, str]:
        with self._lock:
            if not self.path.exists():
                return "", ""
            if self.platform_name != "nt":
                self.path.chmod(0o600)
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            username = str(payload.get("username", ""))
            scheme = str(payload.get("scheme", ""))
            if self.platform_name == "nt":
                if scheme != "windows-dpapi":
                    raise RuntimeError("凭据文件不是 Windows DPAPI 格式")
                try:
                    return username, _windows_unprotect(
                        str(payload["password_dpapi"])
                    )
                except OSError:
                    # Desktop and Windows-service modes use different DPAPI
                    # identities. Preserve the username but require the current
                    # identity to save its own encrypted password.
                    return username, ""
            if scheme != "fernet-v1":
                raise RuntimeError("凭据文件不是 Linux Fernet 格式")
            try:
                password = self._fernet().decrypt(
                    str(payload["password_fernet"]).encode("ascii")
                )
            except (InvalidToken, KeyError, ValueError) as exc:
                raise RuntimeError("无法解密已保存的 smartGPMS 凭据") from exc
            return username, password.decode("utf-8")

    def public(self) -> dict[str, object]:
        username, password = self.load()
        return {
            "username": username,
            "has_password": bool(password),
            "backend": self.backend,
        }

    def clear(self) -> None:
        with self._lock:
            if self.path.exists():
                self.path.unlink()
