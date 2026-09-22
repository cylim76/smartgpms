from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageOps

from .das_browser import DasBrowser
from .database import Database, now_text
from .recognition import (
    RapidOCREngine,
    extract_container_candidates,
    find_seal_candidate,
    save_crop,
)


class PhotoPipeline:
    """Download once, rotate for human orientation, and cache RapidOCR evidence."""

    def __init__(self, database: Database, browser: DasBrowser, cache_dir: Path):
        self.database = database
        self.browser = browser
        self.cache_dir = cache_dir
        self.engine = RapidOCREngine()

    @staticmethod
    def _prepared(image: Image.Image) -> Image.Image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        if max(image.size) > 2400:
            image.thumbnail((2400, 2400))
        return ImageEnhance.Contrast(image).enhance(1.12)

    def _orientations(self, source: Path, work_dir: Path):
        with Image.open(source) as loaded:
            original = self._prepared(loaded)
        for angle in (0, 90, 180, 270):
            image = original if angle == 0 else original.rotate(angle, expand=True)
            path = work_dir / f"orientation_{angle}.jpg"
            image.save(path, "JPEG", quality=94)
            yield angle, image, path, self.engine.recognize(path)

    def process(self, cpm_id: str, detail: dict[str, Any]) -> dict[str, Any]:
        self.database.update_ocr_status(cpm_id, "processing")
        try:
            return self._process(cpm_id, detail)
        except Exception:
            self.database.update_ocr_status(cpm_id, "failed")
            raise

    def _process(self, cpm_id: str, detail: dict[str, Any]) -> dict[str, Any]:
        root = self.cache_dir / cpm_id
        root.mkdir(parents=True, exist_ok=True)
        expected_container = str(detail.get("container_no", ""))
        expected_seal = str(detail.get("seal_no", ""))
        best_container: tuple[float, dict, Image.Image, list, int, dict] | None = None
        best_seal: tuple[float, dict, Image.Image, list, int, dict] | None = None

        for sequence, photo in enumerate(detail.get("photos", []), start=1):
            source_url = photo["source_url"]
            suffix = Path(source_url.split("?", 1)[0]).suffix.lower()
            if suffix not in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}:
                suffix = ".jpg"
            source = root / f"{int(photo.get('step_no', 0))}_{sequence}{suffix}"
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
            work = root / f"photo_{row['id']}"
            work.mkdir(parents=True, exist_ok=True)
            for angle, image, _path, items in self._orientations(source, work):
                candidates = extract_container_candidates(items, expected_container)
                if candidates:
                    candidate = candidates[0]
                    score = (
                        float(candidate.get("confidence", 0))
                        + (2 if candidate.get("matches_expected") else 0)
                        + (1 if candidate.get("valid") else 0)
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
                seal = find_seal_candidate(items, expected_seal)
                if seal:
                    score = float(seal.get("similarity", 0)) + (
                        2 if seal.get("matches_expected") else 0
                    )
                    if best_seal is None or score > best_seal[0]:
                        best_seal = (score, seal, image.copy(), items, angle, row)

        def persist(target_type: str, selected):
            if selected is None:
                return None
            _score, value, image, items, angle, photo = selected
            crop = root / f"best_{target_type}.jpg"
            save_crop(image, items, value["source_indices"], crop)
            result = {
                **value,
                "crop_path": str(crop),
                "rotation": angle,
                "source_hash": photo["source_hash"],
                "photo_id": photo["id"],
                "engine_version": "rapidocr-3",
                "model_version": "onnx-cpu",
                "preprocessing_version": "contrast-rotation-v1",
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
        return {"status": status, "container": container_result, "seal": seal_result}
