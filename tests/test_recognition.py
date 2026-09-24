from PIL import Image

from smartgpms.recognition import (
    OCRItem,
    extract_container_candidates,
    find_seal_candidate,
    save_crop,
)


def test_short_seal_number_is_considered():
    items = [
        OCRItem(text="FX49", score=0.95, box=[[0, 0], [40, 0], [40, 10], [0, 10]]),
        OCRItem(text="4975", score=0.93, box=[[42, 0], [82, 0], [82, 10], [42, 10]]),
    ]
    result = find_seal_candidate(items, "FX494975")
    assert result is not None
    assert result["observed"] == "FX494975"
    assert result["matches_expected"]
    assert result["confidence"] == 0.94


def test_container_crop_excludes_text_after_number():
    items = [
        OCRItem(text="CMAU", score=0.98, box=[[0, 0], [40, 0], [40, 10], [0, 10]]),
        OCRItem(
            text="4338290", score=0.97, box=[[42, 0], [110, 0], [110, 10], [42, 10]]
        ),
        OCRItem(text="45G1", score=0.96, box=[[0, 20], [40, 20], [40, 30], [0, 30]]),
    ]
    best = extract_container_candidates(items, "CMAU4338290")[0]
    assert best["source_indices"] == [0, 1]


def test_complete_vertical_container_crop_is_tight_and_saved_horizontally(tmp_path):
    image = Image.new("RGB", (1280, 720), "white")
    items = [
        OCRItem(
            text="754634 9",
            score=0.97,
            box=[[443, 42], [491, 43], [487, 186], [440, 185]],
        ),
        OCRItem(
            text="FCIU",
            score=0.98,
            box=[[440, 203], [481, 203], [481, 273], [440, 273]],
        ),
    ]
    target = tmp_path / "crop.jpg"

    save_crop(
        image,
        items,
        [1, 0],
        target,
        target_type="container",
        complete=True,
        source_rotation=90,
    )

    with Image.open(target) as crop:
        assert crop.width > crop.height
        assert crop.width < image.width / 2
