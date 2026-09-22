from smartgpms.database import Database


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
