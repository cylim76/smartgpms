from __future__ import annotations

import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from smartgpms.config import AppConfig
from smartgpms.credentials import CredentialStore
from smartgpms.das_browser import DasBrowserError, LoginRequired
from smartgpms.database import Database
from smartgpms.iso6346 import normalize, validate_container_number
from smartgpms.service import SmartGPMSService

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
config = AppConfig(BASE_DIR)
database = Database(DATA_DIR / "smartgpms.sqlite3")
credentials = CredentialStore(DATA_DIR / "credentials.json")
service = SmartGPMSService(config, database, credentials)
pending_prints: dict[str, dict[str, Any]] = {}
pending_prints_lock = threading.Lock()
PENDING_PRINT_TTL_SECONDS = 600


@asynccontextmanager
async def lifespan(_: FastAPI):
    service.start()
    yield
    service.stop()


app = FastAPI(title="smartGPMS", version="0.2.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginPayload(BaseModel):
    username: str
    password: str = ""
    otp: str
    remember: bool = True


class VerifyPayload(BaseModel):
    containers: list[str]


class PrintPayload(BaseModel):
    cpm_id: str
    force: bool = False
    operator: str = ""
    reason: str = ""


class PrintConfirmation(BaseModel):
    token: str
    completed: bool


def _clean_numbers(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        for part in re.split(r"[\s,;，；]+", value):
            number = normalize(part)
            if number and number not in output:
                output.append(number)
    return output


def _public_ocr(
    value: dict[str, Any] | None, cpm_id: str, target: str
) -> dict[str, Any] | None:
    if not value:
        return None
    raw = value.get("raw") or {}
    return {
        "observed": value.get("observed_text", ""),
        "suggested": value.get("suggested_text", ""),
        "confidence": value.get("confidence", 0),
        "rotation": value.get("rotation", 0),
        "matches_expected": bool(raw.get("matches_expected")),
        "crop_url": f"/api/crop/{cpm_id}/{target}?v={value.get('id', 0)}",
    }


def _verdict(
    gate_container: str,
    gate_seal: str,
    container_ocr: dict | None,
    seal_ocr: dict | None,
) -> tuple[str, str]:
    if not gate_container or not gate_seal or not container_ocr or not seal_ocr:
        return "review", "门证、照片或识别结果不完整，请人工确认"
    photo_container = normalize(
        str(
            container_ocr.get("observed_text")
            or container_ocr.get("suggested_text")
            or ""
        )
    )
    photo_seal = normalize(str(seal_ocr.get("observed_text") or ""))
    if (
        normalize(gate_container) == photo_container
        and normalize(gate_seal) == photo_seal
    ):
        return "match", "箱号和铅封号均一致"
    return "mismatch", "门证与照片识别内容不一致"


def _cleanup_pending_prints() -> None:
    cutoff = time.monotonic() - PENDING_PRINT_TTL_SECONDS
    with pending_prints_lock:
        expired = [
            token
            for token, value in pending_prints.items()
            if value["created_at"] < cutoff
        ]
        for token in expired:
            pending_prints.pop(token, None)


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/bootstrap")
def bootstrap():
    return {
        "version": app.version,
        "session": database.session(),
        "credentials": credentials.public(),
        "sync_interval_seconds": config.sync_interval_seconds,
        "ocr_engine": "RapidOCR / ONNX Runtime CPU",
        "database": database.stats(),
    }


@app.post("/api/login")
def login(payload: LoginPayload):
    username = payload.username.strip()
    password = payload.password
    if not password:
        saved_username, saved_password = credentials.load()
        if saved_username == username:
            password = saved_password
    try:
        return service.login(username, password, payload.otp.strip(), payload.remember)
    except (DasBrowserError, LoginRequired, ValueError) as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/session/check")
def check_session():
    return service.check_session()


@app.post("/api/sync")
def sync_now():
    if database.session().get("status") != "logged_in":
        raise HTTPException(status_code=401, detail="请先登录 SSO")
    return service.sync_cpm()


@app.post("/api/verify")
def verify(payload: VerifyPayload):
    numbers = _clean_numbers(payload.containers)
    if not numbers:
        raise HTTPException(status_code=400, detail="请输入至少一个箱号")
    if len(numbers) > 50:
        raise HTTPException(status_code=400, detail="一次最多核验 50 个箱号")
    rows: list[dict[str, Any]] = []
    for number in numbers:
        if len(number) != 11 or not validate_container_number(number):
            rows.append(
                {
                    "input_container": number,
                    "verdict": "review",
                    "message": "箱号格式或校验位不正确",
                }
            )
            continue
        record = database.latest_valid_cpm(number)
        if not record:
            rows.append(
                {
                    "input_container": number,
                    "verdict": "review",
                    "message": "本地 CPMID 映射中未找到；请先同步或等待后台扫描",
                }
            )
            continue
        errors: list[str] = []
        try:
            prepared = service.prepare_container(number) or record
        except LoginRequired as exc:
            database.update_session("logged_out", message=str(exc))
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - keep other container rows available for review
            prepared = database.verification_row(number) or record
            errors.append(f"照片处理失败：{exc}")
        try:
            gate = service._browser_call(service.browser.open_gate_detail, number)
        except LoginRequired as exc:
            database.update_session("logged_out", message=str(exc))
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - return a review row instead of aborting the batch
            gate = {"container_no": "", "seal_no": ""}
            errors.append(f"门证查询失败：{exc}")
        container_ocr = prepared.get("container_ocr")
        seal_ocr = prepared.get("seal_ocr")
        verdict, message = _verdict(
            gate.get("container_no", ""),
            gate.get("seal_no", ""),
            container_ocr,
            seal_ocr,
        )
        if errors:
            verdict, message = "review", "；".join(errors)
        rows.append(
            {
                "input_container": number,
                "cpm_id": record["cpm_id"],
                "business_stage": record["business_stage"],
                "print_status": record["print_status"],
                "print_count": record["print_count"],
                "last_printed_at": record["last_printed_at"],
                "gate_container_no": gate.get("container_no", ""),
                "gate_seal_no": gate.get("seal_no", ""),
                "container_ocr": _public_ocr(
                    container_ocr, record["cpm_id"], "container"
                ),
                "seal_ocr": _public_ocr(seal_ocr, record["cpm_id"], "seal"),
                "verdict": verdict,
                "message": message,
            }
        )
    return {"rows": rows}


@app.get("/api/crop/{cpm_id}/{target_type}")
def crop(cpm_id: str, target_type: str):
    if target_type not in {"container", "seal"}:
        raise HTTPException(status_code=404)
    result = database.best_ocr(cpm_id, target_type)
    if not result or not result.get("crop_path"):
        raise HTTPException(status_code=404)
    path = Path(result["crop_path"]).resolve()
    cache_root = config.cache_dir.resolve()
    if cache_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.post("/api/print")
def print_gate(payload: PrintPayload):
    _cleanup_pending_prints()
    record = database.cpm_by_id(payload.cpm_id)
    if not record:
        raise HTTPException(status_code=404, detail="未找到 CPMID")
    try:
        gate = service._browser_call(
            service.browser.open_gate_detail, record["container_no"]
        )
        container_ocr = database.best_ocr(payload.cpm_id, "container")
        seal_ocr = database.best_ocr(payload.cpm_id, "seal")
        verdict, _ = _verdict(
            gate.get("container_no", ""),
            gate.get("seal_no", ""),
            container_ocr,
            seal_ocr,
        )
        if verdict != "match" and not payload.force:
            raise HTTPException(
                status_code=409, detail="核验未通过；人工确认后才能强制打印"
            )
        if (
            payload.force or int(record["print_status"])
        ) and not payload.reason.strip():
            raise HTTPException(status_code=400, detail="人工确认或重打必须填写原因")
        result = service._browser_call(service.browser.print_current_gate)
        snapshot = {
            "gate_container_no": gate.get("container_no", ""),
            "gate_seal_no": gate.get("seal_no", ""),
            "ocr_container_no": (container_ocr or {}).get("observed_text", ""),
            "ocr_seal_no": (seal_ocr or {}).get("observed_text", ""),
            "verdict": verdict,
            "container_ocr": container_ocr,
            "seal_ocr": seal_ocr,
            "cpm_record": record,
            "das_result": result.get("message", "success"),
        }
        token = uuid.uuid4().hex
        with pending_prints_lock:
            pending_prints[token] = {
                "cpm_id": payload.cpm_id,
                "snapshot": snapshot,
                "operator": payload.operator.strip(),
                "reason": payload.reason.strip(),
                "created_at": time.monotonic(),
            }
        return {"ok": True, "pending_confirmation": True, "token": token, **result}
    except HTTPException:
        raise
    except LoginRequired as exc:
        database.update_session("logged_out", message=str(exc))
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/print/confirm")
def confirm_print(payload: PrintConfirmation):
    _cleanup_pending_prints()
    with pending_prints_lock:
        pending = pending_prints.pop(payload.token, None)
    if not pending:
        raise HTTPException(status_code=404, detail="打印确认已失效")
    if not payload.completed:
        return {"ok": True, "recorded": False, "message": "已取消，不修改打印状态"}
    snapshot = pending["snapshot"]
    try:
        snapshot["evidence_dir"] = service.freeze_print_evidence(
            pending["cpm_id"], snapshot
        )
        state = database.record_print(
            pending["cpm_id"], snapshot, pending["operator"], pending["reason"]
        )
    except Exception:
        with pending_prints_lock:
            pending_prints[payload.token] = pending
        raise
    return {"ok": True, "recorded": True, **state}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": app.version,
        "session": database.session().get("status", "logged_out"),
    }
