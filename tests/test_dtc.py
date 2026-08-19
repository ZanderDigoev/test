"""Кодирование и расшифровка кодов неисправностей."""

import pytest

from avensis.dtc import (
    DtcStatus,
    decode_dtc,
    describe,
    encode_dtc,
    make_dtc,
    parse_dtc_bytes,
)


@pytest.mark.parametrize("code", ["P0301", "P1349", "C1336", "B1180", "U0100", "P0420", "U1000"])
def test_round_trip(code):
    high, low = encode_dtc(code)
    assert decode_dtc(high, low) == code


@pytest.mark.parametrize(
    "raw,expected",
    [((0x03, 0x01), "P0301"), ((0x43, 0x36), "C0336"), ((0x83, 0x01), "B0301"), ((0xC1, 0x00), "U0100"), ((0xD1, 0x00), "U1100")],
)
def test_letter_bits(raw, expected):
    assert decode_dtc(*raw) == expected


def test_empty_slot_is_not_a_code():
    assert decode_dtc(0x00, 0x00) is None


def test_parse_skips_empty_slots():
    codes = parse_dtc_bytes(bytes.fromhex("030100001349"))
    assert [c.code for c in codes] == ["P0301", "P1349"]


def test_odd_trailing_byte_is_ignored():
    codes = parse_dtc_bytes(bytes.fromhex("030113"))
    assert [c.code for c in codes] == ["P0301"]


def test_known_code_gets_real_description():
    description, known = describe("P0301")
    assert known
    assert "цилиндре 1" in description


def test_unknown_code_still_names_its_subsystem():
    description, known = describe("P0777")
    assert not known
    assert "рансмиссия" in description


def test_toyota_database_overrides_generic_text():
    """Для C1336 у Toyota своя трактовка, она и должна побеждать."""
    assert "инициализация" in describe("C1336")[0]


def test_status_is_carried_through():
    code = make_dtc("P0420", status=DtcStatus.PERMANENT, ecu="7E8")
    assert code.status == DtcStatus.PERMANENT
    assert code.ecu == "7E8"
    assert code.system == "Силовой агрегат"


def test_bad_code_is_rejected():
    with pytest.raises(ValueError):
        encode_dtc("X9999")
