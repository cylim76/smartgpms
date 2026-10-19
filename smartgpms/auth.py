from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from typing import Any

from .database import Database
from .time_utils import business_now

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
PASSWORD_MIN_LENGTH = 8
SESSION_HOURS = 12
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def _validate_username(username: str) -> str:
    normalized = username.strip()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("用户名只能包含字母、数字、点、下划线和短横线，长度3至64位")
    return normalized


def _validate_password(password: str) -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码至少需要 {PASSWORD_MIN_LENGTH} 位")
    if len(password) > 256:
        raise ValueError("密码长度不能超过256位")
    return password


def hash_password(password: str) -> str:
    password = _validate_password(password)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            salt.hex(),
            derived.hex(),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(expected)),
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (TypeError, ValueError):
        return False


def public_user(user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": int(user["id"]),
        "username": str(user["username"]),
        "display_name": str(user.get("display_name", "")),
        "role": str(user["role"]),
        "is_active": bool(user["is_active"]),
        "is_protected": bool(user.get("is_protected", 0)),
        "last_login_at": user.get("last_login_at"),
        "created_at": user.get("created_at"),
    }


class AuthManager:
    def __init__(self, database: Database):
        self.database = database

    def setup_required(self) -> bool:
        return self.database.app_user_count() == 0

    def setup_admin(
        self, username: str, password: str, display_name: str = ""
    ) -> dict[str, Any]:
        if not self.setup_required():
            raise ValueError("系统管理员已经创建")
        user = self.database.create_app_user(
            _validate_username(username),
            display_name.strip(),
            hash_password(password),
            "admin",
            is_protected=True,
        )
        return public_user(user) or {}

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        user = self.database.app_user_by_username(username.strip())
        if (
            not user
            or not bool(user.get("is_active"))
            or not verify_password(password, str(user.get("password_hash", "")))
        ):
            return None
        self.database.mark_app_user_login(int(user["id"]))
        return self.database.app_user_by_id(int(user["id"]))

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        expires = business_now() + timedelta(hours=SESSION_HOURS)
        self.database.create_app_session(
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            user_id,
            expires.isoformat(timespec="seconds"),
        )
        return token

    def resolve_session(self, token: str, *, touch: bool = True) -> dict[str, Any] | None:
        if not token:
            return None
        return self.database.resolve_app_session(
            hashlib.sha256(token.encode("utf-8")).hexdigest(), touch=touch
        )

    def logout(self, token: str) -> None:
        if token:
            self.database.delete_app_session(
                hashlib.sha256(token.encode("utf-8")).hexdigest()
            )

    def create_user(
        self,
        username: str,
        password: str,
        display_name: str,
        role: str,
    ) -> dict[str, Any]:
        if role not in {"admin", "operator"}:
            raise ValueError("用户角色无效")
        user = self.database.create_app_user(
            _validate_username(username),
            display_name.strip(),
            hash_password(password),
            role,
        )
        return public_user(user) or {}

    def reset_password(self, user_id: int, password: str) -> dict[str, Any]:
        self.database.update_app_user_password(user_id, hash_password(password))
        return public_user(self.database.app_user_by_id(user_id)) or {}

    def change_password(
        self, user_id: int, current_password: str, new_password: str
    ) -> None:
        user = self.database.app_user_by_id(user_id)
        if not user or not verify_password(
            current_password, str(user.get("password_hash", ""))
        ):
            raise ValueError("当前密码不正确")
        self.database.update_app_user_password(user_id, hash_password(new_password))
