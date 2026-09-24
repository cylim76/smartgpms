from pathlib import Path

from PIL import Image

from smartgpms.check_digit_fallback import (
    apply_context_check_digit_fallback,
    create_context_variants,
)
from smartgpms.recognition import OCRItem


def item(text, score=0.99, left=0, right=100):
    return OCRItem(
        text=text,
        score=score,
        box=[[left, 10], [right, 10], [right, 50], [left, 50]],
    )


class ContextEngine:
    def recognize(self, image_path: Path):
        if image_path.stem == "context_2x":
            return [item("TXGU", left=10, right=100), item("607166 9", left=110)]
        return [item("TXGU", left=10, right=100), item("607166", left=110)]


def source_candidate():
    return {
        "observed": "TXGU607166",
        "suggested": "TXGU6071669",
        "complete": False,
        "valid": False,
        "verification": "unverified",
        "matches_expected": False,
        "suggested_matches_expected": True,
        "observed_check_digit": None,
        "calculated_check_digit": "9",
        "confidence": 0.99,
        "extraction_score": 0.90,
        "corrections": 0,
        "source_indices": [0, 1],
        "source_texts": ["TXGU", "607166"],
    }


def test_context_variants_match_the_validated_profiles():
    image = Image.new("RGB", (200, 80), "brown")

    variants = create_context_variants(image)

    assert set(variants) == {"context_2x", "context_4x", "grayscale_4x"}
    assert variants["context_2x"].size == (400, 160)
    assert variants["context_4x"].size == (800, 320)
    for variant in variants.values():
        variant.close()


def test_context_fallback_requires_observed_full_valid_number(tmp_path):
    completed, metadata = apply_context_check_digit_fallback(
        Image.new("RGB", (200, 80), "brown"),
        source_candidate(),
        ContextEngine(),
        tmp_path,
    )

    assert completed is not None
    assert completed["observed"] == "TXGU6071669"
    assert completed["verification"] == "verified"
    assert completed["check_digit_source"] == "context_ocr"
    assert completed["matches_expected"] is True
    assert metadata["status"] == "applied"
    assert metadata["selected_variant"] == "context_2x"


def test_context_fallback_rejects_an_invalid_or_different_number(tmp_path):
    class WrongEngine:
        def recognize(self, _image_path):
            return [item("TXGU"), item("607166 4", left=110)]

    completed, metadata = apply_context_check_digit_fallback(
        Image.new("RGB", (200, 80), "brown"),
        source_candidate(),
        WrongEngine(),
        tmp_path,
    )

    assert completed is None
    assert metadata["status"] == "not_found"
