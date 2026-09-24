from smartgpms.ai_postprocess import (
    extract_container_candidates,
    extract_seal_candidates,
    fuse_seal_candidates,
)
from smartgpms.recognition import OCRItem


def item(text, score=0.99, left=0, top=0, right=100, bottom=20):
    return OCRItem(
        text=text,
        score=score,
        box=[[left, top], [right, top], [right, bottom], [left, bottom]],
    )


def seal(value, confidence, score):
    return {
        "observed": value,
        "confidence": confidence,
        "extraction_score": score,
        "source_indices": [0],
        "source_texts": [value],
        "_run_key": f"1:{value}",
        "text_is_horizontal": True,
    }


def test_container_postprocessing_keeps_observed_check_digit_mismatch():
    result = extract_container_candidates([item("CMAU4338291")])[0]
    assert result["observed"] == "CMAU4338291"
    assert result["suggested"] == "CMAU4338290"
    assert result["verification"] == "mismatch"


def test_container_candidate_excludes_unrelated_group_text_from_crop_indices():
    candidates = extract_container_candidates(
        [item("FCIU 754634 9", left=400, right=620), item("TARE", left=400, top=80)]
    )
    assert candidates[0]["observed"] == "FCIU7546349"
    assert candidates[0]["source_indices"] == [0]


def test_seal_extractor_filters_container_and_date_noise():
    candidates = extract_seal_candidates(
        [
            item("CMAU4338290"),
            item("2026-09-23"),
            item("MLCN4062676"),
        ]
    )
    assert [candidate["observed"] for candidate in candidates] == ["MLCN4062676"]


def test_seal_extractor_filters_container_capacity_and_weight_noise():
    candidates = extract_seal_candidates(
        [item("76.4 CU.M."), item("8.160 LBS"), item("M7295518")]
    )
    assert [candidate["observed"] for candidate in candidates] == ["M7295518"]


def test_seal_fusion_prefers_exact_expected_value_over_high_confidence_noise():
    fused = fuse_seal_candidates(
        {
            "original": [seal("M7295518", 0.96, 0.97)],
            "ccw90": [seal("764CUM", 0.999, 1.0)],
        },
        expected="M7295518",
    )
    assert fused[0]["observed"] == "M7295518"
    assert fused[0]["matches_expected"]


def test_unreliable_expected_seal_does_not_override_stronger_photo_candidate():
    fused = fuse_seal_candidates(
        {
            "original": [seal("96FLORENS", 0.997, 0.90)],
            "ccw90": [seal("JH0001121", 0.997, 1.0)],
        },
        expected="LGS",
    )
    assert fused[0]["observed"] == "JH0001121"


def test_seal_fusion_prefers_rotated_structured_value():
    fused = fuse_seal_candidates(
        {
            "original": [seal("1000508518", 0.1534, 0.2253)],
            "cw90": [seal("MLCN4062676", 0.9901, 0.9932)],
            "ccw90": [seal("110402120220", 0.2120, 0.2652)],
        }
    )
    assert fused[0]["observed"] == "MLCN4062676"
    assert fused[0]["orientation"] == "cw90"


def test_seal_fusion_rewards_supported_complete_split_number():
    fused = fuse_seal_candidates(
        {
            "original": [seal("WWH200000", 0.55, 0.61)],
            "cw90": [
                seal("0418426", 0.9989, 0.8002),
                seal("230418426", 0.9993, 0.7155),
            ],
        }
    )
    assert fused[0]["observed"] == "230418426"
    assert "supported_complete_extension" in fused[0]["selection_reasons"]
