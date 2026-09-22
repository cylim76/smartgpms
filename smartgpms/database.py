from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
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
            connection.execute(
                """UPDATE cpm_records SET latest_stage_code=CASE business_stage
                     WHEN 1 THEN 'U1' WHEN 2 THEN 'U2' WHEN 3 THEN 'U3' WHEN 4 THEN 'S1' ELSE '' END
                   WHERE latest_stage_code<>CASE business_stage
                     WHEN 1 THEN 'U1' WHEN 2 THEN 'U2' WHEN 3 THEN 'U3' WHEN 4 THEN 'S1' ELSE '' END"""
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
            str(record.get("das_status_text", "")),
            int(record.get("business_stage", 0)),
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
                       cpm_id,container_no,begin_date,end_date,das_status_text,business_stage,
                       latest_stage_code,photo_count,is_valid,first_seen_at,last_seen_at,last_checked_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cpm_id) DO UPDATE SET
                       container_no=excluded.container_no, begin_date=excluded.begin_date,
                       end_date=excluded.end_date, das_status_text=excluded.das_status_text,
                       business_stage=excluded.business_stage, latest_stage_code=excluded.latest_stage_code,
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
                   ORDER BY CASE WHEN end_date='' THEN 1 ELSE 0 END DESC,
                            end_date DESC, begin_date DESC, business_stage DESC,
                            CAST(cpm_id AS INTEGER) DESC, cpm_id DESC LIMIT 1""",
                (container_no.upper(),),
            ).fetchone()
        return self._row(row)

    def list_containers(self, container_numbers: list[str]) -> list[dict[str, Any]]:
        return [
            row
            for number in container_numbers
            if (row := self.latest_valid_cpm(number))
        ]

    def save_photo(self, cpm_id: str, photo: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO photo_cache(cpm_id,step_code,step_no,source_url,local_path,source_hash,downloaded_at,cache_status,error_message)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cpm_id,source_url) DO UPDATE SET
                     step_code=excluded.step_code,step_no=excluded.step_no,local_path=excluded.local_path,
                     source_hash=excluded.source_hash,downloaded_at=excluded.downloaded_at,
                     cache_status=excluded.cache_status,error_message=excluded.error_message""",
                (
                    cpm_id,
                    photo.get("step_code", ""),
                    int(photo.get("step_no", 0)),
                    photo.get("source_url", ""),
                    photo.get("local_path", ""),
                    photo.get("source_hash", ""),
                    photo.get("downloaded_at"),
                    photo.get("cache_status", "pending"),
                    photo.get("error_message", ""),
                ),
            )
            row = connection.execute(
                "SELECT * FROM photo_cache WHERE cpm_id=? AND source_url=?",
                (cpm_id, photo.get("source_url", "")),
            ).fetchone()
        return self._row(row) or {}

    def photos_for_cpm(self, cpm_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM photo_cache WHERE cpm_id=? ORDER BY step_no,id",
                (cpm_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_ocr(
        self, cpm_id: str, photo_id: int, target_type: str, result: dict[str, Any]
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO ocr_cache(cpm_id,photo_id,target_type,observed_text,suggested_text,confidence,crop_path,
                       rotation,raw_json,engine_version,model_version,preprocessing_version,source_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cpm_id,photo_id,target_type,source_hash) DO UPDATE SET
                     observed_text=excluded.observed_text,suggested_text=excluded.suggested_text,
                     confidence=excluded.confidence,crop_path=excluded.crop_path,rotation=excluded.rotation,
                     raw_json=excluded.raw_json,created_at=excluded.created_at""",
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
                "SELECT * FROM ocr_cache WHERE cpm_id=? AND target_type=? ORDER BY confidence DESC,created_at DESC LIMIT 1",
                (cpm_id, target_type),
            ).fetchone()
        result = self._row(row)
        if result:
            try:
                result["raw"] = json.loads(result.get("raw_json") or "{}")
            except json.JSONDecodeError:
                result["raw"] = {}
        return result

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

    def max_numeric_cpm_id(self) -> int | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(CAST(cpm_id AS INTEGER)) AS value FROM cpm_records WHERE cpm_id NOT GLOB '*[^0-9]*'"
            ).fetchone()
        return int(row["value"]) if row and row["value"] is not None else None

    def stats(self) -> dict[str, int | None]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS records, SUM(CASE WHEN business_stage=4 THEN 1 ELSE 0 END) AS stage4 FROM cpm_records"
            ).fetchone()
        return {
            "cpm_records": int(row["records"] or 0),
            "stage4_records": int(row["stage4"] or 0),
            "max_cpm_id": self.max_numeric_cpm_id(),
        }

    def import_legacy_mappings(self, legacy_path: Path) -> int:
        """One-time bootstrap from das_photo; copies metadata only, never photo files."""
        if (
            not legacy_path.is_file()
            or self.get_setting("legacy_mapping_imported") == "1"
        ):
            return 0
        legacy = sqlite3.connect(f"file:{legacy_path.as_posix()}?mode=ro", uri=True)
        legacy.row_factory = sqlite3.Row
        try:
            rows = legacy.execute(
                """SELECT c.cpm_id,c.container_no,c.begin_date,c.end_date,c.status_text,c.file_count,
                          COALESCE(MAX(p.step_no),0) AS business_stage,
                          CASE COALESCE(MAX(p.step_no),0)
                            WHEN 1 THEN 'U1' WHEN 2 THEN 'U2' WHEN 3 THEN 'U3' WHEN 4 THEN 'S1' ELSE ''
                          END AS latest_stage_code,
                          COUNT(p.id) AS photo_count
                   FROM containers c LEFT JOIN photos p ON p.cpm_id=c.cpm_id
                   WHERE TRIM(c.container_no)<>'' GROUP BY c.cpm_id"""
            ).fetchall()
        finally:
            legacy.close()
        for row in rows:
            self.upsert_cpm(
                {
                    "cpm_id": row["cpm_id"],
                    "container_no": row["container_no"],
                    "begin_date": row["begin_date"],
                    "end_date": row["end_date"],
                    "das_status_text": row["status_text"],
                    "business_stage": row["business_stage"],
                    "latest_stage_code": row["latest_stage_code"],
                    "photo_count": row["photo_count"],
                    "is_valid": True,
                }
            )
        self.set_setting("legacy_mapping_imported", "1")
        return len(rows)

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
