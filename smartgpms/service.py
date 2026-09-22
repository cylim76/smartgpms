from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import AppConfig
from .credentials import CredentialStore
from .das_browser import DasBrowser, LoginRequired
from .database import Database, now_text
from .photo_pipeline import PhotoPipeline

LOGGER = logging.getLogger(__name__)


class SmartGPMSService:
    def __init__(
        self, config: AppConfig, database: Database, credentials: CredentialStore
    ):
        self.config = config
        self.database = database
        self.credentials = credentials
        self.browser = DasBrowser(config)
        self.photos = PhotoPipeline(database, self.browser, config.cache_dir)
        self._browser_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="smartgpms-browser"
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        legacy_database = (
            self.config.base_dir.parent
            / "das_photo"
            / "data"
            / "das_cpm_photos.sqlite3"
        )
        self.database.import_legacy_mappings(legacy_database)
        if not self.database.get_setting("cpm_sync_cursor"):
            latest = self.database.max_numeric_cpm_id()
            self.database.set_setting(
                "cpm_sync_cursor", str((latest + 1) if latest is not None else 2000)
            )
        self._thread = threading.Thread(
            target=self._loop, name="smartgpms-background", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._browser_call(self.browser.close)
        self._browser_executor.shutdown(wait=False)

    def _browser_call(self, function, *args, **kwargs):
        return self._browser_executor.submit(function, *args, **kwargs).result()

    def login(
        self, username: str, password: str, otp: str, remember: bool
    ) -> dict[str, Any]:
        self.database.update_session("logging_in", username, "正在登录")
        try:
            result = self._browser_call(self.browser.login, username, password, otp)
            if remember:
                self.credentials.save(username, password)
            else:
                self.credentials.clear()
            self.database.update_session("logged_in", username, "会话有效")
            return result
        except Exception as exc:
            self.database.update_session("logged_out", username, str(exc))
            raise

    def check_session(self) -> dict[str, Any]:
        result = self._browser_call(self.browser.check_session)
        previous = self.database.session()
        self.database.update_session(
            "logged_in" if result["logged_in"] else "logged_out",
            str(previous.get("username", "")),
            str(result.get("message", "")),
        )
        return result

    def sync_cpm(self, cpm_ids: list[str] | None = None) -> dict[str, int]:
        if not self._task_lock.acquire(blocking=False):
            return {"checked": 0, "found": 0, "queued": 0, "errors": 0}
        try:
            next_cursor: str | None = None
            if cpm_ids is None:
                ids, next_cursor = self._sync_candidates()
            else:
                ids = cpm_ids
            checked = found = queued = errors = 0
            session_lost = False
            try:
                self._browser_call(self.browser.refresh_das)
            except LoginRequired:
                self.database.update_session("logged_out", message="DAS 会话已失效")
                return {"checked": 0, "found": 0, "queued": 0, "errors": 1}
            for cpm_id in ids:
                checked += 1
                try:
                    detail = self._browser_call(
                        self.browser.fetch_cpm_detail, cpm_id, False
                    )
                except LoginRequired:
                    self.database.update_session("logged_out", message="DAS 会话已失效")
                    session_lost = True
                    break
                except Exception:
                    LOGGER.exception("CPM metadata sync failed cpm_id=%s", cpm_id)
                    errors += 1
                    break
                if not detail.get("container_no"):
                    continue
                found += 1
                previous, current = self.database.upsert_cpm(
                    {
                        **detail,
                        "das_status_text": detail.get("status_text", ""),
                        "photo_count": len(detail.get("photos", [])),
                        "is_valid": True,
                    }
                )
                transitioned = int(current.get("business_stage", 0)) == 4 and (
                    not previous or int(previous.get("business_stage", 0)) < 4
                )
                recent_unprinted = (
                    int(current.get("business_stage", 0)) == 4
                    and not int(current.get("print_status", 0))
                    and self._is_recent(current)
                )
                if transitioned or recent_unprinted:
                    queued += int(self.database.enqueue_job("download_ocr", cpm_id))
            if next_cursor and not session_lost and errors == 0:
                self.database.set_setting("cpm_sync_cursor", next_cursor)
            return {
                "checked": checked,
                "found": found,
                "queued": queued,
                "errors": errors,
            }
        finally:
            self._task_lock.release()

    def _sync_candidates(self) -> tuple[list[str], str]:
        cursor = int(self.database.get_setting("cpm_sync_cursor", "2000") or 2000)
        new_ids = [
            str(value) for value in range(cursor, cursor + self.config.sync_batch_size)
        ]
        recent = self.database.recent_cpm_ids(100)
        return list(dict.fromkeys(recent + new_ids)), str(
            cursor + self.config.sync_batch_size
        )

    @staticmethod
    def _is_recent(record: dict[str, Any]) -> bool:
        text = str(record.get("end_date") or record.get("begin_date") or "")
        current = datetime.now().astimezone()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                parsed = datetime.strptime(text[:19], fmt).replace(
                    tzinfo=current.tzinfo
                )
                return current - parsed <= timedelta(hours=24)
            except ValueError:
                continue
        return False

    def run_one_job(self) -> bool:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM background_jobs WHERE status='pending' AND run_after<=? ORDER BY id LIMIT 1",
                (now_text(),),
            ).fetchone()
            if not row:
                return False
            connection.execute(
                "UPDATE background_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                (now_text(), row["id"]),
            )
            job = dict(row)
        try:
            detail = self._browser_call(self.browser.fetch_cpm_detail, job["cpm_id"])
            self._browser_call(self.photos.process, job["cpm_id"], detail)
            status, error = "done", ""
        except Exception as exc:
            LOGGER.exception("background photo/OCR job failed cpm_id=%s", job["cpm_id"])
            attempts = int(job.get("attempts", 0)) + 1
            status, error = ("pending" if attempts < 4 else "failed"), str(exc)
        with self.database.connect() as connection:
            run_after = now_text()
            if status == "pending":
                delay = (60, 300, 900)[min(attempts - 1, 2)]
                run_after = (
                    datetime.now().astimezone() + timedelta(seconds=delay)
                ).isoformat(timespec="seconds")
            connection.execute(
                "UPDATE background_jobs SET status=?,last_error=?,run_after=?,updated_at=? WHERE id=?",
                (status, error, run_after, now_text(), job["id"]),
            )
        return True

    def prepare_container(self, container_no: str) -> dict[str, Any] | None:
        record = self.database.latest_valid_cpm(container_no)
        if not record:
            return None
        if record.get("ocr_status") != "ready":
            detail = self._browser_call(self.browser.fetch_cpm_detail, record["cpm_id"])
            self._browser_call(self.photos.process, record["cpm_id"], detail)
        return self.database.verification_row(container_no)

    def freeze_print_evidence(self, cpm_id: str, snapshot: dict[str, Any]) -> str:
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        target = self.config.evidence_dir / cpm_id / stamp
        target.mkdir(parents=True, exist_ok=False)
        for name, ocr in (
            ("container", snapshot.get("container_ocr")),
            ("seal", snapshot.get("seal_ocr")),
        ):
            source = Path(str((ocr or {}).get("crop_path", "")))
            if (
                source.is_file()
                and self.config.cache_dir.resolve() in source.resolve().parents
            ):
                shutil.copy2(
                    source, target / f"{name}{source.suffix.lower() or '.jpg'}"
                )
        (target / "snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return str(target)

    def _loop(self) -> None:
        next_check = next_sync = 0.0
        while not self._stop.wait(2):
            try:
                now = time.monotonic()
                session = self.database.session()
                if session.get("status") != "logged_in":
                    continue
                if now >= next_check:
                    self.check_session()
                    next_check = now + self.config.session_check_seconds
                if (
                    self.database.session().get("status") == "logged_in"
                    and now >= next_sync
                ):
                    self.sync_cpm()
                    next_sync = now + self.config.sync_interval_seconds
                self.run_one_job()
            except Exception:
                LOGGER.exception("smartGPMS background loop recovered from an error")
