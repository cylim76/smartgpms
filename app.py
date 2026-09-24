from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

from smartgpms.config import AppConfig
from smartgpms.credentials import CredentialStore
from smartgpms.das_browser import DasBrowserError, GatePassNotFound, LoginRequired
from smartgpms.database import Database
from smartgpms.iso6346 import normalize, validate_container_number
from smartgpms.service import SmartGPMSService
from smartgpms.time_utils import business_now

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.environ.get("SMARTGPMS_DATA_DIR", BASE_DIR / "data")).resolve()
config = AppConfig(BASE_DIR, data_root=DATA_DIR)
database = Database(DATA_DIR / "smartgpms.sqlite3")
database.activate_ocr_pipeline("das-photo-ai-orientation-fusion-v3")
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


app = FastAPI(title="smartGPMS", version="0.11.1", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class LoginPayload(BaseModel):
    username: str
    password: str = ""
    otp: str
    remember: bool = True


class VerifyPayload(BaseModel):
    containers: list[str]


class GateVerifyPayload(BaseModel):
    container: str


class PrintPayload(BaseModel):
    cpm_id: str
    force: bool = False
    allow_expired_gate: bool = False
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
        "photo_id": value.get("photo_id", 0),
        "matches_expected": bool(raw.get("matches_expected")),
        "crop_url": f"/api/crop/{cpm_id}/{target}?v={value.get('id', 0)}",
        "original_url": f"/api/photo/{cpm_id}/{value.get('photo_id', 0)}",
    }


def _other_stage4_photos(
    cpm_id: str, *selected_ocr: dict[str, Any] | None
) -> list[dict[str, Any]]:
    selected_ids = {
        int(value.get("photo_id", 0)) for value in selected_ocr if value
    }
    return [
        {
            "photo_id": int(photo["id"]),
            "thumbnail_url": f"/api/photo-thumbnail/{cpm_id}/{photo['id']}",
            "original_url": f"/api/photo/{cpm_id}/{photo['id']}",
        }
        for photo in database.photos_for_cpm(cpm_id)
        if int(photo.get("step_no", 0)) == 4
        and photo.get("cache_status") == "ready"
        and int(photo["id"]) not in selected_ids
    ]


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


def _gate_date_valid(gate: dict[str, Any] | None) -> bool:
    if not gate:
        return False
    planned = service._normalized_gate_date(
        str(gate.get("planned_departure_at", ""))
    )
    return planned == business_now().strftime("%Y-%m-%d")


def _comparison_fields(
    gate: dict[str, Any] | None,
    container_ocr: dict[str, Any] | None,
    seal_ocr: dict[str, Any] | None,
) -> dict[str, bool | None]:
    gate_container = normalize(str((gate or {}).get("container_no", "")))
    gate_seal = normalize(str((gate or {}).get("seal_no", "")))
    photo_container = normalize(
        str(
            (container_ocr or {}).get("observed_text")
            or (container_ocr or {}).get("suggested_text")
            or ""
        )
    )
    photo_seal = normalize(str((seal_ocr or {}).get("observed_text") or ""))
    return {
        "container_matches": (
            gate_container == photo_container
            if gate_container and photo_container
            else None
        ),
        "seal_matches": gate_seal == photo_seal if gate_seal and photo_seal else None,
    }


def _gate_public_fields(gate: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "gate_container_no": str((gate or {}).get("container_no", "")),
        "gate_seal_no": str((gate or {}).get("seal_no", "")),
        "gate_pass_no": str((gate or {}).get("gate_pass_no", "")),
        "gate_application_date": str((gate or {}).get("application_date", "")),
        "gate_planned_departure_at": str(
            (gate or {}).get("planned_departure_at", "")
        ),
        "gate_date_valid": _gate_date_valid(gate),
    }


def _cleanup_pending_prints() -> None:
    cutoff = time.monotonic() - PENDING_PRINT_TTL_SECONDS
    expired_values: list[dict[str, Any]] = []
    with pending_prints_lock:
        expired = [
            token
            for token, value in pending_prints.items()
            if value["created_at"] < cutoff
        ]
        for token in expired:
            value = pending_prints.pop(token, None)
            if value:
                expired_values.append(value)
    for value in expired_values:
        _delete_pending_pdf(value)
    if config.print_spool_dir.is_dir():
        disk_cutoff = time.time() - PENDING_PRINT_TTL_SECONDS
        for path in config.print_spool_dir.glob("*.pdf"):
            try:
                if path.stat().st_mtime < disk_cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass


def _pending_pdf_path(pending: dict[str, Any]) -> Path:
    value = str(pending.get("pdf_path", ""))
    if not value:
        raise HTTPException(status_code=404, detail="打印文件不存在")
    path = Path(value).resolve()
    root = config.print_spool_dir.resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="打印文件不存在或已过期")
    return path


def _delete_pending_pdf(pending: dict[str, Any]) -> None:
    value = str(pending.get("pdf_path", ""))
    if not value:
        return
    path = Path(value).resolve()
    root = config.print_spool_dir.resolve()
    if root in path.parents:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _get_pending_print(token: str) -> dict[str, Any]:
    _cleanup_pending_prints()
    with pending_prints_lock:
        pending = pending_prints.get(token)
    if not pending:
        raise HTTPException(status_code=404, detail="打印任务不存在或已过期")
    return pending


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
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"后台浏览器登录异常，请重试：{exc}"
        ) from exc


@app.post("/api/session/check")
def check_session():
    return service.check_session()


@app.post("/api/sync")
def sync_now():
    if database.session().get("status") != "logged_in":
        raise HTTPException(status_code=401, detail="请先登录 SSO")
    return service.sync_latest_window()


@app.get("/api/activity")
def activity(after: int = 0):
    return service.activity(max(0, after))


@app.post("/api/verify")
def verify(payload: VerifyPayload):
    numbers = _clean_numbers(payload.containers)
    if not numbers:
        raise HTTPException(status_code=400, detail="请输入至少一个箱号")
    if len(numbers) > 50:
        raise HTTPException(status_code=400, detail="一次最多核验 50 个箱号")
    service.note_interactive()
    service.log_activity(f"开始核验 {len(numbers)} 个箱号", source="verify")
    rows: list[dict[str, Any]] = []
    for number in numbers:
        service.log_activity(
            f"{number}：开始查询",
            source="verify",
            container_no=number,
            stage="mapping",
        )
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
        gate: dict[str, Any] | None = None
        gate_status = "unknown"
        if not record:
            try:
                resolved = service.resolve_container(number)
                record = resolved.get("record")
                gate = resolved.get("gate")
                gate_status = resolved.get("gate_status", "unknown")
            except LoginRequired as exc:
                database.update_session("logged_out", message=str(exc))
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except Exception:  # noqa: BLE001 - return a review row for this container
                rows.append(
                    {
                        "input_container": number,
                        "verdict": "review",
                        "message": "监装照片记录查询失败，请稍后重试",
                    }
                )
                continue
        if not record:
            message = str(resolved.get("message") or "").strip() or (
                "未找到该箱号的监装照片记录"
            )
            rows.append(
                {
                    "input_container": number,
                    "verdict": "review",
                    **_gate_public_fields(gate),
                    "gate_status": gate_status,
                    "message": message,
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
        if gate is None:
            try:
                service.log_activity(
                    f"{number}：查询 DAS 门证",
                    source="verify",
                    container_no=number,
                    stage="gate_query",
                )
                gate = service._browser_call(service.browser.open_gate_detail, number)
                gate = service.save_gate_detail(gate)
                gate_status = "found"
            except LoginRequired as exc:
                database.update_session("logged_out", message=str(exc))
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except GatePassNotFound:
                gate = {"container_no": "", "seal_no": ""}
                gate_status = "not_found"
            except Exception:  # noqa: BLE001 - return a review row instead of aborting the batch
                gate = {"container_no": "", "seal_no": ""}
                gate_status = "error"
                errors.append("门证查询异常，请重试")
        container_ocr = prepared.get("container_ocr")
        seal_ocr = prepared.get("seal_ocr")
        service.log_activity(
            f"{number}：比对门证与照片识别结果",
            source="verify",
            container_no=number,
            stage="compare",
        )
        verdict, message = _verdict(
            gate.get("container_no", ""),
            gate.get("seal_no", ""),
            container_ocr,
            seal_ocr,
        )
        if gate_status == "not_found":
            verdict, message = "review", "未找到门证"
        elif errors:
            verdict, message = "review", "；".join(errors)
        comparison = _comparison_fields(gate, container_ocr, seal_ocr)
        rows.append(
            {
                "input_container": number,
                "cpm_id": record["cpm_id"],
                "business_stage": record.get("das_process_status")
                or record["business_stage"],
                "print_status": record["print_status"],
                "print_count": record["print_count"],
                "last_printed_at": record["last_printed_at"],
                **_gate_public_fields(gate),
                "gate_status": gate_status,
                **comparison,
                "container_ocr": _public_ocr(
                    container_ocr, record["cpm_id"], "container"
                ),
                "seal_ocr": _public_ocr(seal_ocr, record["cpm_id"], "seal"),
                "other_photos": _other_stage4_photos(
                    record["cpm_id"], container_ocr, seal_ocr
                ),
                "verdict": verdict,
                "message": message,
            }
        )
    service.log_activity(f"本次核验完成，共返回 {len(rows)} 条结果", source="verify")
    return {"rows": rows}


@app.post("/api/verify/local")
def verify_local(payload: VerifyPayload):
    """Return locally cached photos/OCR immediately, without opening DAS pages."""
    numbers = _clean_numbers(payload.containers)
    if not numbers:
        raise HTTPException(status_code=400, detail="请输入至少一个箱号")
    if len(numbers) > 50:
        raise HTTPException(status_code=400, detail="一次最多核验 50 个箱号")
    service.note_interactive()
    rows: list[dict[str, Any]] = []
    for number in numbers:
        if len(number) != 11 or not validate_container_number(number):
            rows.append(
                {
                    "input_container": number,
                    "gate_status": "invalid",
                    "verdict": "review",
                    "message": "箱号格式或校验位不正确",
                }
            )
            continue
        record = database.latest_valid_cpm(number)
        gate = database.latest_gate_pass(number)
        if not record:
            rows.append(
                {
                    "input_container": number,
                    **_gate_public_fields(gate),
                    "gate_status": "found" if gate else "pending",
                    "gate_needs_refresh": not _gate_date_valid(gate),
                    "needs_photo_refresh": True,
                    "verdict": "review",
                    "message": "正在查询本地记录并补充门证",
                }
            )
            continue
        prepared = database.verification_row(number) or record
        container_ocr = prepared.get("container_ocr")
        seal_ocr = prepared.get("seal_ocr")
        needs_photo_refresh = record.get("ocr_status") not in {"ready", "review"}
        if gate:
            verdict, message = _verdict(
                str(gate.get("container_no", "")),
                str(gate.get("seal_no", "")),
                container_ocr,
                seal_ocr,
            )
        else:
            verdict, message = "review", "正在后台查询门证"
        rows.append(
            {
                "input_container": number,
                "cpm_id": record["cpm_id"],
                "business_stage": record.get("das_process_status")
                or record["business_stage"],
                "print_status": record["print_status"],
                "print_count": record["print_count"],
                "last_printed_at": record["last_printed_at"],
                **_gate_public_fields(gate),
                "gate_status": "found" if gate else "pending",
                "gate_needs_refresh": not _gate_date_valid(gate),
                "needs_photo_refresh": needs_photo_refresh,
                **_comparison_fields(gate, container_ocr, seal_ocr),
                "container_ocr": _public_ocr(
                    container_ocr, record["cpm_id"], "container"
                ),
                "seal_ocr": _public_ocr(seal_ocr, record["cpm_id"], "seal"),
                "other_photos": _other_stage4_photos(
                    record["cpm_id"], container_ocr, seal_ocr
                ),
                "verdict": verdict,
                "message": message,
            }
        )
    return {"rows": rows}


@app.post("/api/verify/gate")
def verify_gate(payload: GateVerifyPayload):
    """Complete one locally rendered row with fresh DAS photo and gate data."""
    result = verify(VerifyPayload(containers=[payload.container]))
    return {"row": (result.get("rows") or [{}])[0]}


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


def _original_photo_path(cpm_id: str, photo_id: int) -> Path:
    photo = database.photo_by_id(cpm_id, photo_id)
    if not photo or photo.get("cache_status") != "ready":
        raise HTTPException(status_code=404, detail="原图已清理或不存在")
    path = Path(str(photo.get("local_path", ""))).resolve()
    cache_root = config.cache_dir.resolve()
    if cache_root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="原图已清理或不存在")
    return path


@app.get("/api/photo/{cpm_id}/{photo_id}")
def original_photo(cpm_id: str, photo_id: int):
    path = _original_photo_path(cpm_id, photo_id)
    return FileResponse(path)


@app.get("/api/photo-thumbnail/{cpm_id}/{photo_id}")
def photo_thumbnail(cpm_id: str, photo_id: int):
    photo = database.photo_by_id(cpm_id, photo_id)
    if photo:
        thumbnail_value = str(photo.get("thumbnail_path", ""))
        if thumbnail_value:
            thumbnail = Path(thumbnail_value).resolve()
            cache_root = config.cache_dir.resolve()
            if cache_root in thumbnail.parents and thumbnail.is_file():
                return FileResponse(
                    thumbnail,
                    media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"},
                )
    path = _original_photo_path(cpm_id, photo_id)
    with Image.open(path) as loaded:
        preview = ImageOps.exif_transpose(loaded).convert("RGB")
        preview.thumbnail((256, 160), Image.Resampling.LANCZOS)
        output = BytesIO()
        preview.save(output, "JPEG", quality=78, optimize=True)
    return Response(
        output.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.post("/api/print")
def print_gate(payload: PrintPayload):
    _cleanup_pending_prints()
    record = database.cpm_by_id(payload.cpm_id)
    if not record:
        raise HTTPException(status_code=404, detail="未找到该箱号的本地核验记录，请重新核验")
    try:
        gate = database.latest_gate_pass(str(record["container_no"]))
        if gate is None:
            gate = service._browser_call(
                service.browser.open_gate_detail, record["container_no"]
            )
            gate = service.save_gate_detail(gate)
        if not _gate_date_valid(gate) and not payload.allow_expired_gate:
            raise HTTPException(
                status_code=409,
                detail="通门证日期无效，请确认是否继续打印。",
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
        token = uuid.uuid4().hex
        pdf_path = config.print_spool_dir / f"{token}.pdf"
        gate, cached_pdf, result = service.ensure_gatepass_pdf(gate)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached_pdf, pdf_path)
        snapshot = {
            "gate_container_no": gate.get("container_no", ""),
            "gate_seal_no": gate.get("seal_no", ""),
            "gate_pass_no": gate.get("gate_pass_no", ""),
            "gate_planned_departure_at": gate.get("planned_departure_at", ""),
            "gate_date_valid": _gate_date_valid(gate),
            "ocr_container_no": (container_ocr or {}).get("observed_text", ""),
            "ocr_seal_no": (seal_ocr or {}).get("observed_text", ""),
            "verdict": verdict,
            "container_ocr": container_ocr,
            "seal_ocr": seal_ocr,
            "cpm_record": record,
            "das_result": result.get("message", "success"),
        }
        with pending_prints_lock:
            pending_prints[token] = {
                "cpm_id": payload.cpm_id,
                "snapshot": snapshot,
                "operator": payload.operator.strip(),
                "reason": payload.reason.strip(),
                "created_at": time.monotonic(),
                "pdf_path": str(pdf_path),
            }
        return {
            "ok": True,
            "pending_confirmation": True,
            "token": token,
            "print_preview_url": f"/api/print-preview/{token}",
            "pdf_url": f"/api/print-document/{token}",
            **result,
        }
    except HTTPException:
        raise
    except GatePassNotFound as exc:
        raise HTTPException(
            status_code=409, detail="DAS系统中无门证，请到GERP系统发送门证。"
        ) from exc
    except LoginRequired as exc:
        database.update_session("logged_out", message=str(exc))
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/print-document/{token}")
def print_document(token: str):
    pending = _get_pending_print(token)
    path = _pending_pdf_path(pending)
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="smartgpms-gate-pass.pdf"'},
    )


@app.get("/api/print-preview/{token}", response_class=HTMLResponse)
def print_preview(token: str):
    pending = _get_pending_print(token)
    _pending_pdf_path(pending)
    document_url = f"/api/print-document/{token}"
    return HTMLResponse(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>smartGPMS 门证打印</title>
  <style>
    *{{box-sizing:border-box}}html,body{{height:100%;margin:0;font-family:"Microsoft YaHei",sans-serif;background:#eef2f5;color:#172b3a}}
    body{{display:flex;flex-direction:column}}.bar{{height:56px;display:flex;align-items:center;gap:12px;padding:0 18px;background:#fff;border-bottom:1px solid #d9e1e7}}
    .bar strong{{margin-right:auto}}button{{border:0;border-radius:7px;padding:9px 22px;cursor:pointer;font-weight:700}}#print{{background:#168c61;color:#fff}}#close{{background:#e8edf1;color:#304654}}
    #status{{font-size:13px;color:#617582}}iframe{{width:100%;flex:1;border:0;background:#525659}}
  </style>
</head>
<body>
  <div class="bar"><strong>smartGPMS 门证打印预览</strong><span id="status">正在准备系统打印窗口…</span><button id="print">打印</button><button id="close">关闭</button></div>
  <iframe id="document" title="门证 PDF" src="{document_url}"></iframe>
  <script>
    const frame=document.getElementById("document");
    const status=document.getElementById("status");
    function startPrint(){{
      status.textContent="如果打印窗口没有自动出现，请点击右侧“打印”按钮";
      try{{frame.contentWindow.focus();frame.contentWindow.print();}}
      catch(error){{window.print();}}
    }}
    frame.addEventListener("load",()=>setTimeout(startPrint,700),{{once:true}});
    document.getElementById("print").addEventListener("click",startPrint);
    document.getElementById("close").addEventListener("click",()=>window.close());
  </script>
</body>
</html>"""
    )


@app.post("/api/print/confirm")
def confirm_print(payload: PrintConfirmation):
    _cleanup_pending_prints()
    with pending_prints_lock:
        pending = pending_prints.pop(payload.token, None)
    if not pending:
        raise HTTPException(status_code=404, detail="打印确认已失效")
    if not payload.completed:
        _delete_pending_pdf(pending)
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
    _delete_pending_pdf(pending)
    return {"ok": True, "recorded": True, **state}


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": app.version,
        "session": database.session().get("status", "logged_out"),
    }
