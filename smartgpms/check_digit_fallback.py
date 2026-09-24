from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps

from .ai_postprocess import extract_container_candidates
from .iso6346 import append_check_digit, validate_container_number
from .recognition import OCRItem


class OCREngine(Protocol):
    def recognize(self, image_path: Path) -> list[OCRItem]: ...


def create_context_variants(image: Image.Image) -> dict[str, Image.Image]:
    """Create the v0.4.1-style context crops without requiring PaddleOCR."""

    context_2x = image.resize(
        (image.width * 2, image.height * 2), Image.Resampling.LANCZOS
    )
    context_4x = image.resize(
        (image.width * 4, image.height * 4), Image.Resampling.LANCZOS
    )
    return {
        "context_2x": context_2x,
        "context_4x": context_4x,
        "grayscale_4x": ImageOps.autocontrast(
            ImageOps.grayscale(context_4x)
        ).convert("RGB"),
    }


def _complete_candidate(
    source: dict[str, Any], observed: dict[str, Any], variant: str
) -> dict[str, Any]:
    completed = str(observed["observed"])
    suggested = append_check_digit(str(source["observed"]))
    confidence = round(
        (float(source.get("confidence", 0)) + float(observed.get("confidence", 0)))
        / 2,
        6,
    )
    return {
        **source,
        "observed": completed,
        "suggested": suggested,
        "complete": True,
        "valid": True,
        "verification": "verified",
        "matches_expected": bool(
            source.get("suggested_matches_expected") and completed == suggested
        ),
        "suggested_matches_expected": bool(
            source.get("suggested_matches_expected") and completed == suggested
        ),
        "observed_check_digit": completed[-1],
        "calculated_check_digit": suggested[-1],
        "check_digit_source": "context_ocr",
        "confidence": confidence,
        "extraction_score": max(
            float(source.get("extraction_score", 0)),
            float(observed.get("extraction_score", 0)),
        ),
        "selection_reasons": [
            *list(source.get("selection_reasons", [])),
            "context_crop_general_ocr",
            f"verified_full_number:{variant}",
        ],
    }


def apply_context_check_digit_fallback(
    context_image: Image.Image,
    candidate: dict[str, Any],
    engine: OCREngine,
    work_dir: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Observe a missing check digit again and accept only a valid full number."""

    observed_prefix = str(candidate.get("observed", ""))
    if (
        len(observed_prefix) != 10
        or candidate.get("verification") != "unverified"
    ):
        return None, {"status": "not_needed"}

    variants = create_context_variants(context_image)
    evaluations: dict[str, dict[str, Any]] = {}
    eligible: list[tuple[str, dict[str, Any]]] = []
    try:
        for variant, image in variants.items():
            path = work_dir / f"{variant}.png"
            image.save(path, "PNG")
            candidates = extract_container_candidates(engine.recognize(path))
            matching = [
                item
                for item in candidates
                if len(str(item.get("observed", ""))) == 11
                and str(item["observed"])[:10] == observed_prefix
                and item.get("verification") == "verified"
                and validate_container_number(str(item["observed"]))
            ]
            best = matching[0] if matching else (candidates[0] if candidates else None)
            evaluations[variant] = {
                "observed": best.get("observed") if best else None,
                "verification": best.get("verification") if best else None,
                "confidence": best.get("confidence") if best else None,
                "extraction_score": best.get("extraction_score") if best else None,
            }
            eligible.extend((variant, item) for item in matching)
    finally:
        for image in variants.values():
            image.close()

    metadata: dict[str, Any] = {
        "status": "not_found",
        "strategy": "context_crop_general_ocr",
        "variants": evaluations,
    }
    if not eligible:
        return None, metadata
    selected_variant, selected = max(
        eligible,
        key=lambda item: (
            float(item[1].get("extraction_score", 0)),
            float(item[1].get("confidence", 0)),
            -int(item[1].get("corrections", 0)),
        ),
    )
    completed = _complete_candidate(candidate, selected, selected_variant)
    metadata.update(
        {
            "status": "applied",
            "selected_variant": selected_variant,
            "observed": completed["observed"],
            "verification": completed["verification"],
        }
    )
    return completed, metadata
