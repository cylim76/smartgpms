"""Post-processing adapted from the read-only ``das_photo_ai`` project.

The source project remains untouched.  This module keeps the same important
selection rules while adapting its OCRDocument model to smartGPMS RapidOCR
items and dictionary-shaped cache records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .iso6346 import append_check_digit, normalize, validate_container_number
from .recognition import OCRItem


@dataclass(frozen=True)
class TextGroup:
    items: tuple[tuple[int, OCRItem], ...]

    @property
    def text(self) -> str:
        return "".join(item.text for _index, item in self.items)

    @property
    def spaced_text(self) -> str:
        return " ".join(item.text for _index, item in self.items)

    @property
    def confidence(self) -> float:
        return sum(item.score for _index, item in self.items) / len(self.items)

    @property
    def source_indices(self) -> list[int]:
        return [index for index, _item in self.items]

    @property
    def source_texts(self) -> list[str]:
        return [item.text for _index, item in self.items]


def _box(item: OCRItem) -> tuple[float, float, float, float] | None:
    if not item.box:
        return None
    xs = [float(point[0]) for point in item.box]
    ys = [float(point[1]) for point in item.box]
    return min(xs), min(ys), max(xs), max(ys)


def _center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _group_is_horizontal(group: TextGroup) -> bool:
    horizontal = vertical = 0
    for _index, item in group.items:
        bounds = _box(item)
        if bounds is None:
            continue
        width = max(1.0, bounds[2] - bounds[0])
        height = max(1.0, bounds[3] - bounds[1])
        if width >= height:
            horizontal += 1
        else:
            vertical += 1
    return horizontal >= vertical


def _spatially_related(left: OCRItem, right: OCRItem) -> bool:
    left_box, right_box = _box(left), _box(right)
    if left_box is None or right_box is None:
        return False
    left_x, left_y = _center(left_box)
    right_x, right_y = _center(right_box)
    left_w, left_h = max(1.0, left_box[2] - left_box[0]), max(
        1.0, left_box[3] - left_box[1]
    )
    right_w, right_h = max(1.0, right_box[2] - right_box[0]), max(
        1.0, right_box[3] - right_box[1]
    )
    same_row = abs(left_y - right_y) <= max(left_h, right_h) * 1.5
    row_gap = max(
        0.0, max(left_box[0], right_box[0]) - min(left_box[2], right_box[2])
    )
    if same_row and row_gap <= max(250.0, max(left_w, right_w) * 3.0):
        return True
    same_column = abs(left_x - right_x) <= max(left_w, right_w) * 1.5
    column_gap = max(
        0.0, max(left_box[1], right_box[1]) - min(left_box[3], right_box[3])
    )
    return same_column and column_gap <= max(250.0, max(left_h, right_h) * 4.0)


def generate_text_groups(items: list[OCRItem], max_items: int = 3) -> list[TextGroup]:
    indexed = list(enumerate(items))
    groups: list[TextGroup] = []
    seen: set[tuple[int, ...]] = set()

    def add(values: tuple[tuple[int, OCRItem], ...]) -> None:
        key = tuple(index for index, _item in values)
        if key and key not in seen:
            seen.add(key)
            groups.append(TextGroup(values))

    for value in indexed:
        add((value,))
    for length in range(2, max_items + 1):
        for start in range(len(indexed) - length + 1):
            add(tuple(indexed[start : start + length]))
    positioned = sorted(
        (value for value in indexed if _box(value[1]) is not None),
        key=lambda value: (
            _center(_box(value[1]))[1],  # type: ignore[arg-type]
            _center(_box(value[1]))[0],  # type: ignore[arg-type]
        ),
    )
    for left_index, left in enumerate(positioned):
        for right in positioned[left_index + 1 :]:
            if _spatially_related(left[1], right[1]):
                add(
                    tuple(
                        sorted(
                            (left, right),
                            key=lambda value: _center(_box(value[1]))[0],  # type: ignore[arg-type]
                        )
                    )
                )
    return groups


_DIGIT_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
_LETTER_TO_DIGIT = {
    "O": "0",
    "Q": "0",
    "D": "0",
    "I": "1",
    "L": "1",
    "Z": "2",
    "S": "5",
    "G": "6",
    "B": "8",
}
_CATEGORY_CORRECTIONS = {"V": "U", "Y": "U"}


def _coerce_container(raw: str) -> tuple[str, int] | None:
    if len(raw) not in (10, 11):
        return None
    output: list[str] = []
    corrections = 0
    for index, character in enumerate(raw):
        if index < 3:
            replacement = character if character.isalpha() else _DIGIT_TO_LETTER.get(character)
        elif index == 3:
            replacement = character if character in "UJZ" else _CATEGORY_CORRECTIONS.get(character)
        else:
            replacement = character if character.isdigit() else _LETTER_TO_DIGIT.get(character)
        if replacement is None:
            return None
        output.append(replacement)
        corrections += int(replacement != character)
    return "".join(output), corrections


def _container_windows(value: str) -> list[tuple[str, int, int]]:
    if len(value) == 11:
        return [(value, 0, len(value))]
    output: list[tuple[str, int, int]] = []
    for size in (11, 10):
        if len(value) >= size:
            output.extend(
                (value[start : start + size], start, start + size)
                for start in range(len(value) - size + 1)
            )
    return output


def extract_container_candidates(
    items: list[OCRItem], expected: str = "", max_candidates: int = 10
) -> list[dict]:
    expected = normalize(expected)
    candidates: list[dict] = []
    for group in generate_text_groups(items, max_items=3):
        normalized_parts = [normalize(item.text) for _index, item in group.items]
        compact = "".join(normalized_parts)
        if not 10 <= len(compact) <= 32:
            continue
        spans: list[tuple[tuple[int, OCRItem], int, int]] = []
        cursor = 0
        for indexed_item, part in zip(group.items, normalized_parts, strict=True):
            spans.append((indexed_item, cursor, cursor + len(part)))
            cursor += len(part)
        for raw, start, end in _container_windows(compact):
            coerced = _coerce_container(raw)
            if coerced is None:
                continue
            observed, corrections = coerced
            try:
                suggested = append_check_digit(observed[:10])
            except ValueError:
                continue
            complete = len(observed) == 11
            valid = complete and validate_container_number(observed)
            selected_items = tuple(
                indexed_item
                for indexed_item, left, right in spans
                if right > start and left < end
            )
            selected_group = TextGroup(selected_items)
            rule_score = 1.0 if valid else (0.30 if complete else 0.70)
            score = max(
                0.0,
                min(
                    1.0,
                    selected_group.confidence * 0.62
                    + rule_score * 0.33
                    + 0.05
                    - corrections * 0.055
                    - max(0, len(selected_group.items) - 2) * 0.01,
                ),
            )
            candidates.append(
                {
                    "observed": observed,
                    "suggested": suggested,
                    "complete": complete,
                    "valid": valid,
                    "verification": "verified" if valid else ("mismatch" if complete else "unverified"),
                    "matches_expected": bool(expected and observed == expected),
                    "suggested_matches_expected": bool(expected and suggested == expected),
                    "observed_check_digit": observed[-1] if complete else None,
                    "calculated_check_digit": suggested[-1],
                    "confidence": round(selected_group.confidence, 6),
                    "extraction_score": round(score, 6),
                    "corrections": corrections,
                    "source_indices": selected_group.source_indices,
                    "source_texts": selected_group.source_texts,
                    "text_is_horizontal": _group_is_horizontal(selected_group),
                }
            )
    best: dict[str, dict] = {}
    for candidate in candidates:
        current = best.get(candidate["observed"])
        ranking = (candidate["extraction_score"], candidate["confidence"], -candidate["corrections"])
        if current is None or ranking > (
            current["extraction_score"],
            current["confidence"],
            -current["corrections"],
        ):
            best[candidate["observed"]] = candidate
    return sorted(
        best.values(),
        key=lambda value: (value["extraction_score"], value["confidence"], -value["corrections"]),
        reverse=True,
    )[:max_candidates]


_DATE_PATTERN = re.compile(r"(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2}")
_TIME_PATTERN = re.compile(r"(?:[01]?\d|2[0-3]):[0-5]\d")
_CONTAINER_PREFIX_PATTERN = re.compile(r"^[A-Z]{3}[UJZ]\d{6,7}$")
_ISO_SIZE_PATTERN = re.compile(r"^\d{2}[A-Z]\d$")
_NOISE_SUBSTRINGS = {"MAXGROSS", "MAXPAYLOAD", "PAYLOAD", "TARE", "CUBE", "CUM", "CUFT", "CUFT3", "KGS", "LBS", "ACEP", "HAMBURG"}
_NOISE_TOKENS = {"HMM", "COSCO", "OSCO", "SINOKOR", "EVERGREEN", "SHIPPING", "FLORENS", "CAUTION", "CMA", "CGM", "MSC", "ONE", "KG", "KGS", "LB", "LBS"}


def _seal_noise(raw_text: str, compact: str, source_texts: list[str]) -> bool:
    upper = raw_text.upper()
    normalized_sources = {normalize(text) for text in source_texts}
    return bool(
        _DATE_PATTERN.search(upper)
        or _TIME_PATTERN.search(upper)
        or any(word in compact for word in _NOISE_SUBSTRINGS)
        or normalized_sources & _NOISE_TOKENS
        or _CONTAINER_PREFIX_PATTERN.fullmatch(compact)
        or _ISO_SIZE_PATTERN.fullmatch(compact)
        or (len(compact) >= 8 and compact.isdigit() and compact.startswith(("19", "20")))
    )


def extract_seal_candidates(items: list[OCRItem], max_candidates: int = 10) -> list[dict]:
    candidates: list[dict] = []
    for group in generate_text_groups(items, max_items=2):
        compact = normalize(group.text)
        if not 5 <= len(compact) <= 20 or not any(character.isdigit() for character in compact):
            continue
        if _seal_noise(group.spaced_text, compact, group.source_texts):
            continue
        letters = sum(character.isalpha() for character in compact)
        digits = sum(character.isdigit() for character in compact)
        score = (
            group.confidence * 0.68
            + (1.0 if 6 <= len(compact) <= 12 else 0.65) * 0.14
            + (1.0 if letters and digits else 0.45) * 0.18
            - max(0, len(group.items) - 1) * 0.085
            - (0.10 if not letters else 0.0)
            - (0.08 if len(compact) > 14 else 0.0)
        )
        candidates.append(
            {
                "observed": compact,
                "confidence": round(group.confidence, 6),
                "extraction_score": round(min(1.0, max(0.0, score)), 6),
                "source_indices": group.source_indices,
                "source_texts": group.source_texts,
                "text_is_horizontal": _group_is_horizontal(group),
            }
        )
    best: dict[str, dict] = {}
    for candidate in candidates:
        current = best.get(candidate["observed"])
        if current is None or (candidate["extraction_score"], candidate["confidence"]) > (
            current["extraction_score"], current["confidence"]
        ):
            best[candidate["observed"]] = candidate
    return sorted(
        best.values(),
        key=lambda value: (value["extraction_score"], value["confidence"]),
        reverse=True,
    )[:max_candidates]


_STRUCTURED_SEAL = re.compile(r"^[A-Z]{1,5}\d{4,10}[A-Z]?$")
_ORIENTATION_ORDER = {"original": 0, "cw90": 1, "ccw90": 2, "rotate180": 3}


def _seal_format_score(value: str) -> float:
    if _STRUCTURED_SEAL.fullmatch(value) and 6 <= len(value) <= 12:
        return 1.0
    if value.isdigit() and 6 <= len(value) <= 10:
        return min(0.90, 0.74 + (len(value) - 6) * 0.04)
    if 6 <= len(value) <= 12 and any(character.isalpha() for character in value):
        return 0.72
    return 0.40


def _is_extension(longer: str, shorter: str) -> bool:
    difference = len(longer) - len(shorter)
    return bool(
        longer.isdigit()
        and shorter.isdigit()
        and len(longer) <= 10
        and len(shorter) >= 5
        and 1 <= difference <= 4
        and (longer.startswith(shorter) or longer.endswith(shorter))
    )


def fuse_seal_candidates(
    candidates_by_orientation: dict[str, list[dict]], expected: str = "", max_candidates: int = 10
) -> list[dict]:
    expected = normalize(expected)
    all_candidates = [
        {**candidate, "orientation": orientation}
        for orientation, candidates in candidates_by_orientation.items()
        for candidate in candidates
    ]
    by_value: dict[str, list[dict]] = {}
    for candidate in all_candidates:
        by_value.setdefault(candidate["observed"], []).append(candidate)
    fused: list[dict] = []
    for value, same_value in by_value.items():
        representative = max(
            same_value,
            key=lambda item: (
                bool(item.get("text_is_horizontal")),
                item["extraction_score"],
                item["confidence"],
                -_ORIENTATION_ORDER.get(item["orientation"], 99),
            ),
        )
        orientations = sorted(
            {item["orientation"] for item in same_value},
            key=lambda item: _ORIENTATION_ORDER.get(item, 99),
        )
        score = (
            representative["extraction_score"] * 0.48
            + representative["confidence"] * 0.32
            + _seal_format_score(value) * 0.20
        )
        reasons: list[str] = []
        if len(orientations) > 1:
            score += min(0.12, 0.07 * (len(orientations) - 1))
            reasons.append("cross_orientation_consensus")
        if any(
            _is_extension(value, other["observed"])
            and representative["confidence"] >= other["confidence"] - 0.10
            for other in all_candidates
            if other["observed"] != value
        ):
            score += 0.14
            reasons.append("supported_complete_extension")
        if any(
            _is_extension(other["observed"], value)
            and other["confidence"] >= representative["confidence"] - 0.10
            for other in all_candidates
            if other["observed"] != value
        ):
            score -= 0.06
            reasons.append("shorter_than_supported_candidate")
        similarity = SequenceMatcher(None, expected, value).ratio() if expected else 0.0
        fused.append(
            {
                **representative,
                "expected": expected,
                "similarity": round(similarity, 6),
                "matches_expected": bool(expected and value == expected),
                "extraction_score": round(min(1.0, max(0.0, score)), 6),
                "supporting_orientations": orientations,
                "selection_reasons": reasons,
            }
        )
    return sorted(
        fused,
        key=lambda item: (
            bool(item["matches_expected"]),
            item["extraction_score"],
            item["confidence"],
            item["similarity"],
            len(item["observed"]),
        ),
        reverse=True,
    )[:max_candidates]
