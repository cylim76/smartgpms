import sqlite3

from smartgpms.database import Database


def test_new_database_uses_only_final_schema(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    with database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        cpm_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(cpm_records)")
        }

    assert not {"sync_state", "verification_jobs", "decisions"} & tables
    assert "latest_stage_code" not in cpm_columns
    assert {
        "das_process_status",
        "archive_status",
        "downloaded_photo_count",
        "superseded_by",
        "superseded_at",
    } <= cpm_columns


def test_existing_database_is_upgraded_to_current_columns(tmp_path):
    path = tmp_path / "existing.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE cpm_records (
            cpm_id TEXT PRIMARY KEY,
            container_no TEXT NOT NULL,
            begin_date TEXT NOT NULL DEFAULT '',
            end_date TEXT NOT NULL DEFAULT '',
            das_status_text TEXT NOT NULL DEFAULT '',
            business_stage INTEGER NOT NULL DEFAULT 0,
            photo_count INTEGER NOT NULL DEFAULT 0,
            print_status INTEGER NOT NULL DEFAULT 0,
            print_count INTEGER NOT NULL DEFAULT 0,
            last_printed_at TEXT,
            ocr_status TEXT NOT NULL DEFAULT 'pending',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL
        );
        INSERT INTO cpm_records(
            cpm_id,container_no,first_seen_at,last_seen_at,last_checked_at
        ) VALUES('100','MSCU6639870','2026-09-01','2026-09-01','2026-09-01');
        """
    )
    connection.close()

    database = Database(path)
    database.upsert_cpm(
        {
            "cpm_id": "100",
            "container_no": "MSCU6639870",
            "upload_quantity": "60",
            "seal_no": "SEAL100",
            "stage4_confirmed_at": "2026-09-29 12:00:00",
            "service_year": "2026",
            "inspection_result": "OK",
            "das_process_status": 5,
        }
    )

    record = database.cpm_by_id("100")
    assert record["upload_quantity"] == "60"
    assert record["seal_no"] == "SEAL100"
    assert record["stage4_confirmed_at"] == "2026-09-29 12:00:00"
    assert record["service_year"] == "2026"
    assert record["inspection_result"] == "OK"
    assert record["das_process_status"] == 5
    assert record["superseded_by"] == ""


def test_latest_cpm_and_print_state(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "10",
            "container_no": "MSCU6639870",
            "business_stage": 3,
            "end_date": "2026-09-20",
        }
    )
    database.upsert_cpm(
        {
            "cpm_id": "11",
            "container_no": "MSCU6639870",
            "business_stage": 4,
            "end_date": "2026-09-22",
        }
    )
    assert database.latest_valid_cpm("MSCU6639870")["cpm_id"] == "11"
    result = database.record_print("11", {"verdict": "match", "das_result": "ok"})
    assert result["print_kind"] == "first"
    assert database.cpm_by_id("11")["print_status"] == 1
    assert (
        database.record_print("11", {"verdict": "match", "das_result": "ok"})[
            "print_kind"
        ]
        == "reprint"
    )


def test_invalid_cpm_does_not_block_new_record_discovery_cursor(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_cpm(
        {"cpm_id": "100", "container_no": "MSCU6639870", "business_stage": 4}
    )
    database.upsert_cpm(
        {"cpm_id": "110", "container_no": "MSCU6639870", "business_stage": 4}
    )
    database.set_cpm_validity("110", False)

    assert database.max_numeric_cpm_id() == 100


def test_background_job_can_be_requeued_after_completion(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    assert database.enqueue_job("download_ocr", "88")
    assert not database.enqueue_job("download_ocr", "88")
    with database.connect() as connection:
        connection.execute("UPDATE background_jobs SET status='done' WHERE cpm_id='88'")
    assert database.enqueue_job("download_ocr", "88")
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT status,attempts FROM background_jobs WHERE cpm_id='88'"
        ).fetchall()
    assert [(row["status"], row["attempts"]) for row in rows] == [("pending", 0)]


def test_running_background_job_recovers_after_restart(tmp_path):
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    database.enqueue_job("download_ocr", "88")
    with database.connect() as connection:
        connection.execute("UPDATE background_jobs SET status='running' WHERE cpm_id='88'")

    Database(path)

    with database.connect() as connection:
        row = connection.execute(
            "SELECT status,last_error FROM background_jobs WHERE cpm_id='88'"
        ).fetchone()
    assert row["status"] == "pending"
    assert "自动恢复" in row["last_error"]


def test_manual_photo_refresh_never_collides_with_running_background_job(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.enqueue_job("download_ocr", "88")
    assert database.reserve_manual_photo_refresh("88") == "cancelled"
    with database.connect() as connection:
        connection.execute(
            "UPDATE background_jobs SET status='running' WHERE cpm_id='88'"
        )
    assert database.reserve_manual_photo_refresh("88") == "running"


def test_new_ocr_pipeline_invalidates_derived_results_only_once(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_cpm(
        {"cpm_id": "88", "container_no": "MSCU6639870", "business_stage": 4}
    )
    database.update_ocr_status("88", "ready")

    assert database.activate_ocr_pipeline("pipeline-v2") == 1
    assert database.cpm_by_id("88")["ocr_status"] == "pending"

    database.update_ocr_status("88", "ready")
    assert database.activate_ocr_pipeline("pipeline-v2") == 0
    assert database.cpm_by_id("88")["ocr_status"] == "ready"


def test_check_digit_pipeline_invalidates_only_ten_character_container_ocr(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    for cpm_id, observed in (("88", "TXGU607166"), ("89", "CAJU6050344")):
        database.upsert_cpm(
            {"cpm_id": cpm_id, "container_no": "TXGU6071669", "business_stage": 4}
        )
        photo = database.save_photo(
            cpm_id,
            {
                "step_code": "S1",
                "step_no": 4,
                "source_url": f"http://das/{cpm_id}.jpg",
                "cache_status": "ready",
            },
        )
        database.save_ocr(
            cpm_id,
            int(photo["id"]),
            "container",
            {"observed": observed, "source_hash": cpm_id},
        )
        database.update_ocr_status(cpm_id, "ready")

    assert (
        database.activate_ocr_pipeline(
            "check-digit-v4", incomplete_container_only=True
        )
        == 1
    )
    assert database.cpm_by_id("88")["ocr_status"] == "pending"
    assert database.cpm_by_id("89")["ocr_status"] == "ready"


def test_archive_status_five_survives_metadata_refresh(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    record = {"cpm_id": "99", "container_no": "MSCU6639870", "business_stage": 4}
    database.upsert_cpm(record)
    assert database.cpm_by_id("99")["archive_status"] == 4
    database.mark_archive_status("99", 5)
    database.upsert_cpm(record)
    assert database.cpm_by_id("99")["archive_status"] == 5


def test_cleaned_stage4_cache_is_distinguishable_from_missing_file(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_cpm(
        {"cpm_id": "99", "container_no": "MSCU6639870", "business_stage": 4}
    )
    photo = database.save_photo(
        "99",
        {
            "step_code": "S1",
            "step_no": 4,
            "source_url": "http://das/photo.jpg",
            "cache_status": "ready",
        },
    )
    assert database.photo_by_id("99", int(photo["id"]))["source_url"] == "http://das/photo.jpg"
    assert database.photo_by_id("other", int(photo["id"])) is None
    assert not database.stage4_cache_was_cleaned("99")
    database.mark_photo_cache_cleaned(int(photo["id"]), "99")
    assert database.stage4_cache_was_cleaned("99")


def test_duplicate_container_prefers_photos_then_larger_cpm_id(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    for cpm_id, stage in (("200", 2), ("199", 4), ("198", 4)):
        database.upsert_cpm(
            {
                "cpm_id": cpm_id,
                "container_no": "MSCU6639870",
                "business_stage": stage,
                "das_process_status": 5 if stage == 4 else stage,
                "photo_count": 3 if stage == 4 else 0,
            }
        )
    assert database.latest_valid_cpm("MSCU6639870")["cpm_id"] == "199"

    database.upsert_cpm(
        {
            "cpm_id": "201",
            "container_no": "MSCU6639870",
            "business_stage": 4,
            "das_process_status": 5,
            "photo_count": 3,
        }
    )
    assert database.latest_valid_cpm("MSCU6639870")["cpm_id"] == "201"


def test_superseded_cpm_keeps_history_but_leaves_active_queues(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    for cpm_id in ("100", "200"):
        database.upsert_cpm(
            {
                "cpm_id": cpm_id,
                "container_no": "MSCU6639870",
                "business_stage": 4,
                "photo_count": 3 if cpm_id == "100" else 0,
            }
        )
    assert database.enqueue_job("download_ocr", "100")

    superseded = database.supersede_missing_cpm_records(
        "MSCU6639870", "200", {"200"}
    )

    assert superseded == ["100"]
    old = database.cpm_by_id("100")
    assert old["is_valid"] == 0
    assert old["superseded_by"] == "200"
    assert old["superseded_at"]
    assert database.latest_valid_cpm("MSCU6639870")["cpm_id"] == "200"
    assert [row["cpm_id"] for row in database.numeric_cpm_records_desc()] == ["200"]
    with database.connect() as connection:
        job = connection.execute(
            "SELECT status,last_error FROM background_jobs WHERE cpm_id='100'"
        ).fetchone()
    assert job["status"] == "cancelled"
    assert "替代" in job["last_error"]


def test_gate_pass_prefers_latest_planned_departure_and_tracks_pdf(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    base = {
        "application_date": "2026-09-24",
        "container_no": "MSCU6639870",
        "seal_no": "SEAL1",
        "source_fingerprint": "v1",
    }
    database.upsert_gate_pass(
        {
            **base,
            "gate_key": "20260924:1:PASS1",
            "gate_pass_no": "PASS1",
            "sequence_no": "1",
            "planned_departure_at": "2026-09-24 08:00",
        }
    )
    database.upsert_gate_pass(
        {
            **base,
            "gate_key": "20260924:2:PASS2",
            "gate_pass_no": "PASS2",
            "sequence_no": "2",
            "planned_departure_at": "2026-09-24 09:00",
        }
    )

    selected = database.latest_gate_pass("MSCU6639870")
    assert selected["gate_pass_no"] == "PASS2"
    database.mark_gate_pdf_ready(selected["gate_key"], str(tmp_path / "pass.pdf"))
    assert database.gate_pass_by_key(selected["gate_key"])["pdf_status"] == "ready"

    database.upsert_gate_pass(
        {
            **base,
            "gate_key": selected["gate_key"],
            "gate_pass_no": "PASS2",
            "sequence_no": "2",
            "planned_departure_at": "2026-09-24 09:30",
            "source_fingerprint": "v2",
        }
    )
    assert database.gate_pass_by_key(selected["gate_key"])["pdf_status"] == "stale"


def test_pending_gate_departures_excludes_departed_and_tracks_ack_by_user(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.upsert_cpm(
        {
            "cpm_id": "100",
            "container_no": "CAJU6050344",
            "product_type": "空调",
            "business_stage": 4,
            "photo_count": 3,
        }
    )
    base = {
        "application_date": "2026-09-24",
        "seal_no": "SEAL1",
        "source_fingerprint": "v1",
    }
    for sequence, container, planned, actual in (
        ("1", "MSCU6639870", "2026-09-24 09:30:00", ""),
        ("2", "CAJU6050344", "2026/09/24 09:45:00", "-"),
        ("3", "TCNU5891927", "2026-09-24 09:50:00", "2026-09-24 09:55:00"),
        ("4", "TLLU7886068", "2026-09-23 09:50:00", ""),
    ):
        database.upsert_gate_pass(
            {
                **base,
                "gate_key": f"20260924:{sequence}:PASS{sequence}",
                "gate_pass_no": f"PASS{sequence}",
                "sequence_no": sequence,
                "container_no": container,
                "planned_departure_at": planned,
                "actual_departure_at": actual,
            }
        )

    rows = database.pending_gate_departures("2026-09-24", "QingLong.Lin")

    assert [row["container_no"] for row in rows] == [
        "CAJU6050344",
        "MSCU6639870",
    ]
    assert rows[0]["product_type"] == "空调"
    assert rows[1]["product_type"] == ""
    assert not any(row["acknowledged"] for row in rows)
    assert database.acknowledge_gate_notice(
        "QingLong.Lin", rows[0]["gate_key"], rows[0]["planned_departure_at"]
    )
    refreshed = database.pending_gate_departures("2026-09-24", "qinglong.lin")
    assert refreshed[0]["acknowledged"] == 1
    assert database.pending_gate_departures("2026-09-24", "another.user")[0][
        "acknowledged"
    ] == 0
