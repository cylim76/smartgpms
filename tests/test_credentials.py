from smartgpms.credentials import CredentialStore, protect_text, unprotect_text


def test_windows_dpapi_round_trip():
    encrypted = protect_text("smartgpms-test-secret")
    assert encrypted != "smartgpms-test-secret"
    assert unprotect_text(encrypted) == "smartgpms-test-secret"


def test_credential_store_round_trip_and_clear(tmp_path):
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("user.one", "test-password")
    assert store.load() == ("user.one", "test-password")
    assert store.public() == {"username": "user.one", "has_password": True}
    store.clear()
    assert store.load() == ("", "")
