"""Разбор VIN, включая номер конкретной машины из задачи."""

from avensis.vin import compute_check_digit, describe_vin, parse_vin

AVENSIS_VIN = "SB1BJ56L20E095222"


def test_target_vin_is_well_formed():
    info = parse_vin(AVENSIS_VIN)
    assert info.valid_format
    assert not info.problems


def test_target_vin_check_digit_matches():
    """Девятый символ -- контрольный разряд; он сходится с расчётным."""
    assert compute_check_digit(AVENSIS_VIN) == "2"
    assert parse_vin(AVENSIS_VIN).check_digit_ok


def test_target_vin_manufacturer_and_plant():
    info = parse_vin(AVENSIS_VIN)
    assert info.wmi == "SB1"
    assert "Toyota" in info.manufacturer
    assert info.plant_code == "E"
    assert "Бёрнастон" in info.plant
    assert info.serial == "095222"
    assert info.is_toyota


def test_wrong_length_is_reported_not_crashed():
    info = parse_vin("SB1BJ56L2")
    assert not info.valid_format
    assert any("Длина" in problem for problem in info.problems)


def test_forbidden_letters_are_flagged():
    info = parse_vin("SB1BJ56L2OE095222")
    assert any("Недопустимые" in problem for problem in info.problems)


def test_lowercase_and_spacing_are_normalised():
    assert parse_vin(" sb1bj56l20e095222 ").vin == AVENSIS_VIN


def test_describe_returns_labelled_rows():
    rows = dict(describe_vin(AVENSIS_VIN))
    assert rows["VIN"] == AVENSIS_VIN
    assert "Toyota" in rows["Изготовитель (WMI)"]


def test_check_digit_is_none_for_invalid_alphabet():
    assert compute_check_digit("SB1BJ56L2QE095222") is None
