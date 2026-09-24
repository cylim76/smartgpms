from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    """SQLite repository for DAS metadata, OCR cache and print evidence."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cpm_records (
                    cpm_id TEXT PRIMARY KEY,
                    container_no TEXT NOT NULL,
                    begin_date TEXT NOT NULL DEFAULT '',
                    end_date TEXT NOT NULL DEFAULT '',
                    das_status_text TEXT NOT NULL DEFAULT '',
                    business_stage INTEGER NOT NULL DEFAULT 0 CHECK(business_stage BETWEEN 0 AND 4),
                    latest_stage_code TEXT NOT NULL DEFAULT '',
                    photo_count INTEGER NOT NULL DEFAULT 0,
                    downloaded_photo_count INTEGER NOT NULL DEFAULT 0,
                    is_valid INTEGER NOT NULL DEFAULT 1,
                    print_status INTEGER NOT NULL DEFAULT 0 CHECK(print_status IN (0,1)),
                    print_count INTEGER NOT NULL DEFAULT 0,
                    last_printed_at TEXT,
                    ocr_status TEXT NOT NULL DEFAULT 'pending',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_cpm_container ON cpm_records(container_no);
                CREATE INDEX IF NOT EXISTS idx_cpm_stage_print ON cpm_records(business_stage, print_status);

                CREATE TABLE IF NOT EXISTS photo_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cpm_id TEXT NOT NULL REFERENCES cpm_records(cpm_id) ON DELETE CASCADE,
                    step_code TEXT NOT NULL,
                    step_no INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    local_path TEXT NOT NULL DEFAULT '',
                    thumbnail_path TEXT NOT NULL DEFAULT '',
                    source_hash TEXT NOT NULL DEFAULT '',
                    downloaded_at TEXT,
                    cache_status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT NOT NULL DEFAULT '',
                    UNIQUE(cpm_id, source_url)
                );

                CREATE TABLE IF NOT EXISTS ocr_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cpm_id TEXT NOT NULL REFERENCES cpm_records(cpm_id) ON DELETE CASCADE,
                    photo_id INTEGER REFERENCES photo_cache(id) ON DELETE CASCADE,
                    target_type TEXT NOT NULL CHECK(target_type IN ('container','seal')),
                    observed_text TEXT NOT NULL DEFAULT '',
                    suggested_text TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    crop_path TEXT NOT NULL DEFAULT '',
                    rotation INTEGER NOT NULL DEFAULT 0,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    engine_version TEXT NOT NULL DEFAULT '',
                    model_version TEXT NOT NULL DEFAULT '',
                    preprocessing_version TEXT NOT NULL DEFAULT '',
                    source_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(cpm_id, photo_id, target_type, source_hash)
                );

                CREATE TABLE IF NOT EXISTS verification_jobs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    expected_container TEXT NOT NULL,
                    expected_seal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES verification_jobs(id),
                    decision TEXT NOT NULL,
                    operator TEXT NOT NULL,
                    note TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS print_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cpm_id TEXT NOT NULL REFERENCES cpm_records(cpm_id),
                    container_no TEXT NOT NULL,
                    gate_container_no TEXT NOT NULL,
                    gate_seal_no TEXT NOT NULL,
                    ocr_container_no TEXT NOT NULL,
                    ocr_seal_no TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    evidence_dir TEXT NOT NULL,
                    operator TEXT NOT NULL DEFAULT '',
                    print_kind TEXT NOT NULL CHECK(print_kind IN ('first','reprint')),
                    reprint_reason TEXT NOT NULL DEFAULT '',
                    das_result TEXT NOT NULL,
                    printed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_print_container ON print_history(container_no, printed_at);

                CREATE TABLE IF NOT EXISTS gate_passes (
                    gate_key TEXT PRIMARY KEY,
                    gate_pass_no TEXT NOT NULL,
                    sequence_no TEXT NOT NULL DEFAULT '',
                    application_date TEXT NOT NULL DEFAULT '',
                    gate_type TEXT NOT NULL DEFAULT '',
                    vendor_name TEXT NOT NULL DEFAULT '',
                    vehicle_no TEXT NOT NULL DEFAULT '',
                    returner TEXT NOT NULL DEFAULT '',
                    remark TEXT NOT NULL DEFAULT '',
                    container_no TEXT NOT NULL,
                    seal_no TEXT NOT NULL DEFAULT '',
                    return_quantity TEXT NOT NULL DEFAULT '',
                    process_status TEXT NOT NULL DEFAULT '',
                    planned_departure_at TEXT NOT NULL DEFAULT '',
                    actual_departure_at TEXT NOT NULL DEFAULT '',
                    status_url TEXT NOT NULL DEFAULT '',
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                    pdf_path TEXT NOT NULL DEFAULT '',
                    pdf_status TEXT NOT NULL DEFAULT 'pending',
                    pdf_generated_at TEXT,
                    pdf_cleaned_at TEXT,
                    pdf_error TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gate_container_date
                    ON gate_passes(container_no, planned_departure_at DESC);
                CREATE INDEX IF NOT EXISTS idx_gate_application_date
                    ON gate_passes(application_date);

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    status TEXT NOT NULL DEFAULT 'logged_out',
                    username TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL DEFAULT '',
                    checked_at TEXT,
                    expires_hint TEXT
                );
                INSERT OR IGNORE INTO session_state(id) VALUES (1);
                CREATE TABLE IF NOT EXISTS sync_state (
                    name TEXT PRIMARY KEY,
                    cursor_value TEXT NOT NULL DEFAULT '',
                    last_started_at TEXT,
                    last_succeeded_at TEXT,
                    last_error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS background_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    cpm_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    run_after TEXT NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_type, cpm_id, status)
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(cpm_records)")
            }
            if "archive_status" not in columns:
                connection.execute(
                    "ALTER TABLE cpm_records ADD COLUMN archive_status INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE cpm_records SET archive_status=business_stage"
                )
            if "product_type" not in columns:
                connection.execute(
                    "ALTER TABLE cpm_records ADD COLUMN product_type TEXT NOT NULL DEFAULT ''"
                )
            if "packing_type" not in columns:
                connection.execute(
                    "ALTER TABLE cpm_records ADD COLUMN packing_type TEXT NOT NULL DEFAULT ''"
                )
            if "das_process_status" not in columns:
                connection.execute(
                    "ALTER TABLE cpm_records ADD COLUMN das_process_status INTEGER NOT NULL DEFAULT 0"
                )
            if "downloaded_photo_count" not in columns:
                connection.execute(
                    "ALTER TABLE cpm_records ADD COLUMN downloaded_photo_count INTEGER NOT NULL DEFAULT 0"
                )
            photo_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(photo_cache)")
            }
            if "cleaned_at" not in photo_columns:
                connection.execute("ALTER TABLE photo_cache ADD COLUMN cleaned_at TEXT")
            if "thumbnail_path" not in photo_columns:
                connection.execute(
                    "ALTER TABLE photo_cache ADD COLUMN thumbnail_path TEXT NOT NULL DEFAULT ''"
                )
            ocr_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(ocr_cache)")
            }
            if "cache_status" not in ocr_columns:
                connection.execute(
                    "ALTER TABLE ocr_cache ADD COLUMN cache_status TEXT NOT NULL DEFAULT 'ready'"
                )
            if "cleaned_at" not in ocr_columns:
                connection.execute("ALTER TABLE ocr_cache ADD COLUMN cleaned_at TEXT")
            connection.execute(
                """UPDATE cpm_records SET ocr_status='pending'
                   WHERE cpm_id IN (
                       SELECT seal.cpm_id FROM ocr_cache seal
                       JOIN ocr_cache container
                         ON container.cpm_id=seal.cpm_id
                        AND container.photo_id=seal.photo_id
                        AND container.target_type='container'
                       WHERE seal.target_type='seal'
                   )"""
            )
            connection.execute(
                """DELETE FROM ocr_cache AS seal
                   WHERE seal.target_type='seal' AND EXISTS (
                       SELECT 1 FROM ocr_cache container
                       WHERE container.cpm_id=seal.cpm_id
                         AND container.photo_id=seal.photo_id
                         AND container.target_type='container'
                   )"""
            )
            connection.execute(
                """UPDATE cpm_records SET das_process_status=CASE
                       WHEN TRIM(das_status_text) LIKE '5.%' OR INSTR(das_status_text,'铅封确认')>0 THEN 5
                       WHEN TRIM(das_status_text) LIKE '4.%' THEN 4
                       WHEN TRIM(das_status_text) LIKE '3.%' THEN 3
                       WHEN TRIM(das_status_text) LIKE '2.%' THEN 2
                       WHEN TRIM(das_status_text) LIKE '1.%' THEN 1
                       ELSE das_process_status END
                   WHERE das_process_status=0"""
            )
            connection.execute(
                """UPDATE cpm_records SET latest_stage_code=CASE business_stage
                     WHEN 1 THEN 'U1' WHEN 2 THEN 'U2' WHEN 3 THEN 'U3' WHEN 4 THEN 'S1' ELSE '' END
                   WHERE latest_stage_code<>CASE business_stage
                     WHEN 1 THEN 'U1' WHEN 2 THEN 'U2' WHEN 3 THEN 'U3' WHEN 4 THEN 'S1' ELSE '' END"""
            )
            connection.execute(
                """UPDATE cpm_records
                   SET downloaded_photo_count=(
                       SELECT COUNT(*) FROM photo_cache p
                       WHERE p.cpm_id=cpm_records.cpm_id AND p.step_no=4
                         AND p.cache_status='ready' AND p.local_path<>''
                   )"""
            )
            photo_count_version = connection.execute(
                "SELECT value FROM app_settings WHERE key='stage4_photo_count_semantics'"
            ).fetchone()
            if photo_count_version is None:
                connection.execute(
                    """UPDATE cpm_records SET photo_count=(
                           SELECT COUNT(*) FROM photo_cache p
                           WHERE p.cpm_id=cpm_records.cpm_id AND p.step_no=4
                       )"""
                )
                connection.execute(
                    """INSERT INTO app_settings(key,value,updated_at)
                       VALUES('stage4_photo_count_semantics','1',?)""",
                    (now_text(),),
                )
            connection.execute(
                """UPDATE cpm_records
                   SET archive_status=business_stage
                   WHERE archive_status=5 AND downloaded_photo_count<3"""
            )
            timestamp = now_text()
            connection.execute(
                """UPDATE background_jobs
                   SET status='pending',run_after=?,last_error='程序上次退出时任务仍在运行，已自动恢复',updated_at=?
                   WHERE status='running'""",
                (timestamp, timestamp),
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        timestamp = now_text()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, timestamp),
            )

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def activate_ocr_pipeline(self, version: str) -> int:
        """Invalidate derived OCR once when preprocessing rules change."""
        timestamp = now_text()
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT value FROM app_settings WHERE key='ocr_pipeline_version'"
            ).fetchone()
            if current and str(current["value"]) == version:
                return 0
            cursor = connection.execute(
                """UPDATE cpm_records SET ocr_status='pending'
                   WHERE ocr_status IN ('ready','review','complete','processing','failed')"""
            )
            connection.execute(
                """INSERT INTO app_settings(key,value,updated_at) VALUES('ocr_pipeline_version',?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                (version, timestamp),
            )
        return int(cursor.rowcount)

    def session(self) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_state WHERE id=1"
            ).fetchone()
        return self._row(row) or {}

    def update_session(
        self, status: str, username: str = "", message: str = "", expires_hint: str = ""
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE session_state SET status=?,username=?,message=?,checked_at=?,expires_hint=? WHERE id=1",
                (status, username, message, now_text(), expires_hint),
            )

    def upsert_cpm(
        self, record: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        cpm_id = str(record["cpm_id"])
        previous = self.cpm_by_id(cpm_id)
        timestamp = now_text()
        values = (
            cpm_id,
            str(record.get("container_no", "")).upper(),
            str(record.get("begin_date", "")),
            str(record.get("end_date", "")),
            str(record.get("product_type", "")),
            str(record.get("packing_type", "")),
            str(record.get("das_status_text", "")),
            int(record.get("business_stage", 0)),
            int(record.get("das_process_status", 0)),
            int(record.get("archive_status", record.get("business_stage", 0))),
            str(record.get("latest_stage_code", "")),
            int(record.get("photo_count", 0)),
            int(bool(record.get("is_valid", True))),
            timestamp,
            timestamp,
            timestamp,
        )
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO cpm_records(
                       cpm_id,container_no,begin_date,end_date,product_type,packing_type,
                       das_status_text,business_stage,das_process_status,archive_status,
                       latest_stage_code,photo_count,is_valid,first_seen_at,last_seen_at,last_checked_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cpm_id) DO UPDATE SET
                       container_no=excluded.container_no, begin_date=excluded.begin_date,
                       end_date=excluded.end_date,
                       product_type=CASE WHEN excluded.product_type='' THEN cpm_records.product_type ELSE excluded.product_type END,
                       packing_type=CASE WHEN excluded.packing_type='' THEN cpm_records.packing_type ELSE excluded.packing_type END,
                       das_status_text=excluded.das_status_text,
                       business_stage=excluded.business_stage, latest_stage_code=excluded.latest_stage_code,
                       das_process_status=excluded.das_process_status,
                       archive_status=CASE WHEN cpm_records.archive_status=5 THEN 5 ELSE excluded.archive_status END,
                       photo_count=excluded.photo_count, is_valid=excluded.is_valid,
                       last_seen_at=excluded.last_seen_at, last_checked_at=excluded.last_checked_at""",
                values,
            )
        return previous, self.cpm_by_id(cpm_id) or {}

    def cpm_by_id(self, cpm_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cpm_records WHERE cpm_id=?", (cpm_id,)
            ).fetchone()
        return self._row(row)

    def latest_valid_cpm(self, container_no: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM cpm_records WHERE container_no=? AND is_valid=1
                   ORDER BY CASE WHEN photo_count>0 OR downloaded_photo_count>0 THEN 1 ELSE 0 END DESC,
                            CASE WHEN archive_status=5 OR das_process_status=5 THEN 1 ELSE 0 END DESC,
                            CAST(cpm_id AS INTEGER) DESC, cpm_id DESC LIMIT 1""",
                (container_no.upper(),),
            ).fetchone()
        return self._row(row)

    def upsert_gate_pass(
        self, record: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        gate_key = str(record["gate_key"])
        previous = self.gate_pass_by_key(gate_key)
        timestamp = now_text()
        fields = (
            gate_key,
            str(record.get("gate_pass_no", "")),
            str(record.get("sequence_no", "")),
            str(record.get("application_date", "")),
            str(record.get("gate_type", "")),
            str(record.get("vendor_name", "")),
            str(record.get("vehicle_no", "")),
            str(record.get("returner", "")),
            str(record.get("remark", "")),
            str(record.get("container_no", "")).upper(),
            str(record.get("seal_no", "")).upper(),
            str(record.get("return_quantity", "")),
            str(record.get("process_status", "")),
            str(record.get("planned_departure_at", "")),
            str(record.get("actual_departure_at", "")),
            str(record.get("status_url", "")),
            str(record.get("source_fingerprint", "")),
            timestamp,
            timestamp,
            timestamp,
        )
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO gate_passes(
                       gate_key,gate_pass_no,sequence_no,application_date,gate_type,
                       vendor_name,vehicle_no,returner,remark,container_no,seal_no,
                       return_quantity,process_status,planned_departure_at,
                       actual_departure_at,status_url,source_fingerprint,
                       first_seen_at,last_seen_at,last_checked_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(gate_key) DO UPDATE SET
                       gate_pass_no=excluded.gate_pass_no,
                       sequence_no=excluded.sequence_no,
                       application_date=excluded.application_date,
                       gate_type=excluded.gate_type,
                       vendor_name=excluded.vendor_name,
                       vehicle_no=excluded.vehicle_no,
                       returner=excluded.returner,
                       remark=excluded.remark,
                       container_no=excluded.container_no,
                       seal_no=excluded.seal_no,
                       return_quantity=excluded.return_quantity,
                       process_status=excluded.process_status,
                       planned_departure_at=excluded.planned_departure_at,
                       actual_departure_at=excluded.actual_departure_at,
                       status_url=excluded.status_url,
                       pdf_status=CASE
                           WHEN gate_passes.source_fingerprint<>excluded.source_fingerprint
                           THEN 'stale' ELSE gate_passes.pdf_status END,
                       source_fingerprint=excluded.source_fingerprint,
                       last_seen_at=excluded.last_seen_at,
                       last_checked_at=excluded.last_checked_at""",
                fields,
            )
        return previous, self.gate_pass_by_key(gate_key) or {}

    def gate_pass_by_key(self, gate_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM gate_passes WHERE gate_key=?", (gate_key,)
            ).fetchone()
        return self._row(row)

    def latest_gate_pass(self, container_no: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM gate_passes WHERE container_no=?
                   ORDER BY planned_departure_at DESC,
                            CASE WHEN sequence_no GLOB '[0-9]*' THEN CAST(sequence_no AS INTEGER) ELSE 0 END DESC,
                            gate_pass_no DESC LIMIT 1""",
                (container_no.upper(),),
            ).fetchone()
        return self._row(row)

    def mark_gate_pdf_ready(self, gate_key: str, path: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE gate_passes SET pdf_path=?,pdf_status='ready',
                       pdf_generated_at=?,pdf_cleaned_at=NULL,pdf_error=''
                   WHERE gate_key=?""",
                (path, now_text(), gate_key),
            )

    def mark_gate_pdf_failed(self, gate_key: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE gate_passes SET pdf_status='failed',pdf_error=? WHERE gate_key=?",
                (error[:500], gate_key),
            )

    def gate_pdf_cleanup_entries(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT gate_key,application_date,planned_departure_at,
                          pdf_path,pdf_generated_at
                   FROM gate_passes WHERE pdf_status='ready' AND pdf_path<>''
                   ORDER BY planned_departure_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_gate_pdf_cleaned(self, gate_key: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE gate_passes SET pdf_path='',pdf_status='cleaned',
                       pdf_cleaned_at=?,pdf_error='' WHERE gate_key=?""",
                (now_text(), gate_key),
            )

    def list_containers(self, container_numbers: list[str]) -> list[dict[str, Any]]:
        return [
            row
            for number in container_numbers
            if (row := self.latest_valid_cpm(number))
        ]

    def save_photo(self, cpm_id: str, photo: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO photo_cache(cpm_id,step_code,step_no,source_url,local_path,thumbnail_path,source_hash,downloaded_at,cache_status,error_message)
                   VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cpm_id,source_url) DO UPDATE SET
                     step_code=excluded.step_code,step_no=excluded.step_no,local_path=excluded.local_path,
                     thumbnail_path=excluded.thumbnail_path,
                     source_hash=excluded.source_hash,downloaded_at=excluded.downloaded_at,
                     cache_status=excluded.cache_status,error_message=excluded.error_message,
                     cleaned_at=NULL""",
                (
                    cpm_id,
                    photo.get("step_code", ""),
                    int(photo.get("step_no", 0)),
                    photo.get("source_url", ""),
                    photo.get("local_path", ""),
                    photo.get("thumbnail_path", ""),
                    photo.get("source_hash", ""),
                    photo.get("downloaded_at"),
                    photo.get("cache_status", "pending"),
                    photo.get("error_message", ""),
                ),
            )
            connection.execute(
                """UPDATE cpm_records SET downloaded_photo_count=(
                       SELECT COUNT(*) FROM photo_cache
                       WHERE cpm_id=? AND step_no=4
                         AND cache_status='ready' AND local_path<>''
                   ) WHERE cpm_id=?""",
                (cpm_id, cpm_id),
            )
            row = connection.execute(
                "SELECT * FROM photo_cache WHERE cpm_id=? AND source_url=?",
                (cpm_id, photo.get("source_url", "")),
            ).fetchone()
        return self._row(row) or {}

    def update_photo_inventory(
        self, cpm_id: str, available_count: int, downloaded_count: int
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE cpm_records
                   SET photo_count=?,downloaded_photo_count=?,last_checked_at=?
                   WHERE cpm_id=?""",
                (available_count, downloaded_count, now_text(), cpm_id),
            )

    def set_downloaded_photo_count(self, cpm_id: str, count: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE cpm_records SET downloaded_photo_count=? WHERE cpm_id=?",
                (count, cpm_id),
            )

    def photos_for_cpm(self, cpm_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM photo_cache WHERE cpm_id=? ORDER BY step_no,id",
                (cpm_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def legacy_stage4_filename_ids(self) -> list[str]:
        """Return cached stage-4 records still using the URL-derived S1 name."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT DISTINCT cpm_id,local_path FROM photo_cache
                   WHERE step_no=4 AND cache_status='ready' AND local_path<>''"""
            ).fetchall()
        return sorted(
            {
                str(row["cpm_id"])
                for row in rows
                if Path(str(row["local_path"])).name.upper().startswith("S1_")
            },
            key=lambda value: int(value) if value.isdigit() else 0,
            reverse=True,
        )

    def photo_by_id(self, cpm_id: str, photo_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM photo_cache WHERE cpm_id=? AND id=?",
                (cpm_id, photo_id),
            ).fetchone()
        return self._row(row)

    def stage4_cache_was_cleaned(self, cpm_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT 1 FROM photo_cache
                   WHERE cpm_id=? AND step_no=4 AND cache_status='cleaned' LIMIT 1""",
                (cpm_id,),
            ).fetchone()
        return row is not None

    def save_ocr(
        self, cpm_id: str, photo_id: int, target_type: str, result: dict[str, Any]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO ocr_cache(cpm_id,photo_id,target_type,observed_text,suggested_text,confidence,crop_path,
                       rotation,raw_json,engine_version,model_version,preprocessing_version,source_hash,created_at,cache_status,cleaned_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ready',NULL)
                   ON CONFLICT(cpm_id,photo_id,target_type,source_hash) DO UPDATE SET
                     observed_text=excluded.observed_text,suggested_text=excluded.suggested_text,
                     confidence=excluded.confidence,crop_path=excluded.crop_path,rotation=excluded.rotation,
                     raw_json=excluded.raw_json,created_at=excluded.created_at,
                     cache_status='ready',cleaned_at=NULL""",
                (
                    cpm_id,
                    photo_id,
                    target_type,
                    result.get("observed", ""),
                    result.get("suggested", ""),
                    float(result.get("confidence", 0)),
                    result.get("crop_path", ""),
                    int(result.get("rotation", 0)),
                    json.dumps(result, ensure_ascii=False),
                    result.get("engine_version", "rapidocr"),
                    result.get("model_version", "onnx"),
                    result.get("preprocessing_version", "v1"),
                    result.get("source_hash", ""),
                    now_text(),
                ),
            )

    def clear_ocr(self, cpm_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM ocr_cache WHERE cpm_id=?", (cpm_id,))

    def best_ocr(self, cpm_id: str, target_type: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM ocr_cache
                   WHERE cpm_id=? AND target_type=? AND cache_status='ready'
                   ORDER BY confidence DESC,created_at DESC LIMIT 1""",
                (cpm_id, target_type),
            ).fetchone()
        result = self._row(row)
        if result:
            try:
                result["raw"] = json.loads(result.get("raw_json") or "{}")
            except json.JSONDecodeError:
                result["raw"] = {}
        return result

    def cache_cleanup_entries(self) -> dict[str, list[dict[str, Any]] | set[str]]:
        with self.connect() as connection:
            photos = connection.execute(
                """SELECT p.id,p.cpm_id,p.local_path,p.thumbnail_path,p.downloaded_at,c.begin_date
                   FROM photo_cache p JOIN cpm_records c ON c.cpm_id=p.cpm_id
                   WHERE p.cache_status='ready' AND p.local_path<>''"""
            ).fetchall()
            crops = connection.execute(
                """SELECT id,cpm_id,crop_path,created_at FROM ocr_cache
                   WHERE cache_status='ready' AND crop_path<>''"""
            ).fetchall()
            active = connection.execute(
                """SELECT DISTINCT cpm_id FROM background_jobs
                   WHERE status IN ('pending','running')"""
            ).fetchall()
        return {
            "photos": [dict(row) for row in photos],
            "crops": [dict(row) for row in crops],
            "active_cpm_ids": {str(row["cpm_id"]) for row in active},
        }

    def mark_photo_cache_cleaned(self, photo_id: int, cpm_id: str) -> None:
        timestamp = now_text()
        with self.transaction() as connection:
            connection.execute(
                """UPDATE photo_cache
                   SET cache_status='cleaned',local_path='',thumbnail_path='',cleaned_at=?,error_message='retention_cleanup'
                   WHERE id=?""",
                (timestamp, photo_id),
            )
            connection.execute(
                """UPDATE cpm_records SET archive_status=0,last_checked_at=?,
                       downloaded_photo_count=(
                           SELECT COUNT(*) FROM photo_cache
                           WHERE cpm_id=? AND step_no=4
                             AND cache_status='ready' AND local_path<>''
                       ) WHERE cpm_id=?""",
                (timestamp, cpm_id, cpm_id),
            )

    def mark_ocr_cache_cleaned(self, ocr_id: int, cpm_id: str) -> None:
        timestamp = now_text()
        with self.transaction() as connection:
            connection.execute(
                """UPDATE ocr_cache
                   SET cache_status='cleaned',crop_path='',cleaned_at=? WHERE id=?""",
                (timestamp, ocr_id),
            )
            remaining = connection.execute(
                """SELECT 1 FROM ocr_cache
                   WHERE cpm_id=? AND cache_status='ready' LIMIT 1""",
                (cpm_id,),
            ).fetchone()
            if remaining is None:
                connection.execute(
                    "UPDATE cpm_records SET ocr_status='cleaned' WHERE cpm_id=?",
                    (cpm_id,),
                )

    def print_cleanup_entries(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id,evidence_dir,printed_at FROM print_history ORDER BY printed_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_print_history(self, history_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM print_history WHERE id=?", (history_id,))

    def verification_row(self, container_no: str) -> dict[str, Any] | None:
        record = self.latest_valid_cpm(container_no)
        if not record:
            return None
        record["container_ocr"] = self.best_ocr(record["cpm_id"], "container")
        record["seal_ocr"] = self.best_ocr(record["cpm_id"], "seal")
        return record

    def recent_cpm_ids(self, limit: int = 100) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT cpm_id FROM cpm_records ORDER BY last_seen_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [str(row["cpm_id"]) for row in rows]

    def numeric_cpm_records_desc(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM cpm_records
                   WHERE cpm_id NOT GLOB '*[^0-9]*'
                   ORDER BY CAST(cpm_id AS INTEGER) DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def ocr_backlog_ids(
        self, oldest_cpm_id: str | None = None, limit: int = 500
    ) -> list[str]:
        """Return recent completed CPM records that have not finished OCR yet."""
        parameters: list[Any] = []
        lower_bound = ""
        if oldest_cpm_id and str(oldest_cpm_id).isdigit():
            lower_bound = "AND CAST(c.cpm_id AS INTEGER)>=?"
            parameters.append(int(oldest_cpm_id))
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT c.cpm_id FROM cpm_records c
                    WHERE c.cpm_id GLOB '[0-9]*'
                      AND (c.das_process_status=5 OR c.business_stage=4)
                      AND c.ocr_status NOT IN ('ready','review','cleaned')
                      AND NOT EXISTS (
                        SELECT 1 FROM photo_cache p
                        WHERE p.cpm_id=c.cpm_id AND p.step_no=4
                          AND p.cache_status='cleaned'
                      )
                      {lower_bound}
                    ORDER BY CAST(c.cpm_id AS INTEGER) DESC
                    LIMIT ?""",
                parameters,
            ).fetchall()
        return [str(row["cpm_id"]) for row in rows]

    def max_numeric_cpm_id(self) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(CAST(cpm_id AS INTEGER)) AS value FROM cpm_records WHERE cpm_id NOT GLOB '*[^0-9]*'"
            ).fetchone()
        return int(row["value"]) if row and row["value"] is not None else None

    def cpm_record_count(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS value FROM cpm_records").fetchone()
        return int(row["value"] or 0)

    def cancel_pending_download_jobs(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE background_jobs SET status='cancelled',updated_at=?
                   WHERE job_type='download_ocr' AND status='pending'""",
                (now_text(),),
            )
        return int(cursor.rowcount)

    def mark_archive_status(self, cpm_id: str, status: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE cpm_records SET archive_status=?,last_checked_at=? WHERE cpm_id=?",
                (status, now_text(), cpm_id),
            )

    def stats(self) -> dict[str, int | None]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS records,
                          SUM(CASE WHEN business_stage=4 THEN 1 ELSE 0 END) AS stage4,
                          SUM(CASE WHEN das_process_status=5 THEN 1 ELSE 0 END) AS status5
                   FROM cpm_records"""
            ).fetchone()
            cleaned_photos = connection.execute(
                "SELECT COUNT(*) AS value FROM photo_cache WHERE cache_status='cleaned'"
            ).fetchone()
            cleaned_crops = connection.execute(
                "SELECT COUNT(*) AS value FROM ocr_cache WHERE cache_status='cleaned'"
            ).fetchone()
        return {
            "cpm_records": int(row["records"] or 0),
            "stage4_records": int(row["stage4"] or 0),
            "status5_records": int(row["status5"] or 0),
            "cleaned_photos": int(cleaned_photos["value"] or 0),
            "cleaned_crops": int(cleaned_crops["value"] or 0),
            "max_cpm_id": self.max_numeric_cpm_id(),
        }

    def remove_legacy_import_artifacts(self) -> int:
        """Remove the isolated, dependency-free CPM cluster imported by old builds."""
        if self.get_setting("legacy_mapping_imported") != "1":
            return 0
        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT CAST(cpm_id AS INTEGER) AS value FROM cpm_records
                   WHERE cpm_id NOT GLOB '*[^0-9]*' ORDER BY value"""
            ).fetchall()
            values = [int(row["value"]) for row in rows]
            gaps = [(upper - lower, upper) for lower, upper in pairwise(values)]
            largest_gap, boundary = max(gaps, default=(0, 0))
            removed = 0
            if largest_gap >= 10_000:
                connection.execute(
                    """DELETE FROM background_jobs
                       WHERE cpm_id GLOB '[0-9]*'
                         AND CAST(cpm_id AS INTEGER) < ?""",
                    (boundary,),
                )
                cursor = connection.execute(
                    """DELETE FROM cpm_records AS c
                       WHERE c.cpm_id NOT GLOB '*[^0-9]*'
                         AND CAST(c.cpm_id AS INTEGER) < ?
                         AND NOT EXISTS (SELECT 1 FROM photo_cache p WHERE p.cpm_id=c.cpm_id)
                         AND NOT EXISTS (SELECT 1 FROM ocr_cache o WHERE o.cpm_id=c.cpm_id)
                         AND NOT EXISTS (SELECT 1 FROM print_history h WHERE h.cpm_id=c.cpm_id)""",
                    (boundary,),
                )
                removed = int(cursor.rowcount)
            timestamp = now_text()
            connection.execute(
                """INSERT INTO app_settings(key,value,updated_at)
                   VALUES('legacy_independence_migrated','1',?)
                   ON CONFLICT(key) DO UPDATE SET value='1',updated_at=excluded.updated_at""",
                (timestamp,),
            )
            connection.execute(
                "DELETE FROM app_settings WHERE key='legacy_mapping_imported'"
            )
            remaining = connection.execute(
                "SELECT COUNT(*) AS value FROM cpm_records"
            ).fetchone()
            if int(remaining["value"] or 0) < 500:
                connection.execute(
                    "DELETE FROM app_settings WHERE key='cpm_initialized'"
                )
        return removed

    def update_ocr_status(self, cpm_id: str, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE cpm_records SET ocr_status=? WHERE cpm_id=?", (status, cpm_id)
            )

    def enqueue_job(self, job_type: str, cpm_id: str) -> bool:
        timestamp = now_text()
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT id FROM background_jobs WHERE job_type=? AND cpm_id=? AND status IN ('pending','running') LIMIT 1",
                (job_type, cpm_id),
            ).fetchone()
            if active:
                return False
            reusable = connection.execute(
                "SELECT id FROM background_jobs WHERE job_type=? AND cpm_id=? ORDER BY id DESC LIMIT 1",
                (job_type, cpm_id),
            ).fetchone()
            if reusable:
                connection.execute(
                    "DELETE FROM background_jobs WHERE job_type=? AND cpm_id=? AND id<>?",
                    (job_type, cpm_id, reusable["id"]),
                )
                connection.execute(
                    """UPDATE background_jobs SET status='pending',attempts=0,run_after=?,last_error='',updated_at=?
                       WHERE id=?""",
                    (timestamp, timestamp, reusable["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO background_jobs(job_type,cpm_id,status,run_after,created_at,updated_at) VALUES(?,?,'pending',?,?,?)",
                    (job_type, cpm_id, timestamp, timestamp, timestamp),
                )
        return True

    def cancel_pending_jobs_before(self, oldest_cpm_id: str) -> int:
        """Retire stale automatic work outside the current CPM snapshot window."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE background_jobs SET status='cancelled',updated_at=? "
                "WHERE status='pending' AND cpm_id GLOB '[0-9]*' "
                "AND CAST(cpm_id AS INTEGER) < ?",
                (now_text(), int(oldest_cpm_id)),
            )
        return int(cursor.rowcount)

    def save_job(
        self,
        job_id: str,
        expected_container: str,
        expected_seal: str,
        status: str,
        payload: dict,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO verification_jobs VALUES (?,?,?,?,?,?)",
                (
                    job_id,
                    now_text(),
                    expected_container,
                    expected_seal,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def save_decision(
        self, job_id: str, decision: str, operator: str, note: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO decisions(job_id,decision,operator,note,created_at) VALUES(?,?,?,?,?)",
                (job_id, decision, operator, note, now_text()),
            )

    def record_print(
        self,
        cpm_id: str,
        snapshot: dict[str, Any],
        operator: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        timestamp = now_text()
        with self.transaction() as connection:
            record = connection.execute(
                "SELECT * FROM cpm_records WHERE cpm_id=?", (cpm_id,)
            ).fetchone()
            if record is None:
                raise KeyError(f"unknown cpm_id: {cpm_id}")
            print_kind = "reprint" if int(record["print_status"]) else "first"
            connection.execute(
                """INSERT INTO print_history(
                       cpm_id,container_no,gate_container_no,gate_seal_no,ocr_container_no,ocr_seal_no,
                       verdict,snapshot_json,evidence_dir,operator,print_kind,reprint_reason,das_result,printed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    cpm_id,
                    record["container_no"],
                    snapshot.get("gate_container_no", ""),
                    snapshot.get("gate_seal_no", ""),
                    snapshot.get("ocr_container_no", ""),
                    snapshot.get("ocr_seal_no", ""),
                    snapshot.get("verdict", "review"),
                    json.dumps(snapshot, ensure_ascii=False),
                    snapshot.get("evidence_dir", ""),
                    operator,
                    print_kind,
                    reason,
                    snapshot.get("das_result", "success"),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE cpm_records SET print_status=1,print_count=print_count+1,last_printed_at=? WHERE cpm_id=?",
                (timestamp, cpm_id),
            )
        return {"print_kind": print_kind, "printed_at": timestamp}
