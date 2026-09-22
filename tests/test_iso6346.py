from smartgpms.iso6346 import (
    append_check_digit,
    calculate_check_digit,
    validate_container_number,
)


def test_known_container_number():
    assert calculate_check_digit("MSCU663987") == 0
    assert append_check_digit("MSCU663987") == "MSCU6639870"
    assert validate_container_number("MSCU6639870")


def test_invalid_check_digit():
    assert not validate_container_number("MSCU6639871")
