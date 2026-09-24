from datetime import datetime

from smartgpms.config import AppConfig
from smartgpms.credentials import CredentialStore
from smartgpms.database import Database
from smartgpms.service import SmartGPMSService
from smartgpms.time_utils import business_now

TODAY = business_now().strftime("%Y-%m-%d 08:00:00")


def save_ready_stage4_photos(database, tmp_path, cpm_id, count=3):
    for index in range(count):
        path = tmp_path / f"{cpm_id}_{index}.jpg"
        path.write_bytes(b"photo")
        database.save_photo(
            cpm_id,
            {
                "step_code": "S1",
                "step_no": 4,
                "source_url": f"http://das/photo/{cpm_id}/{index}.jpg",
                "local_path": str(path),
                "cache_status": "ready",
            },
        )


class FailingBrowser:
    def refresh_das(self):
        return True

    def fetch_cpm_detail(self, _cpm_id, _refresh):
        raise RuntimeError("temporary failure")

    def close(self):
        return None


class DiscoverBrowser:
    def __init__(self):
        self.fetched = []

    @staticmethod
    def find_cpm_by_container(container_no):
        return [
            {"cpm_id": "500", "container_no": container_no},
            {"cpm_id": "502", "container_no": container_no},
        ]

    def fetch_cpm_detail(self, cpm_id, _refresh):
        self.fetched.append(cpm_id)
        return {
            "cpm_id": cpm_id,
            "container_no": "MSCU6639870",
            "seal_no": "SEAL1",
            "business_stage": 4,
            "das_process_status": 5,
            "status_text": "5.铅封确认",
            "photos": (
                [{"step_no": 4, "source_url": "http://das/photo/500.jpg"}]
                if cpm_id == "500"
                else []
            ),
        }

    def close(self):
        return None


class SnapshotBrowser:
    @staticmethod
    def _record(cpm_id):
        index = 100687 - int(cpm_id)
        return {
            "cpm_id": str(cpm_id),
            "container_no": f"MSCU66398{7 - index}",
            "business_stage": 1 + index,
            "latest_stage_code": ("U1", "U2", "U3")[index],
            "status_text": f"{1 + index}.阶段",
            "photos": [],
        }

    def cpm_snapshot(self, limit):
        return [
            {
                "cpm_id": str(100687 - index),
                "container_no": f"MSCU66398{7 - index}",
                "business_stage": 1 + index,
                "latest_stage_code": ("U1", "U2", "U3")[index],
                "das_status_text": f"{1 + index}.阶段",
            }
            for index in range(min(limit, 3))
        ]

    def fetch_cpm_detail(self, cpm_id, _refresh):
        return self._record(cpm_id)

    def close(self):
        return None


class BackfillBrowser:
    def __init__(self):
        self.fetched = []

    def cpm_snapshot(self, _limit):
        return [
            {
                "cpm_id": "100010",
                "container_no": "MSCU6639870",
                "business_stage": 4,
                "das_process_status": 5,
                "latest_stage_code": "S1",
                "das_status_text": "5.铅封确认",
            }
        ]

    def fetch_cpm_detail(self, cpm_id, _refresh):
        self.fetched.append(cpm_id)
        stage = 4 if cpm_id == "100010" else 2
        process_status = 5 if cpm_id == "100010" else stage
        return {
            "cpm_id": cpm_id,
            "container_no": f"MSCU{int(cpm_id):07d}"[-11:],
            "business_stage": stage,
            "das_process_status": process_status,
            "latest_stage_code": "S1" if stage == 4 else "U2",
            "status_text": "5.铅封确认" if process_status == 5 else f"{stage}.检查",
            "photos": [],
        }

    def close(self):
        return None


class OldWindowBrowser:
    def cpm_snapshot(self, _limit):
        return [
            {
                "cpm_id": "100",
                "container_no": "MSCU6639870",
                "begin_date": "2020-01-01 00:00:00",
            }
        ]

    def fetch_cpm_detail(self, *_args):
        raise AssertionError("30-day boundary should stop before detail fetch")

    def close(self):
        return None


class BoundaryBrowser:
    def __init__(self):
        self.fetched = []

    def cpm_snapshot(self, _limit):
        return [{"cpm_id": "100007", "begin_date": TODAY}]

    def fetch_cpm_detail(self, cpm_id, _refresh):
        self.fetched.append(cpm_id)
        return {
            "cpm_id": cpm_id,
            "container_no": f"TEST{cpm_id}",
            "begin_date": TODAY,
            "business_stage": 1,
            "das_process_status": 1,
            "status_text": "1.开始",
            "photos": [],
        }

    def close(self):
        return None


class MaintenanceBrowser:
    def __init__(self):
        self.fetched = []

    def cpm_snapshot(self, _limit):
        return [{"cpm_id": "200", "begin_date": TODAY}]

    def fetch_cpm_detail(self, cpm_id, _refresh):
        self.fetched.append(cpm_id)
        return {
            "cpm_id": cpm_id,
            "container_no": f"TEST{cpm_id}",
            "begin_date": TODAY,
            "business_stage": 4,
            "das_process_status": 5,
            "status_text": "5.铅封确认",
            "photos": [
                {
                    "step_no": 4,
                    "step_code": "S1",
                    "url": f"http://das/photo/{cpm_id}.jpg",
                }
            ],
        }

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


def test_missing_container_is_resolved_from_photo_page_and_prefers_stage4(tmp_path):
    config = AppConfig(tmp_path)
    database = Database(tmp_path / "data" / "test.sqlite3")
    database.set_setting("cpm_sync_cursor", "500")
    service = SmartGPMSService(
        config, database, CredentialStore(tmp_path / "credentials.json")
    )
    browser = DiscoverBrowser()
    service.browser = browser
    try:
        result = service.resolve_container("MSCU6639870")
        assert result["record"]["cpm_id"] == "500"
        assert result["checked"] == 2
        assert browser.fetched == ["500", "502"]
        assert database.get_setting("cpm_sync_cursor") == "500"
        assert database.cpm_by_id("502") is not None
    finally:
        service.stop()


def test_photo_page_lookup_uses_largest_cpmid_when_both_have_stage4(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )

    class MultipleStage4Browser(DiscoverBrowser):
        def fetch_cpm_detail(self, cpm_id, _refresh):
            detail = super().fetch_cpm_detail(cpm_id, _refresh)
            detail["photos"] = [
                {
                    "step_no": 4,
                    "source_url": f"http://das/photo/{cpm_id}.jpg",
                }
            ]
            return detail

    service.browser = MultipleStage4Browser()
    try:
        result = service.resolve_container("MSCU6639870")
        assert result["record"]["cpm_id"] == "502"
    finally:
        service.stop()


def test_latest_snapshot_is_saved_newest_first(tmp_path):
    config = AppConfig(tmp_path, startup_snapshot_size=3)
    database = Database(tmp_path / "data" / "test.sqlite3")
    database.enqueue_job("download_ocr", "42")
    service = SmartGPMSService(
        config, database, CredentialStore(tmp_path / "credentials.json")
    )
    service.browser = SnapshotBrowser()
    try:
        result = service.sync_latest_window()
        assert result["latest_cpm_id"] == "100687"
        assert result["oldest_cpm_id"] == "100685"
        assert result["found"] == 3
        assert database.latest_valid_cpm("MSCU663987") is not None
        assert database.get_setting("cpm_sync_cursor") == "100688"
        with database.connect() as connection:
            queued = connection.execute(
                "SELECT status FROM background_jobs WHERE cpm_id='42'"
            ).fetchone()
        assert queued["status"] == "pending"
        assert service.activity()["entries"][-1]["source"] == "sync"
    finally:
        service.stop()


def test_activity_can_describe_container_verification_stage(tmp_path):
    service = SmartGPMSService(
        AppConfig(tmp_path),
        Database(tmp_path / "data" / "test.sqlite3"),
        CredentialStore(tmp_path / "credentials.json"),
    )
    try:
        service.log_activity(
            "CAJU6040388：下载四阶段照片 1/4",
            source="verify",
            container_no="caju6040388",
            stage="download",
        )
        entry = service.activity()["entries"][-1]
        assert entry["container_no"] == "CAJU6040388"
        assert entry["stage"] == "download"
    finally:
        service.stop()


def test_activity_discards_entries_older_than_two_hours(tmp_path, monkeypatch):
    service = SmartGPMSService(
        AppConfig(tmp_path),
        Database(tmp_path / "data" / "test.sqlite3"),
        CredentialStore(tmp_path / "credentials.json"),
    )
    clock = [1_000.0]
    monkeypatch.setattr("smartgpms.service.time.time", lambda: clock[0])
    try:
        service.log_activity("过期日志")
        clock[0] += 2 * 60 * 60 + 1
        service.log_activity("当前日志")

        assert [entry["message"] for entry in service.activity()["entries"]] == [
            "当前日志"
        ]
    finally:
        service.stop()


def test_new_install_initializes_current_valid_window(tmp_path):
    config = AppConfig(tmp_path, startup_snapshot_size=3)
    database = Database(tmp_path / "data" / "test.sqlite3")
    service = SmartGPMSService(
        config, database, CredentialStore(tmp_path / "credentials.json")
    )
    service.browser = SnapshotBrowser()
    try:
        result = service.sync_latest_window()
        assert result["checked"] == 3
        assert result["found"] == 3
        assert database.get_setting("cpm_initialized") == "1"
    finally:
        service.stop()


def test_snapshot_backfills_ids_missing_from_first_result_page(tmp_path):
    config = AppConfig(tmp_path, startup_snapshot_size=4)
    database = Database(tmp_path / "data" / "test.sqlite3")
    service = SmartGPMSService(
        config, database, CredentialStore(tmp_path / "credentials.json")
    )
    browser = BackfillBrowser()
    service.browser = browser
    try:
        result = service.sync_latest_window()
        assert result["checked"] == 4
        assert browser.fetched == ["100010", "100009", "100008", "100007"]
        assert result["queued"] == 1
        assert result["oldest_cpm_id"] == "100007"
    finally:
        service.stop()


def test_daily_scan_stops_at_thirty_day_boundary(tmp_path):
    config = AppConfig(tmp_path, startup_snapshot_size=2)
    database = Database(tmp_path / "data" / "test.sqlite3")
    for cpm_id in ("100", "99"):
        database.upsert_cpm(
            {
                "cpm_id": cpm_id,
                "container_no": f"MSCU0000{cpm_id}",
                "begin_date": "2020-01-01 00:00:00",
                "business_stage": 1,
            }
        )
    database.set_setting("cpm_initialized", "1")
    service = SmartGPMSService(
        config, database, CredentialStore(tmp_path / "credentials.json")
    )
    service.browser = OldWindowBrowser()
    try:
        result = service.sync_latest_window()
        assert result["checked"] == 0
        assert result["found"] == 0
    finally:
        service.stop()


def test_new_scan_stops_at_fixed_local_maximum_not_arbitrary_existing_row(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "100005",
            "container_no": "TEST100005",
            "begin_date": TODAY,
            "business_stage": 4,
            "das_process_status": 5,
        }
    )
    save_ready_stage4_photos(database, tmp_path, "100005")
    database.set_setting("cpm_initialized", "1")
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )
    browser = BoundaryBrowser()
    service.browser = browser
    try:
        result = service.sync_latest_window()
        assert browser.fetched == ["100007", "100006"]
        assert result["scanned"] == 3
        assert result["new"] == 2
        assert result["updated"] == 0
        assert result["skipped"] == 1
        assert result["downloads"] == 1
    finally:
        service.stop()


def test_recent_maintenance_rechecks_only_incomplete_or_missing_photo_rows(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    for cpm_id, process_status, begin_date in (
        ("200", 5, TODAY),
        ("199", 4, TODAY),
        ("198", 5, TODAY),
        ("197", 4, "2026-07-01 08:00:00"),
    ):
        database.upsert_cpm(
            {
                "cpm_id": cpm_id,
                "container_no": f"TEST{cpm_id}",
                "begin_date": begin_date,
                "business_stage": min(4, process_status),
                "das_process_status": process_status,
                "das_status_text": (
                    "5.铅封确认" if process_status == 5 else "4.封箱检查"
                ),
            }
        )
    save_ready_stage4_photos(database, tmp_path, "200")
    database.set_setting("cpm_initialized", "1")
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )
    browser = MaintenanceBrowser()
    service.browser = browser
    try:
        result = service.sync_latest_window()
        assert browser.fetched == ["199"]
        assert result["scanned"] == 3
        assert result["new"] == 0
        assert result["updated"] == 1
        assert result["skipped"] == 1
        assert result["downloads"] == 3
        assert service.activity()["entries"][-1]["message"].endswith(
            "扫描 3 箱 / 新增 0 箱 / 更新 1 箱 / 跳过完成 1 箱 / 后台处理排队 3 箱"
        )
    finally:
        service.stop()


def test_background_schedule_runs_only_from_0700_through_2359():
    assert not SmartGPMSService._background_window_open(
        datetime.fromisoformat("2026-09-23T06:59:59+08:00")
    )
    assert SmartGPMSService._background_window_open(
        datetime.fromisoformat("2026-09-23T07:00:00+08:00")
    )
    assert SmartGPMSService._background_window_open(
        datetime.fromisoformat("2026-09-23T23:59:59+08:00")
    )
    assert not SmartGPMSService._background_window_open(
        datetime.fromisoformat("2026-09-24T00:00:00+08:00")
    )


def test_background_job_logs_actual_completed_original_count(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    database.enqueue_job("download_ocr", "100715")
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )

    class JobBrowser:
        @staticmethod
        def fetch_cpm_detail(cpm_id):
            return {
                "cpm_id": cpm_id,
                "das_process_status": 5,
                "photos": [{"step_no": 4, "source_url": "http://das/photo.jpg"}],
            }

        @staticmethod
        def close():
            return None

    class JobPhotos:
        @staticmethod
        def process(_cpm_id, _detail):
            return {"downloaded_photo_count": 3}

    service.browser = JobBrowser()
    service.photos = JobPhotos()
    try:
        assert service.run_one_job()
        assert service.activity()["entries"][-1]["message"].endswith(
            "监装照片下载与文字识别完成：原图 3 张"
        )
        with database.connect() as connection:
            status = connection.execute(
                "SELECT status FROM background_jobs WHERE cpm_id='100715'"
            ).fetchone()["status"]
        assert status == "done"
    finally:
        service.stop()


def test_start_queues_one_time_migration_for_s1_named_originals(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "100715",
            "container_no": "CAAU5328959",
            "business_stage": 4,
            "das_process_status": 5,
        }
    )
    original = tmp_path / "S1_legacy.jpeg"
    original.write_bytes(b"photo")
    database.save_photo(
        "100715",
        {
            "step_code": "S1",
            "step_no": 4,
            "source_url": "http://das/photo/S1_legacy.jpeg",
            "local_path": str(original),
            "cache_status": "ready",
        },
    )
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )
    try:
        service.start()
        assert database.get_setting("das_photo_label_filename_migrated") == "1"
        with database.connect() as connection:
            job = connection.execute(
                "SELECT status FROM background_jobs WHERE cpm_id='100715'"
            ).fetchone()
        assert job["status"] == "pending"
        assert any(
            "迁移为 DAS F 编号文件名" in entry["message"]
            for entry in service.activity()["entries"]
        )
    finally:
        service.stop()


def test_cache_cleanup_is_scheduled_for_next_local_noon(tmp_path):
    service = SmartGPMSService(
        AppConfig(tmp_path),
        Database(tmp_path / "data" / "test.sqlite3"),
        CredentialStore(tmp_path / "credentials.json"),
    )
    try:
        assert service._next_cache_cleanup(
            datetime.fromisoformat("2026-09-23T08:00:00+08:00")
        ) == datetime.fromisoformat("2026-09-23T12:00:00+08:00")
        assert service._next_cache_cleanup(
            datetime.fromisoformat("2026-09-23T12:00:01+08:00")
        ) == datetime.fromisoformat("2026-09-24T12:00:00+08:00")
    finally:
        service.stop()


def test_review_ocr_is_reused_without_live_reprocessing(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "88",
            "container_no": "MSCU6639870",
            "business_stage": 4,
            "das_process_status": 5,
        }
    )
    save_ready_stage4_photos(database, tmp_path, "88")
    database.update_ocr_status("88", "review")
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )

    class MustNotProcess:
        @staticmethod
        def process(*_args):
            raise AssertionError("review OCR should be reused from the database")

    service.photos = MustNotProcess()
    try:
        result = service.prepare_container("MSCU6639870")
        assert result is not None
        assert result["ocr_status"] == "review"
    finally:
        service.stop()


def test_archive_requires_three_downloaded_stage4_originals(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "88",
            "container_no": "CAJU6050344",
            "business_stage": 4,
            "das_process_status": 5,
        }
    )
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )
    try:
        save_ready_stage4_photos(database, tmp_path, "88", count=1)
        assert not service._archive_files_complete("88")
        assert database.cpm_by_id("88")["downloaded_photo_count"] == 1

        save_ready_stage4_photos(database, tmp_path, "88", count=3)
        assert service._archive_files_complete("88")
        assert database.cpm_by_id("88")["downloaded_photo_count"] == 3
    finally:
        service.stop()


def test_gate_sync_selects_latest_plan_and_queues_pdf(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )

    class GateBrowser:
        @staticmethod
        def gate_pass_snapshot(date_value):
            assert date_value == "20260924"
            common = {
                "application_date": "2026-09-24",
                "container_no": "MSCU6639870",
                "seal_no": "SEAL1",
                "process_status": "返出批准",
            }
            return [
                {
                    **common,
                    "gate_pass_no": "PASS1",
                    "sequence_no": "1",
                    "planned_departure_at": "2026-09-24 08:00",
                },
                {
                    **common,
                    "gate_pass_no": "PASS2",
                    "sequence_no": "2",
                    "planned_departure_at": "2026-09-24 09:00",
                },
            ]

        @staticmethod
        def close():
            return None

    service.browser = GateBrowser()
    try:
        result = service.sync_gate_passes(
            datetime.fromisoformat("2026-09-24T10:00:00+08:00")
        )
        assert result == {
            "scanned": 2,
            "new": 2,
            "updated": 0,
            "queued": 2,
            "busy": False,
        }
        assert database.latest_gate_pass("MSCU6639870")["gate_pass_no"] == "PASS2"
        with database.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS value FROM background_jobs WHERE job_type='gate_pdf'"
            ).fetchone()["value"]
        assert count == 2
    finally:
        service.stop()


def test_gatepass_pdf_name_uses_planned_departure_timestamp(tmp_path):
    service = SmartGPMSService(
        AppConfig(tmp_path),
        Database(tmp_path / "data" / "test.sqlite3"),
        CredentialStore(tmp_path / "credentials.json"),
    )
    try:
        path = service.gatepass_pdf_path(
            {
                "planned_departure_at": "2026-09-24 08:49",
                "application_date": "2026-09-23",
                "container_no": "MSCU6639870",
                "gate_pass_no": "PASS1",
            }
        )

        assert path.name == "20260924084900_MSCU6639870_PASS1.pdf"
        assert path.parent == tmp_path / "data" / "gatepass" / "2026" / "09"
    finally:
        service.stop()


def test_gate_sync_requeues_pdf_when_cached_filename_is_outdated(tmp_path):
    database = Database(tmp_path / "data" / "test.sqlite3")
    service = SmartGPMSService(
        AppConfig(tmp_path),
        database,
        CredentialStore(tmp_path / "credentials.json"),
    )
    row = {
        "application_date": "2026-09-24",
        "container_no": "MSCU6639870",
        "seal_no": "SEAL1",
        "gate_pass_no": "PASS1",
        "sequence_no": "1",
        "process_status": "返出批准",
        "planned_departure_at": "2026-09-24 08:49",
    }
    record = service._gate_record(row)
    database.upsert_gate_pass(record)
    legacy_pdf = (
        tmp_path
        / "data"
        / "gatepass"
        / "2026"
        / "09"
        / "20260924_MSCU6639870_PASS1.pdf"
    )
    legacy_pdf.parent.mkdir(parents=True, exist_ok=True)
    legacy_pdf.write_bytes(b"old")
    database.mark_gate_pdf_ready(record["gate_key"], str(legacy_pdf))

    class GateBrowser:
        @staticmethod
        def gate_pass_snapshot(_date_value):
            return [row]

        @staticmethod
        def close():
            return None

    service.browser = GateBrowser()
    try:
        result = service.sync_gate_passes(
            datetime.fromisoformat("2026-09-24T10:00:00+08:00")
        )

        assert result["queued"] == 1
    finally:
        service.stop()
