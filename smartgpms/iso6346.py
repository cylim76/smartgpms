from __future__ import annotations

import re

LETTER_VALUES = {
    "A": 10,
    "B": 12,
    "C": 13,
    "D": 14,
    "E": 15,
    "F": 16,
    "G": 17,
    "H": 18,
    "I": 19,
    "J": 20,
    "K": 21,
    "L": 23,
    "M": 24,
    "N": 25,
    "O": 26,
    "P": 27,
    "Q": 28,
    "R": 29,
    "S": 30,
    "T": 31,
    "U": 32,
    "V": 34,
    "W": 35,
    "X": 36,
    "Y": 37,
    "Z": 38,
}

PREFIX_PATTERN = re.compile(r"^[A-Z]{3}[UJZ][0-9]{6}$")
FULL_PATTERN = re.compile(r"^[A-Z]{3}[UJZ][0-9]{7}$")


def normalize(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def calculate_check_digit(prefix: str) -> int:
    value = normalize(prefix)
    if not PREFIX_PATTERN.fullmatch(value):
        raise ValueError("箱号前10位格式应为3位字母、U/J/Z、6位数字")
    total = 0
    for position, character in enumerate(value):
        number = int(character) if character.isdigit() else LETTER_VALUES[character]
        total += number * (2**position)
    remainder = total % 11
    return 0 if remainder == 10 else remainder


def append_check_digit(prefix: str) -> str:
    value = normalize(prefix)
    return f"{value}{calculate_check_digit(value)}"


def validate_container_number(value: str) -> bool:
    normalized = normalize(value)
    return bool(
        FULL_PATTERN.fullmatch(normalized)
        and calculate_check_digit(normalized[:10]) == int(normalized[10])
    )
