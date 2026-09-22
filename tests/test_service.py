from smartgpms.config import AppConfig
from smartgpms.credentials import CredentialStore
from smartgpms.database import Database
from smartgpms.service import SmartGPMSService


class FailingBrowser:
    def refresh_das(self):
        return True

    def fetch_cpm_detail(self, _cpm_id, _refresh):
        raise RuntimeError("temporary failure")

    def close(self):
        return None


def test_sync_cursor_does_not_advance_after_failure(tmp_path):
    config = AppConfig(tmp_path, sync_batch_size=3)
    database = Database(tmp_path / "data" / "test.sqlite3")
    database.set_setting("cpm_sync_cursor", "500")
    service = SmartGPMSService(
        config, database, CredentialStore(tmp_path / "credentials.json")
    )
    service.browser = FailingBrowser()
    try:
        result = service.sync_cpm()
        assert result["errors"] == 1
        assert database.get_setting("cpm_sync_cursor") == "500"
    finally:
        service.stop()
