import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from smartgpms.credentials import CredentialStore


def test_credential_store_round_trip_and_clear(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("user.one", "test-password")
    assert store.load() == ("user.one", "test-password")
    assert store.public() == {
        "username": "user.one",
        "has_password": True,
        "backend": store.backend,
    }
    store.clear()
    assert store.load() == ("", "")


def test_linux_credential_store_uses_local_fernet_key(tmp_path):
    store = CredentialStore(
        tmp_path / "credentials.json", platform_name="posix"
    )

    store.save("linux.user", "linux-password")

    assert store.load() == ("linux.user", "linux-password")
    assert store.public() == {
        "username": "linux.user",
        "has_password": True,
        "backend": "Linux 本机密钥",
    }
    assert "linux-password" not in store.path.read_text(encoding="utf-8")
    assert store.key_path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o600


def test_linux_rejects_a_non_fernet_credential_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        '{"username":"windows.user","scheme":"windows-dpapi","password_dpapi":"unreadable"}',
        encoding="utf-8",
    )
    store = CredentialStore(path, platform_name="posix")

    with pytest.raises(RuntimeError, match="Linux Fernet"):
        store.load()


def test_windows_service_identity_can_ignore_another_dpapi_identity(
    tmp_path, monkeypatch
):
    path = tmp_path / "credentials.json"
    path.write_text(
        '{"username":"desktop.user","scheme":"windows-dpapi","password_dpapi":"encrypted"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "smartgpms.credentials._windows_unprotect",
        lambda _value: (_ for _ in ()).throw(OSError("different DPAPI identity")),
    )

    store = CredentialStore(path, platform_name="nt")

    assert store.load() == ("desktop.user", "")
    assert store.public()["has_password"] is False


def test_linux_credential_store_serializes_concurrent_saves(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json", platform_name="posix")
    expected = {f"user.{index}": f"password-{index}" for index in range(8)}

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda item: store.save(*item), expected.items()))

    username, password = store.load()
    assert password == expected[username]
