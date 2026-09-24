from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import AppConfig
from .credentials import CredentialStore
from .das_browser import DasBrowser, LoginRequired
from .database import Database, now_text
from .photo_pipeline import PhotoPipeline
from .time_utils import business_now

LOGGER = logging.getLogger(__name__)
ACTIVITY_RETENTION_SECONDS = 2 * 60 * 60


class SmartGPMSService:
    def __init__(
        self, config: AppConfig, database: Database, credentials: CredentialStore
    ):
        self.config = config
        self.database = database
        self.credentials = credentials
        self.browser = DasBrowser(config)
        self.photos = PhotoPipeline(
            database, self.browser, config.cache_dir, config.evidence_dir
        )
        self._browser_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="smartgpms-browser"
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task_lock = threading.Lock()
        self._activity_lock = threading.Lock()
        self._activity: deque[dict[str, Any]] = deque(maxlen=100)
        self._activity_seq = 0
        self._startup_sync_pending = True
        self._background_resume_at = 0.0
        self._background_window_active: bool | None = None

    def log_activity(
        self,
        message: str,
        level: str = "info",
        source: str = "system",
        *,
        container_no: str = "",
        stage: str = "",
    ) -> None:
        message = " ".join(str(message).split())[:300]
        with self._activity_lock:
            logged_at = time.time()
            self._activity_seq += 1
            entry = {
                "id": self._activity_seq,
                "at": business_now().strftime("%H:%M:%S"),
                "logged_at": logged_at,
                "level": level,
                "source": source,
                "message": message,
            }
            if container_no:
                entry["container_no"] = container_no.strip().upper()
            if stage:
                entry["stage"] = stage
            self._activity.append(entry)
            self._discard_expired_activity(logged_at)

    def _discard_expired_activity(self, current_timestamp: float | None = None) -> None:
        cutoff = (current_timestamp or time.time()) - ACTIVITY_RETENTION_SECONDS
        while self._activity and float(self._activity[0]["logged_at"]) < cutoff:
            self._activity.popleft()

    def activity(self, after: int = 0) -> dict[str, Any]:
        with self._activity_lock:
            self._discard_expired_activity()
            entries = [item.copy() for item in self._activity if item["id"] > after]
            return {"entries": entries, "last_id": self._activity_seq}

    def note_interactive(self) -> None:
        """Give interactive verification priority over new background OCR work."""
        self._background_resume_at = time.monotonic() + 60

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        removed_legacy = self.database.remove_legacy_import_artifacts()
        if removed_legacy:
            self.log_activity(
                f"独立化迁移完成：已移除旧 das_photo 导入的 {removed_legacy} 个箱号",
                source="migration",
            )
        if not self.database.get_setting("cpm_sync_cursor"):
            latest = self.database.max_numeric_cpm_id()
            self.database.set_setting(
                "cpm_sync_cursor", str((latest + 1) if latest is not None else 2000)
            )
        if self.database.get_setting("das_photo_label_filename_migrated") != "1":
            legacy_ids = self.database.legacy_stage4_filename_ids()
            queued = sum(
                int(self.database.enqueue_job("download_ocr", cpm_id))
                for cpm_id in legacy_ids
            )
            self.database.set_setting("das_photo_label_filename_migrated", "1")
            if legacy_ids:
                self.log_activity(
                    f"已安排 {len(legacy_ids)} 个箱号迁移为 DAS F 编号文件名，"
                    f"新增后台任务 {queued} 个",
                    source="migration",
                )
        self._thread = threading.Thread(
            target=self._loop, name="smartgpms-background", daemon=True
        )
        self._thread.start()
        self.log_activity("smartGPMS 已启动，等待 SSO / DAS 会话")

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
        self.log_activity("正在登录 SSO，并建立 DAS 会话", source="login")
        try:
            result = self._browser_call(self.browser.login, username, password, otp)
            if remember:
                self.credentials.save(username, password)
            else:
                self.credentials.clear()
            self.database.update_session("logged_in", username, "会话有效")
            self._startup_sync_pending = True
            self._background_resume_at = time.monotonic() + 15
            if self.database.get_setting("cpm_initialized") == "1":
                message = "登录成功；后台巡检将在 07:00–23:59 自动运行"
            else:
                message = "登录成功；将在作业时段初始化最新 500 条监装记录"
            self.log_activity(message, source="login")
            return result
        except Exception as exc:
            self.database.update_session("logged_out", username, str(exc))
            self.log_activity(f"登录失败：{exc}", "error", "login")
            raise

    def sync_latest_window(self, limit: int | None = None) -> dict[str, Any]:
        """Discover new CPMIDs downward, then maintain only incomplete 30-day rows."""
        limit = limit or self.config.startup_snapshot_size
        if not self._task_lock.acquire(blocking=False):
            self.log_activity("监装数据同步已在运行，本次请求未重复启动", "warning", "sync")
            return self._sync_result(busy=True)
        try:
            existing_count = self.database.cpm_record_count()
            initialized = self.database.get_setting("cpm_initialized") == "1"
            if not initialized and existing_count >= limit:
                self.database.set_setting("cpm_initialized", "1")
                initialized = True
            initializing = not initialized
            mode = "首次初始化" if initializing else "日常同步"
            local_max = self.database.max_numeric_cpm_id()
            cutoff = (business_now() - timedelta(days=30)).date()
            self.log_activity(
                f"{mode}：正在读取 DAS 最新监装记录",
                source="sync",
            )
            listing_rows = self._browser_call(self.browser.cpm_snapshot, limit)
            latest = int(str(listing_rows[0]["cpm_id"]))
            listed = {str(row["cpm_id"]): row for row in listing_rows}
            counters = {
                "scanned": 0,
                "new": 0,
                "updated": 0,
                "skipped": 0,
                "downloads": 0,
                "errors": 0,
            }
            new_ids: set[str] = set()
            candidate = latest
            oldest = str(latest)
            max_checks = (
                limit * 3 if initializing else self.config.daily_scan_safety_limit
            )
            checked_ids = 0
            reached_date_boundary = False

            while checked_ids < max_checks:
                if initializing and existing_count + len(new_ids) >= limit:
                    break
                if not initializing and local_max is not None and candidate <= local_max:
                    break
                cpm_id = str(candidate)
                candidate -= 1
                checked_ids += 1
                oldest = cpm_id
                current = self.database.cpm_by_id(cpm_id)
                metadata = current or listed.get(cpm_id) or {}
                known_date = self._business_date(str(metadata.get("begin_date", "")))
                if known_date and known_date < cutoff:
                    reached_date_boundary = True
                    break
                if initializing and current:
                    continue
                try:
                    detail = self._browser_call(
                        self.browser.fetch_cpm_detail, cpm_id, False
                    )
                except LoginRequired:
                    raise
                except Exception as exc:  # noqa: BLE001 - one missing CPMID is expected
                    LOGGER.warning(
                        "CPM discovery failed cpm_id=%s: %s", cpm_id, exc
                    )
                    counters["errors"] += 1
                    continue
                if not detail.get("container_no"):
                    continue
                detail = self._merge_listing_metadata(detail, listed.get(cpm_id) or {})
                detail_date = self._business_date(str(detail.get("begin_date", "")))
                if detail_date and detail_date < cutoff:
                    reached_date_boundary = True
                    break
                counters["scanned"] += 1
                previous, saved = self.database.upsert_cpm(
                    self._sync_record(detail)
                )
                if previous is None:
                    counters["new"] += 1
                    new_ids.add(cpm_id)
                elif self._status_changed(previous, saved):
                    counters["updated"] += 1
                if self._schedule_photo_work(cpm_id, detail, saved):
                    counters["downloads"] += 1
                self._log_sync_progress(mode, counters)

            for current in self.database.numeric_cpm_records_desc():
                cpm_id = str(current["cpm_id"])
                if cpm_id in new_ids:
                    continue
                business_date = self._business_date(str(current.get("begin_date", "")))
                if business_date is None or business_date < cutoff:
                    continue
                counters["scanned"] += 1
                intentionally_cleaned = self.database.stage4_cache_was_cleaned(cpm_id)
                completed = int(current.get("das_process_status", 0)) >= 5
                files_complete = self._archive_files_complete(cpm_id)
                if completed and (files_complete or intentionally_cleaned):
                    if files_complete and current.get("ocr_status") not in {
                        "ready",
                        "review",
                        "cleaned",
                    }:
                        counters["downloads"] += int(
                            self.database.enqueue_job("download_ocr", cpm_id)
                        )
                    counters["skipped"] += 1
                    self._log_sync_progress(mode, counters)
                    continue
                if completed:
                    counters["downloads"] += int(
                        self.database.enqueue_job("download_ocr", cpm_id)
                    )
                    self._log_sync_progress(mode, counters)
                    continue
                try:
                    detail = self._browser_call(
                        self.browser.fetch_cpm_detail, cpm_id, False
                    )
                except LoginRequired:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep maintaining other boxes
                    LOGGER.warning(
                        "CPM maintenance failed cpm_id=%s: %s", cpm_id, exc
                    )
                    counters["errors"] += 1
                    continue
                if not detail.get("container_no"):
                    continue
                detail = self._merge_listing_metadata(detail, current)
                previous, saved = self.database.upsert_cpm(
                    self._sync_record(detail)
                )
                if previous and self._status_changed(previous, saved):
                    counters["updated"] += 1
                if self._schedule_photo_work(cpm_id, detail, saved):
                    counters["downloads"] += 1
                self._log_sync_progress(mode, counters)

            if initializing and (
                self.database.cpm_record_count() >= limit or reached_date_boundary
            ):
                self.database.set_setting("cpm_initialized", "1")
            self.database.set_setting("latest_cpm_id", str(latest))
            self.database.set_setting("snapshot_oldest_cpm_id", oldest)
            self.database.set_setting("cpm_sync_cursor", str(latest + 1))
            self.log_activity(
                f"{mode}完成：{self._sync_summary(counters)}",
                source="sync",
            )
            return self._sync_result(
                **counters,
                latest_cpm_id=str(latest),
                oldest_cpm_id=oldest,
            )
        except LoginRequired:
            self.database.update_session("logged_out", message="DAS 会话已失效")
            self.log_activity("监装数据同步停止：DAS 会话已失效", "error", "sync")
            raise
        except Exception as exc:
            self.log_activity(f"监装数据同步失败：{exc}", "error", "sync")
            raise
        finally:
            self._task_lock.release()

    @staticmethod
    def _sync_record(detail: dict[str, Any]) -> dict[str, Any]:
        return {
            **detail,
            "das_status_text": detail.get("status_text", ""),
            "photo_count": SmartGPMSService._stage4_photo_count(detail),
            "archive_status": int(detail.get("business_stage", 0)),
            "is_valid": True,
        }

    @staticmethod
    def _stage4_photo_count(detail: dict[str, Any]) -> int:
        return sum(
            int(photo.get("step_no", 0)) == 4
            for photo in detail.get("photos", [])
        )

    @staticmethod
    def _merge_listing_metadata(
        detail: dict[str, Any], metadata: dict[str, Any]
    ) -> dict[str, Any]:
        merged = dict(detail)
        for name in ("begin_date", "end_date", "product_type", "packing_type"):
            merged[name] = merged.get(name) or metadata.get(name, "")
        return merged

    @staticmethod
    def _status_changed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
        fields = ("business_stage", "das_process_status", "das_status_text")
        return any(str(previous.get(name, "")) != str(current.get(name, "")) for name in fields)

    def _schedule_photo_work(
        self, cpm_id: str, detail: dict[str, Any], record: dict[str, Any]
    ) -> bool:
        has_stage4 = any(
            int(photo.get("step_no", 0)) == 4 for photo in detail.get("photos", [])
        )
        process_complete = int(detail.get("das_process_status", 0)) >= 5
        if not process_complete and not has_stage4:
            return False
        if self.database.stage4_cache_was_cleaned(cpm_id):
            return False
        files_complete = self._archive_files_complete(cpm_id)
        if files_complete:
            self.database.mark_archive_status(cpm_id, 5)
        needs_ocr = record.get("ocr_status") not in {"ready", "review", "cleaned"}
        if files_complete and not needs_ocr:
            return False
        return self.database.enqueue_job("download_ocr", cpm_id)

    @staticmethod
    def _parsed_gate_datetime(value: str) -> datetime | None:
        text = value.strip()
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
            "%Y%m%d%H%M%S",
            "%Y%m%d",
        ):
            try:
                return datetime.strptime(text, fmt).replace(
                    tzinfo=business_now().tzinfo
                )
            except ValueError:
                continue
        return None

    @classmethod
    def _normalized_gate_date(cls, value: str) -> str:
        parsed = cls._parsed_gate_datetime(value)
        return parsed.strftime("%Y-%m-%d") if parsed else ""

    def pending_gate_departures(
        self, username: str, current: datetime | None = None
    ) -> list[dict[str, Any]]:
        current = current or business_now()
        rows = self.database.pending_gate_departures(
            current.strftime("%Y-%m-%d"), username
        )
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for row in rows:
            planned = self._parsed_gate_datetime(
                str(row.get("planned_departure_at", ""))
            )
            if planned is None or planned.date() != current.date():
                continue
            candidates.append((planned, row))
        candidates.sort(key=lambda item: item[0], reverse=True)
        output: list[dict[str, Any]] = []
        seen_containers: set[str] = set()
        recent_cutoff = current - timedelta(minutes=30)
        for planned, row in candidates:
            container_no = str(row.get("container_no", "")).strip().upper()
            if not container_no or container_no in seen_containers:
                continue
            seen_containers.add(container_no)
            output.append(
                {
                    "gate_key": str(row.get("gate_key", "")),
                    "container_no": container_no,
                    "product_type": str(row.get("product_type", "")).strip(),
                    "planned_departure_at": str(
                        row.get("planned_departure_at", "")
                    ),
                    "is_new": planned > recent_cutoff
                    and not bool(row.get("acknowledged")),
                }
            )
        return output

    @classmethod
    def _gate_record(cls, row: dict[str, Any]) -> dict[str, Any]:
        application_date = cls._normalized_gate_date(
            str(row.get("application_date", ""))
        )
        sequence_no = str(row.get("sequence_no", "")).strip()
        gate_pass_no = str(row.get("gate_pass_no", "")).strip().upper()
        gate_key = ":".join(
            (
                application_date.replace("-", "") or "unknown",
                sequence_no or gate_pass_no or "unknown",
                gate_pass_no or "unknown",
            )
        )
        tracked = {
            name: str(row.get(name, "")).strip()
            for name in (
                "gate_pass_no",
                "sequence_no",
                "application_date",
                "gate_type",
                "vendor_name",
                "vehicle_no",
                "returner",
                "remark",
                "container_no",
                "seal_no",
                "return_quantity",
                "process_status",
                "planned_departure_at",
                "actual_departure_at",
                "status_url",
            )
        }
        fingerprint = hashlib.sha256(
            json.dumps(tracked, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            **row,
            "gate_key": gate_key,
            "gate_pass_no": gate_pass_no,
            "sequence_no": sequence_no,
            "application_date": application_date,
            "container_no": str(row.get("container_no", "")).strip().upper(),
            "seal_no": str(row.get("seal_no", "")).strip().upper(),
            "source_fingerprint": fingerprint,
        }

    def sync_gate_passes(self, current: datetime | None = None) -> dict[str, int | bool]:
        """Synchronize gate passes planned for today and queue new/changed PDFs."""
        if not self._task_lock.acquire(blocking=False):
            return {"scanned": 0, "new": 0, "updated": 0, "queued": 0, "busy": True}
        current = current or business_now()
        date_value = current.strftime("%Y%m%d")
        today = current.strftime("%Y-%m-%d")
        scanned = new = updated = queued = 0
        try:
            self.log_activity("正在同步今天的通门证", source="gate_sync")
            rows = self._browser_call(self.browser.gate_pass_snapshot, date_value)
            for row in rows:
                planned_date = self._normalized_gate_date(
                    str(row.get("planned_departure_at", ""))
                )
                if planned_date != today:
                    continue
                record = self._gate_record(row)
                if not record["container_no"]:
                    continue
                scanned += 1
                previous, saved = self.database.upsert_gate_pass(record)
                changed = bool(
                    previous
                    and previous.get("source_fingerprint")
                    != saved.get("source_fingerprint")
                )
                if previous is None:
                    new += 1
                elif changed:
                    updated += 1
                pdf_path = Path(str(saved.get("pdf_path", "")))
                expected_pdf_path = self.gatepass_pdf_path(saved)
                needs_pdf = (
                    previous is None
                    or changed
                    or saved.get("pdf_status") != "ready"
                    or not pdf_path.is_file()
                    or pdf_path.resolve() != expected_pdf_path.resolve()
                )
                if needs_pdf:
                    queued += int(
                        self.database.enqueue_job("gate_pdf", str(saved["gate_key"]))
                    )
            self.database.set_setting("gate_sync_date", today)
            self.database.set_setting("gate_sync_completed_at", now_text())
            self.log_activity(
                f"今天的通门证同步完成：扫描 {scanned} 箱 / 新增 {new} 箱 / "
                f"更新 {updated} 箱 / PDF排队 {queued} 箱",
                source="gate_sync",
            )
            return {
                "scanned": scanned,
                "new": new,
                "updated": updated,
                "queued": queued,
                "busy": False,
            }
        except LoginRequired:
            self.database.update_session("logged_out", message="DAS 会话已失效")
            raise
        except Exception as exc:
            self.log_activity(f"通门证同步失败：{exc}", "error", "gate_sync")
            raise
        finally:
            self._task_lock.release()

    def save_gate_detail(self, gate: dict[str, Any]) -> dict[str, Any]:
        record = self._gate_record(gate)
        previous, saved = self.database.upsert_gate_pass(record)
        planned_date = self._normalized_gate_date(
            str(saved.get("planned_departure_at", ""))
        )
        today = business_now().strftime("%Y-%m-%d")
        pdf_path = Path(str(saved.get("pdf_path", "")))
        expected_pdf_path = self.gatepass_pdf_path(saved)
        changed = bool(
            previous
            and previous.get("source_fingerprint") != saved.get("source_fingerprint")
        )
        if planned_date == today and (
            previous is None
            or changed
            or saved.get("pdf_status") != "ready"
            or not pdf_path.is_file()
            or pdf_path.resolve() != expected_pdf_path.resolve()
        ):
            self.database.enqueue_job("gate_pdf", str(saved["gate_key"]))
        return saved

    def gatepass_pdf_path(self, gate: dict[str, Any]) -> Path:
        planned = self._parsed_gate_datetime(
            str(gate.get("planned_departure_at", ""))
        )
        if planned is None:
            planned = self._parsed_gate_datetime(
                str(gate.get("application_date", ""))
            ) or business_now()
        year = planned.strftime("%Y")
        month = planned.strftime("%m")
        prefix = planned.strftime("%Y%m%d%H%M%S")
        container = re.sub(r"[^A-Z0-9]", "", str(gate.get("container_no", "")).upper())
        pass_no = re.sub(r"[^A-Z0-9_-]", "", str(gate.get("gate_pass_no", "")).upper())
        filename = f"{prefix}_{container}_{pass_no or 'GATEPASS'}.pdf"
        return self.config.gatepass_dir / year / month / filename

    def ensure_gatepass_pdf(
        self, gate: dict[str, Any], *, allow_cached: bool = True
    ) -> tuple[dict[str, Any], Path, dict[str, Any]]:
        current = self.database.gate_pass_by_key(str(gate["gate_key"])) or gate
        cached = Path(str(current.get("pdf_path", "")))
        expected = self.gatepass_pdf_path(current)
        if (
            allow_cached
            and current.get("pdf_status") == "ready"
            and cached.is_file()
            and cached.resolve() == expected.resolve()
        ):
            return current, cached, {"ok": True, "message": "已读取本地通门证 PDF"}
        detail = self._browser_call(
            self.browser.open_gate_detail,
            str(current["container_no"]),
            str(current.get("gate_pass_no", "")),
        )
        saved = self.save_gate_detail(detail)
        target = self.gatepass_pdf_path(saved)
        temporary = target.with_name(f".{target.stem}.tmp.pdf")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        try:
            result = self._browser_call(self.browser.export_current_gate_pdf, temporary)
            temporary.replace(target)
            self.database.mark_gate_pdf_ready(str(saved["gate_key"]), str(target))
            self._remove_replaced_gate_pdf(cached, target)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            self.database.mark_gate_pdf_failed(str(saved["gate_key"]), str(exc))
            raise
        return self.database.gate_pass_by_key(str(saved["gate_key"])) or saved, target, result

    def _remove_replaced_gate_pdf(self, previous: Path, current: Path) -> None:
        if not str(previous) or previous.resolve() == current.resolve():
            return
        root = self.config.gatepass_dir.resolve()
        resolved = previous.resolve()
        if root in resolved.parents:
            try:
                resolved.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("Unable to remove replaced gate-pass PDF: %s", resolved)

    def cleanup_gatepass_pdfs(self, current: datetime | None = None) -> dict[str, int]:
        current = current or business_now()
        cutoff = (current - timedelta(days=self.config.gatepass_pdf_retention_days)).date()
        root = self.config.gatepass_dir.resolve()
        removed = released = 0
        for row in self.database.gate_pdf_cleanup_entries():
            basis = self._business_date(
                str(row.get("planned_departure_at", ""))
            ) or self._business_date(str(row.get("application_date", "")))
            if basis is None or basis >= cutoff:
                continue
            path = Path(str(row.get("pdf_path", ""))).resolve()
            if root in path.parents and path.is_file():
                released += path.stat().st_size
                path.unlink()
            self.database.mark_gate_pdf_cleaned(str(row["gate_key"]))
            removed += 1
        return {"gatepass_pdfs": removed, "released_bytes": released}

    @staticmethod
    def _sync_summary(counters: dict[str, int]) -> str:
        return (
            f"扫描 {counters['scanned']} 箱 / 新增 {counters['new']} 箱 / "
            f"更新 {counters['updated']} 箱 / 跳过完成 {counters['skipped']} 箱 / "
            f"后台处理排队 {counters['downloads']} 箱"
        )

    def _log_sync_progress(self, mode: str, counters: dict[str, int]) -> None:
        if counters["scanned"] and counters["scanned"] % 25 == 0:
            self.log_activity(
                f"{mode}：{self._sync_summary(counters)}", source="sync"
            )

    @staticmethod
    def _sync_result(
        *,
        scanned: int = 0,
        new: int = 0,
        updated: int = 0,
        skipped: int = 0,
        downloads: int = 0,
        errors: int = 0,
        latest_cpm_id: str = "",
        oldest_cpm_id: str = "",
        busy: bool = False,
    ) -> dict[str, Any]:
        return {
            "scanned": scanned,
            "new": new,
            "updated": updated,
            "skipped": skipped,
            "downloads": downloads,
            "errors": errors,
            "checked": scanned,
            "found": scanned - skipped,
            "queued": downloads,
            "latest_cpm_id": latest_cpm_id,
            "oldest_cpm_id": oldest_cpm_id,
            "busy": busy,
        }

    def _sync_latest_window_obsolete(
        self, limit: int | None = None
    ) -> dict[str, Any]:
        """Initialize 500 CPM records, then maintain the latest 500-record window."""
        limit = limit or self.config.startup_snapshot_size
        if not self._task_lock.acquire(blocking=False):
            self.log_activity("监装数据同步已在运行，本次请求未重复启动", "warning", "sync")
            return {"checked": 0, "found": 0, "queued": 0, "errors": 0, "busy": True}
        try:
            existing_count = self.database.cpm_record_count()
            initialized = self.database.get_setting("cpm_initialized") == "1"
            if not initialized and existing_count >= limit:
                self.database.set_setting("cpm_initialized", "1")
                initialized = True
            initializing = not initialized
            mode = "首次初始化" if initializing else "日常巡检"
            self.log_activity(
                f"{mode}：正在打开 DAS 照片列表获取最新监装记录", source="sync"
            )
            listing_rows = self._browser_call(self.browser.cpm_snapshot, limit)
            latest = int(str(listing_rows[0]["cpm_id"]))
            listed = {str(row["cpm_id"]): row for row in listing_rows}
            checked = found = new_records = queued = errors = 0
            candidate = latest
            oldest = str(latest)
            max_checks = (
                limit * 3 if initializing else self.config.daily_scan_safety_limit
            )
            cutoff = (business_now() - timedelta(days=30)).date()
            while checked < max_checks:
                if initializing and found >= limit:
                    break
                cpm_id = str(candidate)
                candidate -= 1
                checked += 1
                oldest = cpm_id
                current = self.database.cpm_by_id(cpm_id)
                known_date = self._business_date(
                    str((current or listed.get(cpm_id) or {}).get("begin_date", ""))
                )
                if not initializing and known_date and known_date < cutoff:
                    self.log_activity(
                        f"日常巡检到达30天边界：开始日期 {known_date}",
                        source="sync",
                    )
                    break
                if (
                    not initializing
                    and current
                    and int(current.get("archive_status", 0)) == 5
                    and self._archive_files_complete(cpm_id)
                ):
                    found += 1
                    continue
                if current and int(current.get("archive_status", 0)) == 5:
                    self.database.mark_archive_status(
                        cpm_id, int(current.get("business_stage", 0))
                    )
                    self.log_activity(
                        "发现已完成记录的本地照片缺失，已安排修复", "warning", "sync"
                    )
                try:
                    detail = self._browser_call(
                        self.browser.fetch_cpm_detail, cpm_id, False
                    )
                except LoginRequired:
                    raise
                except Exception as exc:  # noqa: BLE001 - skip one malformed/missing CPM record
                    LOGGER.warning(
                        "CPM metadata fetch failed cpm_id=%s: %s", cpm_id, exc
                    )
                    errors += 1
                    continue
                if not detail.get("container_no"):
                    continue
                list_metadata = listed.get(cpm_id) or {}
                detail["begin_date"] = detail.get("begin_date") or list_metadata.get(
                    "begin_date", ""
                )
                detail["end_date"] = detail.get("end_date") or list_metadata.get(
                    "end_date", ""
                )
                detail["product_type"] = detail.get(
                    "product_type"
                ) or list_metadata.get("product_type", "")
                detail["packing_type"] = detail.get(
                    "packing_type"
                ) or list_metadata.get("packing_type", "")
                detail_date = self._business_date(str(detail.get("begin_date", "")))
                if not initializing and detail_date and detail_date < cutoff:
                    self.log_activity(
                        f"日常巡检到达30天边界：开始日期 {detail_date}",
                        source="sync",
                    )
                    break
                found += 1
                previous, current = self.database.upsert_cpm(
                    {
                        **detail,
                        "das_status_text": detail.get("status_text", ""),
                        "photo_count": self._stage4_photo_count(detail),
                        "archive_status": int(detail.get("business_stage", 0)),
                        "is_valid": True,
                    }
                )
                new_records += int(previous is None)
                has_stage4 = any(
                    int(photo.get("step_no", 0)) == 4
                    for photo in detail.get("photos", [])
                )
                process_complete = int(detail.get("das_process_status", 0)) == 5
                if process_complete or has_stage4:
                    if self._archive_files_complete(cpm_id):
                        self.database.mark_archive_status(cpm_id, 5)
                    elif self.database.stage4_cache_was_cleaned(cpm_id):
                        self.log_activity(
                            "该监装记录的照片已按保留策略清理，仅在人工核验时重新下载",
                            source="cleanup",
                        )
                    else:
                        queued += int(
                            self.database.enqueue_job("download_ocr", cpm_id)
                        )
                if checked % 25 == 0:
                    self.log_activity(
                        f"{mode}已检查 {checked} 箱，有效 {found} 箱，新增 {new_records} 箱，照片排队 {queued} 箱",
                        source="sync",
                    )
            latest_text = str(latest)
            self.database.cancel_pending_jobs_before(oldest)
            queued += self._queue_ocr_backlog(oldest)
            if initializing and found >= limit:
                self.database.set_setting("cpm_initialized", "1")
            self.database.set_setting("latest_cpm_id", latest_text)
            self.database.set_setting("snapshot_oldest_cpm_id", oldest)
            self.database.set_setting("cpm_sync_cursor", str(latest + 1))
            self.log_activity(
                f"{mode}完成：{latest_text} 至 {oldest}，检查 {checked}，有效 {found}，待归档照片排队 {queued}",
                source="sync",
            )
            return {
                "checked": checked,
                "found": found,
                "queued": queued,
                "errors": errors,
                "latest_cpm_id": latest_text,
                "oldest_cpm_id": oldest,
            }
        except LoginRequired:
            self.database.update_session("logged_out", message="DAS 会话已失效")
            self.log_activity("监装数据同步停止：DAS 会话已失效", "error", "sync")
            raise
        except Exception as exc:
            self.log_activity(f"监装数据同步失败：{exc}", "error", "sync")
            raise
        finally:
            self._task_lock.release()

    def _archive_files_complete(self, cpm_id: str) -> bool:
        photos = [
            photo
            for photo in self.database.photos_for_cpm(cpm_id)
            if int(photo.get("step_no", 0)) == 4
        ]
        ready = [
            photo
            for photo in photos
            if photo.get("cache_status") == "ready"
            and Path(str(photo.get("local_path", ""))).is_file()
        ]
        self.database.set_downloaded_photo_count(cpm_id, len(ready))
        return len(ready) >= 3

    @staticmethod
    def _business_date(value: str):
        text = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(text[:19], fmt).replace(
                    tzinfo=business_now().tzinfo
                ).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _background_window_open(current: datetime | None = None) -> bool:
        current = current or business_now()
        return 7 <= current.hour <= 23

    def _next_cache_cleanup(self, current: datetime | None = None) -> datetime:
        current = current or business_now()
        scheduled = current.replace(
            hour=self.config.cache_cleanup_hour,
            minute=self.config.cache_cleanup_minute,
            second=0,
            microsecond=0,
        )
        if scheduled <= current:
            scheduled += timedelta(days=1)
        return scheduled

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
                        "photo_count": self._stage4_photo_count(detail),
                        "is_valid": True,
                    }
                )
                transitioned = int(current.get("das_process_status", 0)) == 5 and (
                    not previous or int(previous.get("das_process_status", 0)) < 5
                )
                recent_unprinted = (
                    int(current.get("das_process_status", 0)) == 5
                    and not int(current.get("print_status", 0))
                    and self._is_recent(current)
                )
                eligible_for_ocr = (
                    int(current.get("das_process_status", 0)) == 5
                    or int(current.get("business_stage", 0)) == 4
                ) and current.get("ocr_status") not in {"ready", "review", "cleaned"}
                if transitioned or recent_unprinted or eligible_for_ocr:
                    queued += int(self.database.enqueue_job("download_ocr", cpm_id))
            queued += self._queue_ocr_backlog(
                self.database.get_setting("snapshot_oldest_cpm_id") or None
            )
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

    def _queue_ocr_backlog(self, oldest_cpm_id: str | None = None) -> int:
        queued = 0
        for cpm_id in self.database.ocr_backlog_ids(
            oldest_cpm_id, self.config.startup_snapshot_size
        ):
            queued += int(self.database.enqueue_job("download_ocr", cpm_id))
        if queued:
            self.log_activity(
                f"已安排 {queued} 个箱号在后台空闲时预先下载、裁剪并 OCR",
                source="ocr",
            )
        return queued

    @staticmethod
    def _is_recent(record: dict[str, Any]) -> bool:
        text = str(record.get("end_date") or record.get("begin_date") or "")
        current = business_now()
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
                "SELECT * FROM background_jobs WHERE status='pending' AND run_after<=? "
                "ORDER BY CASE WHEN job_type='gate_pdf' THEN 0 ELSE 1 END, "
                "CASE WHEN cpm_id GLOB '[0-9]*' THEN CAST(cpm_id AS INTEGER) ELSE 0 END DESC, "
                "id DESC LIMIT 1",
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
            if job["job_type"] == "gate_pdf":
                gate = self.database.gate_pass_by_key(str(job["cpm_id"]))
                if not gate:
                    raise RuntimeError("通门证记录已不存在")
                planned_date = self._normalized_gate_date(
                    str(gate.get("planned_departure_at", ""))
                )
                today = business_now().strftime("%Y-%m-%d")
                if planned_date == today:
                    self.log_activity(
                        f"{gate['container_no']}：后台生成通门证 PDF",
                        source="gate_pdf",
                        container_no=str(gate["container_no"]),
                    )
                    _saved, _path, _result = self.ensure_gatepass_pdf(
                        gate, allow_cached=False
                    )
                    self.log_activity(
                        f"{gate['container_no']}：通门证 PDF 已缓存",
                        source="gate_pdf",
                        container_no=str(gate["container_no"]),
                    )
            else:
                self.log_activity("后台正在处理监装照片与文字识别", source="ocr")
                detail = self._browser_call(
                    self.browser.fetch_cpm_detail, job["cpm_id"]
                )
                if int(detail.get("das_process_status", 0)) == 5 and not detail.get(
                    "photos"
                ):
                    raise RuntimeError("DAS 照片页面状态为 5，但详情暂未返回照片")
                result = self._browser_call(self.photos.process, job["cpm_id"], detail)
                self.log_activity(
                    "监装照片下载与文字识别完成："
                    f"原图 {int(result.get('downloaded_photo_count', 0))} 张",
                    source="ocr",
                )
            status, error = "done", ""
        except Exception as exc:
            LOGGER.exception(
                "background job failed type=%s key=%s",
                job["job_type"],
                job["cpm_id"],
            )
            attempts = int(job.get("attempts", 0)) + 1
            status, error = ("pending" if attempts < 4 else "failed"), str(exc)
            if job["job_type"] == "gate_pdf":
                self.log_activity(
                    f"后台通门证 PDF 生成失败：{exc}", "error", "gate_pdf"
                )
            else:
                self.log_activity(f"后台照片文字识别失败：{exc}", "error", "ocr")
        with self.database.connect() as connection:
            run_after = now_text()
            if status == "pending":
                delay = (60, 300, 900)[min(attempts - 1, 2)]
                run_after = (
                    business_now() + timedelta(seconds=delay)
                ).isoformat(timespec="seconds")
            connection.execute(
                "UPDATE background_jobs SET status=?,last_error=?,run_after=?,updated_at=? WHERE id=?",
                (status, error, run_after, now_text(), job["id"]),
            )
        return True

    def prepare_container(self, container_no: str) -> dict[str, Any] | None:
        self.log_activity(
            f"{container_no}：查询本地监装记录",
            source="verify",
            container_no=container_no,
            stage="mapping",
        )
        record = self.database.latest_valid_cpm(container_no)
        if not record:
            return None
        if record.get("ocr_status") not in {
            "ready",
            "review",
        } or not self._archive_files_complete(str(record["cpm_id"])):
            self.log_activity(
                f"{container_no}：查询 DAS 四阶段照片",
                source="verify",
                container_no=container_no,
                stage="photo_query",
            )
            detail = self._browser_call(self.browser.fetch_cpm_detail, record["cpm_id"])
            self._browser_call(
                self.photos.process,
                record["cpm_id"],
                detail,
                lambda stage, label: self.log_activity(
                    f"{container_no}：{label}",
                    source="verify",
                    container_no=container_no,
                    stage=stage,
                ),
            )
        else:
            self.log_activity(
                f"{container_no}：读取本地 OCR 缓存",
                source="verify",
                container_no=container_no,
                stage="ocr_cache",
            )
        self.log_activity(
            f"{container_no}：照片识别完成",
            source="verify",
            container_no=container_no,
            stage="ocr_done",
        )
        return self.database.verification_row(container_no)

    def resolve_container(self, container_no: str) -> dict[str, Any]:
        """Resolve a missing container through the DAS photo-download search page."""
        wanted = container_no.strip().upper()
        self.log_activity(
            f"{wanted}：本地无记录，正在查询 DAS 照片页面",
            source="verify",
            container_no=wanted,
            stage="photo_query",
        )
        record = self.database.latest_valid_cpm(wanted)
        if record:
            return {
                "record": record,
                "gate": None,
                "gate_status": "unknown",
                "checked": 0,
            }
        try:
            candidates = self._browser_call(
                self.browser.find_cpm_by_container, wanted
            )
        except LoginRequired:
            raise
        except Exception as exc:
            LOGGER.exception("Photo-page CPM lookup failed container=%s", wanted)
            return {
                "record": None,
                "gate": None,
                "gate_status": "unknown",
                "checked": 0,
                "message": f"DAS 照片页面查询失败：{exc}",
            }
        if not candidates:
            return {
                "record": None,
                "gate": None,
                "gate_status": "unknown",
                "checked": 0,
                "message": "DAS 照片页面未找到对应箱号记录",
            }

        resolved: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
        for metadata in candidates:
            cpm_id = str(metadata.get("cpm_id", ""))
            if not cpm_id:
                continue
            try:
                detail = self._browser_call(
                    self.browser.fetch_cpm_detail, cpm_id, False
                )
            except LoginRequired:
                raise
            except Exception:
                LOGGER.exception(
                    "Photo detail lookup failed container=%s cpm_id=%s",
                    wanted,
                    cpm_id,
                )
                continue
            if str(detail.get("container_no", "")).strip().upper() != wanted:
                continue
            detail = self._merge_listing_metadata(detail, metadata)
            _previous, current = self.database.upsert_cpm(
                self._sync_record(detail)
            )
            stage4_count = self._stage4_photo_count(detail)
            process_complete = int(detail.get("das_process_status", 0)) >= 5
            numeric_id = int(cpm_id) if cpm_id.isdigit() else 0
            resolved.append(
                (
                    (
                        int(stage4_count > 0),
                        int(process_complete),
                        numeric_id,
                    ),
                    current,
                )
            )
        if not resolved:
            return {
                "record": None,
                "gate": None,
                "gate_status": "unknown",
                "checked": len(candidates),
                "message": "已找到监装记录，但照片详情读取失败，请稍后重试",
            }
        _rank, selected = max(resolved, key=lambda item: item[0])
        self.log_activity(
            f"{wanted}：已找到监装照片记录",
            source="verify",
            container_no=wanted,
            stage="mapping",
        )
        return {
            "record": selected,
            "gate": None,
            "gate_status": "unknown",
            "checked": len(candidates),
        }

    def freeze_print_evidence(self, cpm_id: str, snapshot: dict[str, Any]) -> str:
        stamp = business_now().strftime("%Y%m%d_%H%M%S_%f")
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
        next_cleanup = self._next_cache_cleanup()
        self.log_activity(
            f"缓存自动清理已计划于 {next_cleanup.strftime('%Y-%m-%d %H:%M')}",
            source="cleanup",
        )
        while not self._stop.wait(2):
            try:
                now = time.monotonic()
                wall_now = business_now()
                if wall_now >= next_cleanup:
                    next_cleanup = self._next_cache_cleanup(
                        wall_now + timedelta(minutes=1)
                    )
                    cleanup = self._browser_call(
                        self.photos.cleanup_cache,
                        photo_retention_days=self.config.photo_retention_days,
                        crop_retention_days=self.config.crop_retention_days,
                        print_retention_days=self.config.print_evidence_retention_days,
                        cache_max_bytes=self.config.cache_max_bytes,
                        min_free_bytes=self.config.cache_min_free_bytes,
                    )
                    gate_cleanup = self.cleanup_gatepass_pdfs(wall_now)
                    cleanup["gatepass_pdfs"] = gate_cleanup["gatepass_pdfs"]
                    cleanup["released_bytes"] += gate_cleanup["released_bytes"]
                    if any(cleanup.values()):
                        released_mb = cleanup["released_bytes"] / (1024 * 1024)
                        self.log_activity(
                            "缓存清理完成："
                            f"原图 {cleanup['photos']}，裁剪图 {cleanup['crops']}，"
                            f"打印记录 {cleanup['print_records']}，"
                            f"通门证PDF {cleanup['gatepass_pdfs']}，"
                            f"中间目录 {cleanup['intermediate_dirs']}，"
                            f"释放 {released_mb:.1f} MB",
                            source="cleanup",
                        )
                session = self.database.session()
                if session.get("status") != "logged_in":
                    continue
                if now >= next_check:
                    self.check_session()
                    next_check = now + self.config.session_check_seconds
                window_open = self._background_window_open()
                if window_open != self._background_window_active:
                    self._background_window_active = window_open
                    self.log_activity(
                        (
                            "已进入后台作业时段（07:00–23:59）"
                            if window_open
                            else "当前为后台休眠时段（00:00–06:59），暂停同步与照片下载"
                        ),
                        source="schedule",
                    )
                if not window_open:
                    continue
                if (
                    self.database.session().get("status") == "logged_in"
                    and (self._startup_sync_pending or now >= next_sync)
                ):
                    self._startup_sync_pending = False
                    self.sync_gate_passes(wall_now)
                    self.sync_latest_window()
                    next_sync = time.monotonic() + self.config.sync_interval_seconds
                if (
                    self.database.session().get("status") == "logged_in"
                    and now >= self._background_resume_at
                ):
                    self.run_one_job()
            except Exception:
                LOGGER.exception("smartGPMS background loop recovered from an error")
