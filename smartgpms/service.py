from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
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
INITIAL_IMPORT_DAYS = 30


class ServiceStopping(RuntimeError):
    """Raised when new interactive work arrives during graceful shutdown."""


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
        self._ocr_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="smartgpms-ocr"
        )
        self._ocr_job_lock = threading.Lock()
        self._ocr_jobs_inflight: set[int] = set()
        self._ocr_pipeline_capacity = 2
        self._stop = threading.Event()
        self._drain_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._task_lock = threading.Lock()
        self._manual_refresh_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._foreground_condition = threading.Condition()
        self._foreground_tasks = 0
        self._closed = False
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
        self._drain_requested.clear()
        self._closed = False
        self._thread = threading.Thread(
            target=self._loop, name="smartgpms-background", daemon=True
        )
        self._thread.start()
        self.log_activity("smartGPMS 已启动，等待 SSO / DAS 会话")

    @property
    def is_draining(self) -> bool:
        return self._drain_requested.is_set()

    def run_foreground(self, function, *args, **kwargs):
        """Run one user request while making graceful shutdown wait for it."""
        with self._foreground_condition:
            if self._drain_requested.is_set():
                raise ServiceStopping("smartGPMS 正在安全退出，请重新启动后再操作")
            self._foreground_tasks += 1
        try:
            return function(*args, **kwargs)
        finally:
            with self._foreground_condition:
                self._foreground_tasks -= 1
                self._foreground_condition.notify_all()

    def request_shutdown(self) -> None:
        """Stop accepting new work; current foreground/background unit may finish."""
        if self._drain_requested.is_set():
            return
        self._drain_requested.set()
        self.log_activity(
            "桌面窗口已关闭；正在完成当前箱号后安全退出",
            source="shutdown",
        )

    def drain_and_stop(self, timeout: float = 300.0) -> None:
        """Drain the current work unit, close DAS/SSO, then stop the service."""
        self.request_shutdown()
        deadline = time.monotonic() + timeout
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._foreground_condition:
            while self._foreground_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    LOGGER.warning(
                        "Graceful shutdown timed out with %s foreground task(s)",
                        self._foreground_tasks,
                    )
                    break
                self._foreground_condition.wait(timeout=remaining)
        self.stop()

    def stop(self) -> None:
        with self._stop_lock:
            if self._closed:
                return
            self._closed = True
        self._drain_requested.set()
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        try:
            self._browser_call(self.browser.close)
        finally:
            self._browser_executor.shutdown(wait=True)
            self._ocr_executor.shutdown(wait=True)
        previous = self.database.session()
        self.database.update_session(
            "logged_out",
            str(previous.get("username", "")),
            "smartGPMS 服务已安全关闭",
        )

    def _browser_call(self, function, *args, **kwargs):
        return self._browser_executor.submit(function, *args, **kwargs).result()

    def _ocr_call(self, function, *args, **kwargs):
        return self._ocr_executor.submit(function, *args, **kwargs).result()

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
            initial_import = self.initial_import_state()
            self._startup_sync_pending = not bool(initial_import["required"])
            self._background_resume_at = time.monotonic() + 15
            if initial_import["required"]:
                message = "登录成功；请确认首次导入最近30天数据"
            elif self.database.get_setting("cpm_initialized") == "1":
                message = "登录成功；后台巡检将在 07:00–23:59 自动运行"
            else:
                message = "登录成功；即将初始化最近30天监装及门证数据"
            self.log_activity(message, source="login")
            return {**result, "initial_import": initial_import}
        except Exception as exc:
            self.database.update_session("logged_out", username, str(exc))
            self.log_activity(f"登录失败：{exc}", "error", "login")
            raise

    def initial_import_state(self) -> dict[str, int | bool]:
        """Describe whether an empty installation still needs user confirmation."""
        saved = self.database.get_setting("initial_import_confirmed").strip()
        required = self.database.cpm_record_count() == 0 and not saved
        return {
            "required": required,
            "configured": bool(saved),
            "days": INITIAL_IMPORT_DAYS,
        }

    def configure_initial_import(self, _target: int | None = None) -> dict[str, int | bool]:
        """Confirm the one-time 30-day import and release initialization."""
        state = self.initial_import_state()
        if not state["required"]:
            return state
        self.database.set_setting("initial_import_confirmed", "1")
        self.database.set_setting("cpm_initialized", "0")
        self._startup_sync_pending = True
        self._background_resume_at = time.monotonic()
        self.log_activity(
            "已确认首次导入最近30天数据，后台初始化即将开始",
            source="sync",
        )
        return self.initial_import_state()

    def sync_latest_window(
        self,
        gate_checkpoint: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Synchronize 30 days of exported metadata, then queue changed status-5 rows."""
        return self._sync_bulk_window(gate_checkpoint=gate_checkpoint)

    def _sync_bulk_window(
        self, gate_checkpoint: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        if not self._task_lock.acquire(blocking=False):
            self.log_activity("监装数据同步已在运行，本次请求未重复启动", "warning", "sync")
            return self._sync_result(busy=True)
        try:
            current = business_now()
            start = (current - timedelta(days=self.config.bulk_sync_days)).strftime(
                "%Y%m%d"
            )
            end = current.strftime("%Y%m%d")
            initializing = self.database.get_setting("cpm_initialized") != "1"
            mode = "首次初始化" if initializing else "日常同步"
            counters = {
                "scanned": 0,
                "new": 0,
                "updated": 0,
                "skipped": 0,
                "downloads": 0,
                "errors": 0,
            }

            gate_rows: list[dict[str, Any]] = []
            departed_count = 0
            if initializing:
                self.log_activity(
                    f"{mode}：正在下载最近{self.config.bulk_sync_days}天通门证明细",
                    source="sync",
                )
                gate_rows = self._browser_call(
                    self.browser.gate_pass_export_snapshot, start, end
                )
                for row in gate_rows:
                    record = self._gate_record(row)
                    if not record["container_no"]:
                        continue
                    self.database.upsert_gate_pass(record)
                    if self._gate_has_departed(record):
                        departed_count += 1

            self.log_activity(
                f"{mode}：正在下载最近{self.config.bulk_sync_days}天监装明细",
                source="sync",
            )
            rows = self._browser_call(
                self.browser.cpm_export_snapshot, start, end
            )
            rows.sort(
                key=lambda row: (
                    int(str(row.get("cpm_id", "0")))
                    if str(row.get("cpm_id", "")).isdigit()
                    else 0
                ),
                reverse=True,
            )
            latest = str(rows[0].get("cpm_id", "")) if rows else ""
            oldest = str(rows[-1].get("cpm_id", "")) if rows else ""
            for row in rows:
                if self._drain_requested.is_set():
                    break
                cpm_id = str(row.get("cpm_id", ""))
                if not cpm_id or not row.get("container_no"):
                    counters["errors"] += 1
                    continue
                previous, saved = self.database.upsert_cpm(row)
                counters["scanned"] += 1
                if previous is None:
                    counters["new"] += 1
                elif self._status_changed(previous, saved):
                    counters["updated"] += 1

                complete = int(saved.get("das_process_status", 0)) >= 5
                departed = complete and self._cpm_has_departed(saved)
                if departed:
                    self.database.cancel_photo_job(cpm_id, "该箱已实际出厂，跳过自动下载")
                    counters["skipped"] += 1
                elif complete and self._schedule_photo_work(cpm_id, row, saved):
                    counters["downloads"] += 1
                else:
                    counters["skipped"] += 1
                self._log_sync_progress(mode, counters)
                self._photo_scan_checkpoint(counters, gate_checkpoint)

            completed = not self._drain_requested.is_set()
            if completed:
                self.database.set_setting("cpm_initialized", "1")
            self.database.set_setting("latest_cpm_id", latest)
            self.database.set_setting("bulk_sync_start_date", start)
            self.database.set_setting("bulk_sync_completed_at", now_text())
            self.log_activity(
                f"{mode}{'完成' if completed else '已安全暂停'}："
                f"{self._sync_summary(counters)} / "
                f"门证明细 {len(gate_rows)} 箱 / 已出厂 {departed_count} 箱",
                source="sync",
            )
            return self._sync_result(
                **counters,
                latest_cpm_id=latest,
                oldest_cpm_id=oldest,
                gate_rows=len(gate_rows),
                departed=departed_count,
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

    def _cpm_has_departed(self, record: dict[str, Any]) -> bool:
        """Match departure by container and chronology; seal is only informational."""
        begin = self._parsed_gate_datetime(str(record.get("begin_date", "")))
        for gate in self.database.gate_passes_for_container(
            str(record.get("container_no", ""))
        ):
            if not self._gate_has_departed(gate):
                continue
            actual = self._parsed_gate_datetime(
                str(gate.get("actual_departure_at", ""))
            )
            if actual is not None and (begin is None or actual >= begin):
                return True
        return False

    @staticmethod
    def _sync_record(detail: dict[str, Any]) -> dict[str, Any]:
        return {
            **detail,
            "das_status_text": detail.get(
                "status_text", detail.get("das_status_text", "")
            ),
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
        for name in (
            "begin_date",
            "end_date",
            "product_type",
            "packing_type",
            "upload_quantity",
            "seal_no",
            "stage1_confirmed_at",
            "stage2_confirmed_at",
            "stage3_confirmed_at",
            "stage4_confirmed_at",
            "service_year",
            "inspection_result",
        ):
            merged[name] = merged.get(name) or metadata.get(name, "")
        return merged

    @staticmethod
    def _status_changed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
        fields = ("business_stage", "das_process_status", "das_status_text")
        return any(str(previous.get(name, "")) != str(current.get(name, "")) for name in fields)

    def _schedule_photo_work(
        self, cpm_id: str, detail: dict[str, Any], record: dict[str, Any]
    ) -> bool:
        if not int(record.get("is_valid", 1)):
            return False
        process_complete = int(detail.get("das_process_status", 0)) >= 5
        if not process_complete:
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

    def sync_gate_passes(
        self,
        current: datetime | None = None,
        *,
        _task_lock_owned: bool = False,
    ) -> dict[str, int | bool]:
        """Synchronize gate passes planned for today and queue new/changed PDFs."""
        acquired = _task_lock_owned or self._task_lock.acquire(blocking=False)
        if not acquired:
            return {
                "scanned": 0,
                "new": 0,
                "updated": 0,
                "departed": 0,
                "queued": 0,
                "busy": True,
            }
        current = current or business_now()
        date_value = current.strftime("%Y%m%d")
        today = current.strftime("%Y-%m-%d")
        scanned = new = updated = departed = queued = 0
        try:
            self.log_activity("正在同步今天的通门证", source="gate_sync")
            rows = self._browser_call(self.browser.gate_pass_snapshot, date_value)
            for row in rows:
                if self._drain_requested.is_set():
                    break
                planned_date = self._normalized_gate_date(
                    str(row.get("planned_departure_at", ""))
                )
                if planned_date != today:
                    continue
                record = self._gate_record(row)
                if not record["container_no"]:
                    continue
                previous, saved = self.database.upsert_gate_pass(record)
                if self._gate_has_departed(record):
                    self.database.mark_gate_departed(
                        str(record["gate_key"]),
                        str(record.get("actual_departure_at", "")),
                    )
                    cpm = self.database.latest_valid_cpm(str(record["container_no"]))
                    if cpm and self._cpm_has_departed(cpm):
                        self.database.cancel_photo_job(
                            str(cpm["cpm_id"]), "该箱已实际出厂，跳过自动下载"
                        )
                    departed += 1
                    continue
                scanned += 1
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
                f"更新 {updated} 箱 / 已出厂排除 {departed} 箱 / PDF排队 {queued} 箱",
                source="gate_sync",
            )
            return {
                "scanned": scanned,
                "new": new,
                "updated": updated,
                "departed": departed,
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
            if not _task_lock_owned:
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
        if not self._gate_has_departed(saved) and planned_date == today and (
            previous is None
            or changed
            or saved.get("pdf_status") != "ready"
            or not pdf_path.is_file()
            or pdf_path.resolve() != expected_pdf_path.resolve()
        ):
            self.database.enqueue_job("gate_pdf", str(saved["gate_key"]))
        return saved

    @staticmethod
    def _gate_has_departed(gate: dict[str, Any]) -> bool:
        actual = str(gate.get("actual_departure_at", "")).strip()
        return bool(actual and actual not in {"-", "--"})

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

    def _photo_scan_checkpoint(
        self,
        counters: dict[str, int],
        gate_checkpoint: Callable[[], None] | None,
    ) -> None:
        """Yield to a due gate-pass sync between bounded photo scan batches."""
        batch_size = max(1, self.config.photo_scan_batch_size)
        if (
            gate_checkpoint is not None
            and counters["scanned"]
            and counters["scanned"] % batch_size == 0
        ):
            gate_checkpoint()

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
        gate_rows: int = 0,
        departed: int = 0,
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
            "gate_rows": gate_rows,
            "departed": departed,
            "busy": busy,
        }

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

    def _finish_background_job(
        self,
        job: dict[str, Any],
        status: str,
        error: str = "",
    ) -> None:
        if job.get("job_type") == "download_ocr":
            record = self.database.cpm_by_id(str(job["cpm_id"]))
            if record is not None and not int(record.get("is_valid", 1)):
                status = "cancelled"
                error = "该监装记录已被新的记录替代"
        run_after = now_text()
        attempts = int(job.get("attempts", 0)) + 1
        if status == "pending":
            delay = (60, 300, 900)[min(attempts - 1, 2)]
            run_after = (
                business_now() + timedelta(seconds=delay)
            ).isoformat(timespec="seconds")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE background_jobs SET status=?,last_error=?,run_after=?,updated_at=? WHERE id=?",
                (status, error, run_after, now_text(), job["id"]),
            )

    def _complete_photo_ocr(
        self,
        job: dict[str, Any],
        downloaded: dict[str, Any],
        future: Future,
    ) -> None:
        try:
            result = future.result()
            count = int(
                result.get(
                    "downloaded_photo_count",
                    downloaded.get("downloaded_photo_count", 0),
                )
            )
            self.log_activity(
                f"监装照片下载与文字识别完成：原图 {count} 张",
                source="ocr",
            )
            self._finish_background_job(job, "done")
        except Exception as exc:
            LOGGER.exception(
                "background OCR failed key=%s",
                job["cpm_id"],
            )
            attempts = int(job.get("attempts", 0)) + 1
            status = "pending" if attempts < 4 else "failed"
            self.log_activity(f"后台照片文字识别失败：{exc}", "error", "ocr")
            self._finish_background_job(job, status, str(exc))
        finally:
            with self._ocr_job_lock:
                self._ocr_jobs_inflight.discard(int(job["id"]))

    def run_one_job(self) -> bool:
        if self._drain_requested.is_set():
            return False
        with self._ocr_job_lock:
            photo_capacity_available = (
                len(self._ocr_jobs_inflight) < self._ocr_pipeline_capacity
            )
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM background_jobs WHERE status='pending' AND run_after<=? "
                "AND (?=0 OR job_type='gate_pdf') "
                "AND (job_type='gate_pdf' OR NOT EXISTS ("
                "SELECT 1 FROM cpm_records c WHERE c.cpm_id=background_jobs.cpm_id AND c.is_valid=0)) "
                "ORDER BY CASE WHEN job_type='gate_pdf' THEN 0 ELSE 1 END, "
                "CASE WHEN cpm_id GLOB '[0-9]*' THEN CAST(cpm_id AS INTEGER) ELSE 0 END DESC, "
                "id DESC LIMIT 1",
                (now_text(), int(not photo_capacity_available)),
            ).fetchone()
            if not row:
                return False
            connection.execute(
                "UPDATE background_jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",
                (now_text(), row["id"]),
            )
            job = dict(row)
        is_photo_job = job["job_type"] != "gate_pdf"
        if is_photo_job:
            with self._ocr_job_lock:
                self._ocr_jobs_inflight.add(int(job["id"]))
        try:
            completed_status = "done"
            if job["job_type"] == "gate_pdf":
                gate = self.database.gate_pass_by_key(str(job["cpm_id"]))
                if not gate:
                    raise RuntimeError("通门证记录已不存在")
                if self._gate_has_departed(gate):
                    completed_status = "cancelled"
                    self.log_activity(
                        f"{gate['container_no']}：已实际出厂，跳过通门证 PDF 自动下载",
                        source="gate_pdf",
                        container_no=str(gate["container_no"]),
                    )
                planned_date = self._normalized_gate_date(
                    str(gate.get("planned_departure_at", ""))
                )
                today = business_now().strftime("%Y-%m-%d")
                if completed_status != "cancelled" and planned_date == today:
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
                self._finish_background_job(job, completed_status)
            else:
                cpm = self.database.cpm_by_id(str(job["cpm_id"]))
                if cpm and self._cpm_has_departed(cpm):
                    self.log_activity(
                        f"{cpm['container_no']}：已实际出厂，跳过监装照片自动下载",
                        source="ocr",
                        container_no=str(cpm["container_no"]),
                    )
                    self._finish_background_job(job, "cancelled")
                    with self._ocr_job_lock:
                        self._ocr_jobs_inflight.discard(int(job["id"]))
                    return True
                self.log_activity("后台正在下载监装照片", source="ocr")
                detail = self._browser_call(
                    self.browser.fetch_cpm_detail, job["cpm_id"]
                )
                if int(detail.get("das_process_status", 0)) == 5 and not detail.get(
                    "photos"
                ):
                    raise RuntimeError("DAS 照片页面状态为 5，但详情暂未返回照片")
                downloaded = self._browser_call(
                    self.photos.download,
                    job["cpm_id"],
                    detail,
                )
                self.log_activity(
                    "监装原图下载完成，已转入独立 CPU 线程识别",
                    source="ocr",
                )
                future = self._ocr_executor.submit(
                    self.photos.recognize,
                    job["cpm_id"],
                    detail,
                )
                future.add_done_callback(
                    lambda completed, current=job, inventory=downloaded: (
                        self._complete_photo_ocr(current, inventory, completed)
                    )
                )
                return True
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
            self._finish_background_job(job, status, error)
            if is_photo_job:
                with self._ocr_job_lock:
                    self._ocr_jobs_inflight.discard(int(job["id"]))
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
                self.photos.download,
                record["cpm_id"],
                detail,
                lambda stage, label: self.log_activity(
                    f"{container_no}：{label}",
                    source="verify",
                    container_no=container_no,
                    stage=stage,
                ),
            )
            self._ocr_call(
                self.photos.recognize,
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

    def refresh_container_photos(self, container_no: str) -> dict[str, Any]:
        """Force a safe one-container refresh while keeping the old cache on failure."""
        if not self._manual_refresh_lock.acquire(blocking=False):
            raise RuntimeError("已有照片更新任务正在执行，请稍候")
        wanted = container_no.strip().upper()
        previous_active = self.database.latest_valid_cpm(wanted)
        previous_valid_ids = set(self.database.valid_cpm_ids(wanted))
        selected: dict[str, Any] | None = None
        context: dict[str, Any] | None = None
        reservation = "available"
        self.note_interactive()
        self.log_activity(
            f"{wanted}：正在查询最新监装照片记录",
            source="verify",
            container_no=wanted,
            stage="photo_query",
        )
        try:
            resolved = self.resolve_container(wanted)
            selected = resolved.get("record")
            detail = resolved.get("detail")
            if not selected or not detail:
                raise RuntimeError(
                    str(resolved.get("message") or "未找到该箱号的监装照片记录")
                )
            cpm_id = str(selected["cpm_id"])
            reservation = self.database.reserve_manual_photo_refresh(cpm_id)
            if reservation == "running":
                raise RuntimeError("该箱照片正在后台处理中，请稍候再更新")
            context = self._browser_call(
                self.photos.begin_forced_refresh,
                cpm_id,
                detail,
                lambda stage, label: self.log_activity(
                    f"{wanted}：{label}",
                    source="verify",
                    container_no=wanted,
                    stage=stage,
                ),
            )
            self._ocr_call(
                self.photos.recognize,
                cpm_id,
                detail,
                lambda stage, label: self.log_activity(
                    f"{wanted}：{label}",
                    source="verify",
                    container_no=wanted,
                    stage=stage,
                ),
            )
            self.photos.commit_forced_refresh(context)
            refreshed = self.database.verification_row(wanted)
            count = int(context.get("downloaded_photo_count", 0))
            self.log_activity(
                f"{wanted}：监装照片已更新，共 {count} 张",
                source="verify",
                container_no=wanted,
                stage="ocr_done",
            )
            return {
                "record": refreshed or selected,
                "cpm_id": cpm_id,
                "downloaded_photo_count": count,
                "changed_cpm": bool(
                    previous_active
                    and str(previous_active["cpm_id"]) != cpm_id
                ),
            }
        except Exception:
            if context is not None:
                self.photos.rollback_forced_refresh(context)
            if previous_active is not None:
                self.database.set_cpm_validity(
                    str(previous_active["cpm_id"]), True
                )
            if (
                selected is not None
                and str(selected["cpm_id"]) not in previous_valid_ids
            ):
                self.database.set_cpm_validity(str(selected["cpm_id"]), False)
            if reservation == "cancelled" and selected is not None:
                self.database.enqueue_job("download_ocr", str(selected["cpm_id"]))
            self.log_activity(
                f"{wanted}：照片更新失败，已保留原照片",
                "error",
                "verify",
                container_no=wanted,
                stage="photo_query",
            )
            raise
        finally:
            self._manual_refresh_lock.release()

    def resolve_container(self, container_no: str) -> dict[str, Any]:
        """Refresh a container mapping through the DAS photo-download search page."""
        wanted = container_no.strip().upper()
        self.log_activity(
            f"{wanted}：正在确认最新监装记录",
            source="verify",
            container_no=wanted,
            stage="photo_query",
        )
        local_record = self.database.latest_valid_cpm(wanted)
        try:
            candidates = self._browser_call(
                self.browser.find_cpm_by_container, wanted
            )
        except LoginRequired:
            raise
        except Exception:
            LOGGER.exception("Photo-page CPM lookup failed container=%s", wanted)
            return {
                "record": local_record,
                "gate": None,
                "gate_status": "unknown",
                "checked": 0,
                "message": "监装记录暂时无法刷新，已使用本地数据",
            }
        if not candidates:
            return {
                "record": local_record,
                "gate": None,
                "gate_status": "unknown",
                "checked": 0,
                "message": (
                    "未找到该箱号的监装照片记录"
                    if local_record is None
                    else "监装记录暂时无法刷新，已使用本地数据"
                ),
            }

        visible_ids = {
            str(metadata.get("cpm_id", ""))
            for metadata in candidates
            if str(metadata.get("cpm_id", ""))
        }
        resolved: list[
            tuple[tuple[int, int], dict[str, Any], dict[str, Any]]
        ] = []
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
            has_stage4 = bool(
                stage4_count
                or int(current.get("downloaded_photo_count", 0))
                or int(current.get("photo_count", 0))
            )
            numeric_id = int(cpm_id) if cpm_id.isdigit() else 0
            resolved.append(
                (
                    (
                        int(has_stage4),
                        numeric_id,
                    ),
                    current,
                    detail,
                )
            )
        if not resolved:
            return {
                "record": local_record,
                "gate": None,
                "gate_status": "unknown",
                "checked": len(candidates),
                "message": (
                    "监装照片详情读取失败，请稍后重试"
                    if local_record is None
                    else "监装记录暂时无法刷新，已使用本地数据"
                ),
            }
        _rank, selected, selected_detail = max(resolved, key=lambda item: item[0])
        superseded = self.database.supersede_missing_cpm_records(
            wanted,
            str(selected["cpm_id"]),
            visible_ids,
        )
        if superseded:
            self.log_activity(
                f"{wanted}：已采用新的监装记录，旧记录停止后台处理",
                source="sync",
                container_no=wanted,
            )
        self.log_activity(
            f"{wanted}：已找到监装照片记录",
            source="verify",
            container_no=wanted,
            stage="mapping",
        )
        return {
            "record": selected,
            "detail": selected_detail,
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
        next_check = next_gate_sync = next_photo_sync = 0.0
        next_cleanup = self._next_cache_cleanup()

        def check_gate_between_photo_batches() -> None:
            nonlocal next_gate_sync
            if time.monotonic() < next_gate_sync or self._drain_requested.is_set():
                return
            try:
                self.sync_gate_passes(
                    business_now(),
                    _task_lock_owned=True,
                )
            finally:
                next_gate_sync = (
                    time.monotonic() + self.config.gate_sync_interval_seconds
                )

        self.log_activity(
            f"缓存自动清理已计划于 {next_cleanup.strftime('%Y-%m-%d %H:%M')}",
            source="cleanup",
        )
        while not self._stop.wait(2):
            try:
                if self._drain_requested.is_set():
                    break
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
                    if self.database.session().get("status") != "logged_in":
                        continue
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
                startup_sync = self._startup_sync_pending
                initialization_exception = (
                    startup_sync
                    and not self.initial_import_state()["required"]
                    and self.database.get_setting("cpm_initialized") != "1"
                )
                if not window_open and not initialization_exception:
                    continue
                self._startup_sync_pending = False
                if startup_sync or now >= next_gate_sync:
                    try:
                        self.sync_gate_passes(wall_now)
                    finally:
                        next_gate_sync = (
                            time.monotonic()
                            + self.config.gate_sync_interval_seconds
                        )
                    if self._drain_requested.is_set():
                        break
                if self.initial_import_state()["required"]:
                    continue
                if startup_sync or now >= next_photo_sync:
                    try:
                        self.sync_latest_window(
                            gate_checkpoint=check_gate_between_photo_batches
                        )
                    finally:
                        next_photo_sync = (
                            time.monotonic()
                            + self.config.photo_sync_interval_seconds
                        )
                if (
                    not self._drain_requested.is_set()
                    and self.database.session().get("status") == "logged_in"
                    and now >= self._background_resume_at
                ):
                    self.run_one_job()
            except Exception:
                LOGGER.exception("smartGPMS background loop recovered from an error")
