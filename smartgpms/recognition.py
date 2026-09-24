from __future__ import annotations

import threading
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .iso6346 import append_check_digit, normalize, validate_container_number

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


@dataclass
class OCRItem:
    text: str
    score: float
    box: list[list[float]]


class RapidOCREngine:
    def __init__(self) -> None:
        self._engine: Any | None = None
        self._lock = threading.Lock()

    def _get_engine(self):
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    from rapidocr import RapidOCR

                    self._engine = RapidOCR()
        return self._engine

    def recognize(self, image_path: Path) -> list[OCRItem]:
        result = self._get_engine()(str(image_path))
        if result is None:
            return []
        raw_boxes = getattr(result, "boxes", None)
        raw_texts = getattr(result, "txts", None)
        raw_scores = getattr(result, "scores", None)
        boxes = list(raw_boxes) if raw_boxes is not None else []
        texts = list(raw_texts) if raw_texts is not None else []
        scores = list(raw_scores) if raw_scores is not None else []
        return [
            OCRItem(
                text=str(text),
                score=float(scores[index]) if index < len(scores) else 0.0,
                box=[[float(point[0]), float(point[1])] for point in boxes[index]],
            )
            for index, text in enumerate(texts)
            if index < len(boxes)
        ]


def _coerce_container(raw: str) -> tuple[str, int] | None:
    raw = normalize(raw)
    if len(raw) not in (10, 11):
        return None
    output: list[str] = []
    corrections = 0
    for index, character in enumerate(raw):
        if index < 3:
            if character.isalpha():
                output.append(character)
            elif character in _DIGIT_TO_LETTER:
                output.append(_DIGIT_TO_LETTER[character])
                corrections += 1
            else:
                return None
        elif index == 3:
            if character in "UJZ":
                output.append(character)
            elif character in _CATEGORY_CORRECTIONS:
                output.append(_CATEGORY_CORRECTIONS[character])
                corrections += 1
            else:
                return None
        elif character.isdigit():
            output.append(character)
        elif character in _LETTER_TO_DIGIT:
            output.append(_LETTER_TO_DIGIT[character])
            corrections += 1
        else:
            return None
    return "".join(output), corrections


def _windows(value: str) -> list[tuple[str, int, int]]:
    if len(value) in (10, 11):
        return [(value, 0, len(value))]
    values: list[tuple[str, int, int]] = []
    for size in (11, 10):
        values.extend(
            (value[start : start + size], start, start + size)
            for start in range(max(0, len(value) - size + 1))
        )
    return values


def _candidate_groups(
    items: list[OCRItem], minimum_length: int = 10
) -> list[tuple[str, list[int]]]:
    groups: list[tuple[str, list[int]]] = []
    for start in range(len(items)):
        for size in (1, 2, 3):
            indices = list(range(start, min(start + size, len(items))))
            if len(indices) != size:
                continue
            compact = "".join(normalize(items[index].text) for index in indices)
            if minimum_length <= len(compact) <= 32:
                groups.append((compact, indices))
    return groups


def extract_container_candidates(
    items: list[OCRItem], expected: str = ""
) -> list[dict[str, Any]]:
    expected = normalize(expected)
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for compact, indices in _candidate_groups(items):
        spans: list[tuple[int, int, int]] = []
        cursor = 0
        for index in indices:
            length = len(normalize(items[index].text))
            spans.append((index, cursor, cursor + length))
            cursor += length
        for raw, start, end in _windows(compact):
            coerced = _coerce_container(raw)
            if not coerced:
                continue
            observed, corrections = coerced
            source_indices = [
                index for index, left, right in spans if right > start and left < end
            ]
            suggested = append_check_digit(observed[:10])
            key = (observed, tuple(source_indices))
            if key in seen:
                continue
            seen.add(key)
            complete = len(observed) == 11
            valid = complete and validate_container_number(observed)
            confidence = sum(items[index].score for index in source_indices) / len(
                source_indices
            )
            output.append(
                {
                    "observed": observed,
                    "suggested": suggested,
                    "complete": complete,
                    "valid": valid,
                    "matches_expected": bool(expected and observed == expected),
                    "suggested_matches_expected": bool(
                        expected and suggested == expected
                    ),
                    "observed_check_digit": observed[-1] if complete else None,
                    "calculated_check_digit": suggested[-1],
                    "confidence": round(confidence, 4),
                    "corrections": corrections,
                    "source_indices": source_indices,
                    "source_texts": [items[index].text for index in source_indices],
                }
            )
    output.sort(
        key=lambda row: (
            row["matches_expected"],
            row["valid"],
            row["suggested_matches_expected"],
            row["complete"],
            row["confidence"],
            -row["corrections"],
        ),
        reverse=True,
    )
    return output[:10]


def find_seal_candidate(items: list[OCRItem], expected: str) -> dict[str, Any] | None:
    expected = normalize(expected)
    if not expected:
        return None
    best: dict[str, Any] | None = None
    for compact, indices in _candidate_groups(
        items, minimum_length=max(3, len(expected) - 2)
    ):
        candidates = {compact}
        for size in range(
            max(3, len(expected) - 2), min(len(compact), len(expected) + 2) + 1
        ):
            candidates.update(
                compact[start : start + size]
                for start in range(len(compact) - size + 1)
            )
        for observed in candidates:
            ratio = SequenceMatcher(None, expected, observed).ratio()
            if expected in observed or observed in expected:
                ratio = max(
                    ratio,
                    min(len(observed), len(expected))
                    / max(len(observed), len(expected)),
                )
            if best is None or ratio > best["similarity"]:
                best = {
                    "observed": observed,
                    "expected": expected,
                    "similarity": round(ratio, 4),
                    "confidence": round(
                        sum(items[index].score for index in indices) / len(indices), 4
                    ),
                    "matches_expected": observed == expected,
                    "source_indices": indices,
                    "source_texts": [items[index].text for index in indices],
                }
    return best if best and best["similarity"] >= 0.45 else None


def _bounds(
    items: list[OCRItem],
    indices: list[int],
    image_size: tuple[int, int],
    margin: float = 0.28,
) -> tuple[int, int, int, int]:
    points = [point for index in indices for point in items[index].box]
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
    dx = max(18.0, (right - left) * margin)
    dy = max(18.0, (bottom - top) * margin)
    width, height = image_size
    return (
        max(0, int(left - dx)),
        max(0, int(top - dy)),
        min(width, int(right + dx)),
        min(height, int(bottom + dy)),
    )


def save_crop(
    image: Image.Image,
    items: list[OCRItem],
    indices: list[int],
    target: Path,
    target_type: str = "",
    complete: bool = False,
    source_rotation: int = 0,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target_type == "container" and not complete:
        points = [point for index in indices for point in items[index].box]
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        left, top, right, bottom = min(xs), min(ys), max(xs), max(ys)
        item_sizes = []
        for index in indices:
            item_xs = [float(point[0]) for point in items[index].box]
            item_ys = [float(point[1]) for point in items[index].box]
            item_sizes.append(
                min(max(item_xs) - min(item_xs), max(item_ys) - min(item_ys))
            )
        text_height = max(1.0, sum(item_sizes) / len(item_sizes))
        width, height = image.size
        bounds = (
            max(0, int(left - text_height * 0.8)),
            max(0, int(top - text_height * 1.0)),
            min(width, int(right + text_height * 3.0)),
            min(height, int(bottom + text_height * 1.0)),
        )
    else:
        bounds = _bounds(items, indices, image.size)
    crop = image.crop(bounds)
    vertical_boxes = horizontal_boxes = 0
    for index in indices:
        xs = [float(point[0]) for point in items[index].box]
        ys = [float(point[1]) for point in items[index].box]
        if max(ys) - min(ys) > max(xs) - min(xs):
            vertical_boxes += 1
        else:
            horizontal_boxes += 1
    if vertical_boxes > horizontal_boxes:
        transpose = (
            Image.Transpose.ROTATE_270
            if source_rotation == 90
            else Image.Transpose.ROTATE_90
        )
        crop = crop.transpose(transpose)
    crop.save(target, format="JPEG", quality=94)


def save_annotated(image: Image.Image, items: list[OCRItem], target: Path) -> None:
    preview = image.copy().convert("RGB")
    draw = ImageDraw.Draw(preview)
    for index, item in enumerate(items):
        coords = [(point[0], point[1]) for point in item.box]
        draw.line(
            coords + [coords[0]], fill="#21c7a8", width=max(2, preview.width // 500)
        )
        x = min(point[0] for point in item.box)
        y = min(point[1] for point in item.box)
        draw.text(
            (x, max(0, y - 16)),
            str(index + 1),
            fill="#ffb347",
            stroke_width=2,
            stroke_fill="#10252b",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    preview.thumbnail((1800, 1800))
    preview.save(target, format="JPEG", quality=88)


def classify_result(candidate: dict[str, Any] | None, expected: str) -> tuple[str, str]:
    expected = normalize(expected)
    if not candidate:
        return "review", "未找到可靠的箱号候选"
    if expected and candidate["matches_expected"] and candidate["valid"]:
        return "match", "照片识别值与输入箱号完全一致，且校验位有效"
    if (
        expected
        and candidate["suggested_matches_expected"]
        and not candidate["complete"]
    ):
        return "review", "前10位疑似一致，但照片中的校验位未可靠识别"
    if expected and candidate["observed"] != expected:
        return "mismatch", "照片识别值与输入箱号不一致"
    if candidate["valid"]:
        return "review", "识别到有效箱号，请人工确认"
    return "review", "已识别候选，但校验位未通过"
