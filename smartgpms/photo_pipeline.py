from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from PIL import Image, ImageOps

from .ai_postprocess import (
    extract_container_candidates,
    extract_seal_candidates,
    fuse_seal_candidates,
)
from .check_digit_fallback import apply_context_check_digit_fallback
from .das_browser import DasBrowser
from .database import Database, now_text
from .recognition import RapidOCREngine, save_crop
from .time_utils import business_now


class PhotoPipeline:
    """Download once, rotate for human orientation, and cache RapidOCR evidence."""

    def __init__(
        self,
        database: Database,
        browser: DasBrowser,
        cache_dir: Path,
        evidence_dir: Path | None = None,
    ):
        self.database = database
        self.browser = browser
        self.cache_dir = cache_dir
        self.evidence_dir = evidence_dir or cache_dir.parent / "evidence"
        self.engine = RapidOCREngine()

    @staticmethod
    def _prepared(image: Image.Image) -> Image.Image:
        return ImageOps.exif_transpose(image).convert("RGB")

    @staticmethod
    def _ensure_thumbnail(source: Path, target: Path) -> Path:
        """Create the small list preview once instead of resizing an original per request."""
        if target.is_file() and target.stat().st_size > 0:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as loaded:
            preview = ImageOps.exif_transpose(loaded).convert("RGB")
            preview.thumbnail((256, 160), Image.Resampling.LANCZOS)
            preview.save(target, "JPEG", quality=78, optimize=True)
        return target

    def _orientations(self, source: Path, work_dir: Path):
        with Image.open(source) as loaded:
            original = self._prepared(loaded)
        variants = (
            ("original", 0, original),
            ("cw90", 270, original.transpose(Image.Transpose.ROTATE_270)),
            ("ccw90", 90, original.transpose(Image.Transpose.ROTATE_90)),
            ("rotate180", 180, original.transpose(Image.Transpose.ROTATE_180)),
        )
        for orientation, angle, image in variants:
            path = work_dir / f"{orientation}.png"
            image.save(path, "PNG")
            yield orientation, angle, image, path, self.engine.recognize(path)

    @staticmethod
    def _storage_period(begin_date: str) -> tuple[str, ...]:
        text = str(begin_date or "").strip()
        match = re.search(r"(?<!\d)(\d{4})[-/.](\d{1,2})(?:[-/.]|\D|$)", text)
        if not match:
            match = re.search(r"(?<!\d)(\d{4})(\d{2})\d{2}(?!\d)", text)
        if match and 1 <= int(match.group(2)) <= 12:
            return match.group(1), f"{int(match.group(2)):02d}"
        return ("unknown",)

    @staticmethod
    def _original_filename(source_url: str, photo_label: str = "") -> str:
        decoded_path = unquote(urlparse(source_url).path).replace("\\", "/")
        filename = decoded_path.rsplit("/", 1)[-1]
        label = str(photo_label or "").strip().upper()
        if re.fullmatch(r"F\d+", label):
            filename = f"{label}{Path(filename).suffix.lower()}"
        forbidden = '<>:"/\\|?*'
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{number}" for number in range(1, 10)),
            *(f"LPT{number}" for number in range(1, 10)),
        }
        if (
            not filename
            or filename in {".", ".."}
            or any(character in filename for character in forbidden)
            or filename.endswith((" ", "."))
            or Path(filename).stem.upper() in reserved
        ):
            raise ValueError(f"DAS 原始文件名无法跨平台安全保存：{filename!r}")
        return filename

    def process(
        self,
        cpm_id: str,
        detail: dict[str, Any],
        progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        self.database.update_ocr_status(cpm_id, "processing")
        try:
            return self._process(cpm_id, detail, progress)
        except Exception:
            self.database.update_ocr_status(cpm_id, "failed")
            raise

    def _process(
        self,
        cpm_id: str,
        detail: dict[str, Any],
        progress: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        expected_container = str(detail.get("container_no", ""))
        safe_container = "".join(
            character for character in expected_container.upper() if character.isalnum()
        )
        period = self._storage_period(str(detail.get("begin_date", "")))
        root = self.cache_dir.joinpath(
            *period, f"{cpm_id}_{safe_container or 'UNKNOWN'}"
        )
        root.mkdir(parents=True, exist_ok=True)
        ocr_root = root / "_ocr"
        ocr_root.mkdir(parents=True, exist_ok=True)
        thumbnail_root = root / "_thumb"
        thumbnail_root.mkdir(parents=True, exist_ok=True)
        expected_seal = str(detail.get("seal_no", ""))
        best_container: tuple[float, dict, Image.Image, list, int, dict] | None = None
        best_seal: tuple[float, dict, Image.Image, list, int, dict] | None = None
        seal_candidates: dict[str, list[dict]] = {}
        orientation_runs: dict[str, tuple[Image.Image, list, int, dict]] = {}
        container_photo_ids: set[int] = set()
        has_stage4 = False
        cached_photos = {
            str(row.get("source_url", "")): row
            for row in self.database.photos_for_cpm(cpm_id)
        }

        occupied_names: dict[str, str] = {}
        stage4_photos = [
            photo
            for photo in detail.get("photos", [])
            if int(photo.get("step_no", 0)) == 4
        ]
        total_photos = len(stage4_photos)
        for photo_index, photo in enumerate(stage4_photos, start=1):
            has_stage4 = True
            source_url = photo["source_url"]
            filename = self._original_filename(source_url, str(photo.get("label", "")))
            collision_key = filename.casefold()
            previous_url = occupied_names.get(collision_key)
            if previous_url and previous_url != source_url:
                raise ValueError(f"DAS 两张照片的原始文件名重复，为避免覆盖已停止：{filename}")
            occupied_names[collision_key] = source_url
            source = root / filename
            cached = cached_photos.get(source_url)
            cached_path = self._safe_cache_file(str((cached or {}).get("local_path", "")))
            if (
                cached
                and cached.get("cache_status") == "ready"
                and cached_path is not None
                and cached.get("source_hash")
            ):
                if cached_path != source:
                    if source.exists():
                        raise ValueError(
                            f"DAS 照片编号对应的目标文件已存在，为避免覆盖已停止：{filename}"
                        )
                    cached_path.replace(source)
                    row = self.database.save_photo(
                        cpm_id,
                        {
                            **photo,
                            "local_path": str(source),
                            "source_hash": cached.get("source_hash", ""),
                            "downloaded_at": cached.get("downloaded_at"),
                            "cache_status": "ready",
                        },
                    )
                else:
                    source = cached_path
                    row = cached
            else:
                if progress:
                    progress(
                        "download",
                        f"下载四阶段照片 {photo_index}/{total_photos}",
                    )
                downloaded = self.browser.download(source_url, source)
                row = self.database.save_photo(
                    cpm_id,
                    {
                        **photo,
                        "local_path": str(source),
                        "source_hash": downloaded["sha256"],
                        "downloaded_at": now_text(),
                        "cache_status": "ready",
                    },
                )
            thumbnail = self._ensure_thumbnail(
                source, thumbnail_root / f"{source.stem}_thumb.jpg"
            )
            if str(row.get("thumbnail_path", "")) != str(thumbnail):
                row = self.database.save_photo(
                    cpm_id,
                    {
                        **photo,
                        "local_path": str(source),
                        "thumbnail_path": str(thumbnail),
                        "source_hash": row.get("source_hash", ""),
                        "downloaded_at": row.get("downloaded_at"),
                        "cache_status": "ready",
                    },
                )
            if progress:
                progress("ocr", f"RapidOCR 识别照片 {photo_index}/{total_photos}")
            with tempfile.TemporaryDirectory(
                dir=ocr_root, prefix=f"{source.stem}_{row['id']}_"
            ) as work_name:
                work = Path(work_name)
                for orientation, angle, image, _path, items in self._orientations(
                    source, work
                ):
                    run_key = f"{row['id']}:{orientation}"
                    orientation_runs[run_key] = (image.copy(), items, angle, row)
                    candidates = extract_container_candidates(items, expected_container)
                    if candidates:
                        candidate = candidates[0]
                        candidate = {**candidate, "orientation": orientation}
                        if (
                            candidate.get("matches_expected")
                            or candidate.get("suggested_matches_expected")
                            or candidate.get("valid")
                        ):
                            container_photo_ids.add(int(row["id"]))
                        score = (
                            float(candidate.get("extraction_score", 0))
                            + 5.0 * int(bool(candidate.get("matches_expected")))
                            + 2.0
                            * int(bool(candidate.get("suggested_matches_expected")))
                            + 1.0 * int(bool(candidate.get("valid")))
                        )
                        if best_container is None or score > best_container[0]:
                            best_container = (
                                score,
                                candidate,
                                image.copy(),
                                items,
                                angle,
                                row,
                            )
                    for seal in extract_seal_candidates(items):
                        seal_candidates.setdefault(orientation, []).append(
                            {**seal, "_run_key": run_key}
                        )

        if best_container is not None:
            container_photo_ids.add(int(best_container[-1]["id"]))
            score, candidate, image, items, angle, row = best_container
            if (
                len(str(candidate.get("observed", ""))) == 10
                and candidate.get("verification") == "unverified"
            ):
                with tempfile.TemporaryDirectory(
                    dir=ocr_root, prefix=f"{Path(str(row['local_path'])).stem}_check_"
                ) as fallback_name:
                    fallback_work = Path(fallback_name)
                    context_path = fallback_work / "context.jpg"
                    save_crop(
                        image,
                        items,
                        candidate["source_indices"],
                        context_path,
                        target_type="container",
                        complete=False,
                        source_rotation=angle,
                    )
                    with Image.open(context_path) as opened:
                        context_image = opened.convert("RGB")
                    recovered, postprocessing = apply_context_check_digit_fallback(
                        context_image,
                        candidate,
                        self.engine,
                        fallback_work,
                    )
                    context_image.close()
                candidate = {
                    **(recovered or candidate),
                    "check_digit_postprocessing": postprocessing,
                }
                best_container = (score, candidate, image, items, angle, row)
        eligible_seals = {
            orientation: [
                candidate
                for candidate in candidates
                if int(orientation_runs[candidate["_run_key"]][3]["id"])
                not in container_photo_ids
            ]
            for orientation, candidates in seal_candidates.items()
        }
        fused_seals = fuse_seal_candidates(eligible_seals, expected_seal)
        if fused_seals:
            seal = fused_seals[0]
            image, items, angle, row = orientation_runs[seal["_run_key"]]
            best_seal = (
                float(seal.get("extraction_score", 0)),
                seal,
                image,
                items,
                angle,
                row,
            )

        downloaded_count = sum(
            1
            for row in self.database.photos_for_cpm(cpm_id)
            if int(row.get("step_no", 0)) == 4
            and row.get("cache_status") == "ready"
            and self._safe_cache_file(str(row.get("local_path", ""))) is not None
        )
        self.database.update_photo_inventory(cpm_id, total_photos, downloaded_count)
        if has_stage4 and total_photos >= 3 and downloaded_count >= 3:
            self.database.mark_archive_status(cpm_id, 5)
        elif has_stage4:
            self.database.mark_archive_status(
                cpm_id, min(4, int(detail.get("business_stage", 4)))
            )

        def persist(target_type: str, selected):
            if selected is None:
                return None
            _score, value, image, items, angle, photo = selected
            original_stem = Path(str(photo["local_path"])).stem
            crop = ocr_root / f"{original_stem}_{target_type}_crop.jpg"
            # A check digit recovered from the expanded context crop is not part of
            # the original OCR box indices. Keep that wider evidence crop so the
            # displayed image includes the digit that was actually re-read.
            crop_complete = bool(value.get("complete")) and not (
                target_type == "container"
                and value.get("check_digit_source") == "context_ocr"
            )
            save_crop(
                image,
                items,
                value["source_indices"],
                crop,
                target_type=target_type,
                complete=crop_complete,
                source_rotation=angle,
            )
            result = {
                **value,
                "crop_path": str(crop),
                "rotation": angle,
                "source_hash": photo["source_hash"],
                "photo_id": photo["id"],
                "engine_version": "rapidocr-3",
                "model_version": "onnx-cpu",
                "preprocessing_version": "das-photo-ai-check-digit-fallback-v4",
            }
            if target_type == "seal" and not result.get("confidence"):
                result["confidence"] = float(result.get("similarity", 0))
            self.database.save_ocr(cpm_id, int(photo["id"]), target_type, result)
            return result

        self.database.clear_ocr(cpm_id)
        container_result = persist("container", best_container)
        seal_result = persist("seal", best_seal)
        status = "ready" if container_result and seal_result else "review"
        self.database.update_ocr_status(cpm_id, status)
        return {
            "status": status,
            "container": container_result,
            "seal": seal_result,
            "available_photo_count": total_photos,
            "downloaded_photo_count": downloaded_count,
        }

    @staticmethod
    def _parsed_time(value: str) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text[:25])
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=business_now().tzinfo)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                return datetime.strptime(text[:19], fmt).replace(
                    tzinfo=business_now().tzinfo
                )
            except ValueError:
                continue
        return None

    def _safe_cache_file(self, value: str) -> Path | None:
        if not value:
            return None
        path = Path(value).resolve()
        root = self.cache_dir.resolve()
        return path if root in path.parents and path.is_file() else None

    def _remove_intermediate_directories(self) -> int:
        root = self.cache_dir.resolve()
        if not root.is_dir():
            return 0
        removed = 0
        candidates = [
            path
            for path in root.rglob("*")
            if path.is_dir()
            and (path.name.startswith("photo_") or path.parent.name == "_ocr")
        ]
        for path in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
            resolved = path.resolve()
            if root not in resolved.parents or not resolved.is_dir():
                continue
            shutil.rmtree(resolved)
            removed += 1
        return removed

    def cleanup_cache(
        self,
        *,
        photo_retention_days: int = 30,
        crop_retention_days: int = 60,
        print_retention_days: int = 365,
        cache_max_bytes: int = 20 * 1024 * 1024 * 1024,
        min_free_bytes: int = 10 * 1024 * 1024 * 1024,
        current: datetime | None = None,
    ) -> dict[str, int]:
        """Clean reproducible cache while preserving print evidence and metadata."""
        current = current or business_now()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        entries = self.database.cache_cleanup_entries()
        active = set(entries["active_cpm_ids"])
        removed_intermediate = self._remove_intermediate_directories()
        removed_photos = removed_crops = removed_prints = released_bytes = 0
        photo_cutoff = current - timedelta(days=photo_retention_days)
        crop_cutoff = current - timedelta(days=crop_retention_days)
        print_cutoff = current - timedelta(days=print_retention_days)

        photo_rows = sorted(
            entries["photos"],
            key=lambda row: self._parsed_time(
                str(row.get("begin_date") or row.get("downloaded_at") or "")
            )
            or datetime.min.replace(tzinfo=current.tzinfo),
        )

        def remove_photo(row: dict) -> bool:
            nonlocal removed_photos, released_bytes
            if str(row["cpm_id"]) in active:
                return False
            paths = {
                path
                for value in (row.get("local_path", ""), row.get("thumbnail_path", ""))
                if (path := self._safe_cache_file(str(value))) is not None
            }
            for path in paths:
                size = path.stat().st_size
                path.unlink()
                released_bytes += size
            self.database.mark_photo_cache_cleaned(int(row["id"]), str(row["cpm_id"]))
            removed_photos += 1
            return True

        remaining: list[dict] = []
        for row in photo_rows:
            basis = self._parsed_time(
                str(row.get("begin_date") or row.get("downloaded_at") or "")
            )
            if basis is not None and basis < photo_cutoff:
                remove_photo(row)
            else:
                remaining.append(row)

        def cache_size() -> int:
            return sum(
                path.stat().st_size
                for path in self.cache_dir.rglob("*")
                if path.is_file()
            )

        total = cache_size()
        for row in remaining:
            free = shutil.disk_usage(self.cache_dir).free
            if total <= cache_max_bytes and free >= min_free_bytes:
                break
            before = released_bytes
            if remove_photo(row):
                total = max(0, total - (released_bytes - before))

        for row in entries["crops"]:
            if str(row["cpm_id"]) in active:
                continue
            created = self._parsed_time(str(row.get("created_at", "")))
            if created is None or created >= crop_cutoff:
                continue
            path = self._safe_cache_file(str(row.get("crop_path", "")))
            if path is not None:
                size = path.stat().st_size
                path.unlink()
                released_bytes += size
            self.database.mark_ocr_cache_cleaned(int(row["id"]), str(row["cpm_id"]))
            removed_crops += 1

        evidence_root = self.evidence_dir.resolve()
        for row in self.database.print_cleanup_entries():
            printed = self._parsed_time(str(row.get("printed_at", "")))
            if printed is None or printed >= print_cutoff:
                continue
            path_text = str(row.get("evidence_dir", ""))
            if path_text:
                path = Path(path_text).resolve()
                if evidence_root in path.parents and path.is_dir():
                    released_bytes += sum(
                        item.stat().st_size for item in path.rglob("*") if item.is_file()
                    )
                    shutil.rmtree(path)
            self.database.delete_print_history(int(row["id"]))
            removed_prints += 1

        return {
            "intermediate_dirs": removed_intermediate,
            "photos": removed_photos,
            "crops": removed_crops,
            "print_records": removed_prints,
            "released_bytes": released_bytes,
        }
