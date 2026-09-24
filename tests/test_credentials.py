import os

import pytest

from smartgpms.credentials import CredentialStore, protect_text, unprotect_text


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI test")
def test_windows_dpapi_round_trip():
    encrypted = protect_text("smartgpms-test-secret")
    assert encrypted != "smartgpms-test-secret"
    assert unprotect_text(encrypted) == "smartgpms-test-secret"


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


def test_linux_does_not_try_to_decrypt_a_windows_dpapi_file(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        '{"username":"windows.user","password_dpapi":"unreadable"}',
        encoding="utf-8",
    )
    store = CredentialStore(path, platform_name="posix")

    assert store.load() == ("windows.user", "")
