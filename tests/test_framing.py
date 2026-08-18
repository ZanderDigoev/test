"""Разбор строк адаптера: сборка ISO-TP и снятие заголовков K-line."""

import pytest

from avensis.framing import (
    AdapterError,
    NoDataError,
    clean_lines,
    parse_can,
    parse_kline,
    parse_response,
)


def test_single_frame_can():
    messages = parse_response("7E8064100BE3EA813", protocol=6)
    assert len(messages) == 1
    assert messages[0].ecu == "7E8"
    assert messages[0].data.hex().upper() == "4100BE3EA813"


def test_spaces_are_tolerated_even_though_we_ask_for_none():
    """ATS0 применяют не все клоны, поэтому пробелы не должны ломать разбор."""
    messages = parse_response("7E8 06 41 00 BE 3E A8 13", protocol=6)
    assert messages[0].data.hex().upper() == "4100BE3EA813"


def test_multi_frame_can_reassembles_vin():
    lines = "\r".join([
        "7E81014490201534231",
        "7E821424A35364C3230",
        "7E82245303935323232",
    ])
    messages = parse_response(lines, protocol=6)
    assert len(messages) == 1
    # Длина из первого кадра (0x014 = 20) обрезает набивку последнего кадра.
    assert len(messages[0].data) == 20
    assert messages[0].data[3:].decode() == "SB1BJ56L20E095222"


def test_padding_beyond_declared_length_is_discarded():
    messages = parse_can(["7E8100A4902015342310000", "7E8214A4B0000000000"], extended=False)
    assert len(messages[0].data) == 10


def test_two_ecus_answer_one_broadcast():
    messages = parse_response("7E8034100FF\r7E9034100AA", protocol=6)
    assert [m.ecu for m in messages] == ["7E8", "7E9"]


def test_extended_can_uses_four_byte_header():
    messages = parse_can(["18DAF110034100FF"], extended=True)
    assert messages[0].ecu == "18DAF110"
    assert messages[0].data.hex().upper() == "4100FF"


def test_flow_control_frames_are_ignored():
    messages = parse_can(["7E8300000", "7E8034100FF"], extended=False)
    assert messages[0].data.hex().upper() == "4100FF"


def test_kline_strips_header_and_checksum():
    raw = bytes.fromhex("486B104100BE3EA813")
    line = (raw + bytes([sum(raw) & 0xFF])).hex().upper()
    messages = parse_response(line, protocol=3)
    assert messages[0].ecu == "10"
    assert messages[0].data.hex().upper() == "4100BE3EA813"


def test_kline_keeps_payload_when_checksum_is_wrong():
    """Испорченную контрольную сумму нельзя молча принимать за данные."""
    messages = parse_kline(["486B104100BE3EA81300"])
    assert messages[0].data.hex().upper() == "4100BE3EA81300"


def test_kline_multiple_messages_stay_separate():
    messages = parse_kline(["83F110410C1AF8C1", "83F110410D32AA"])
    assert len(messages) == 2


def test_no_data_raises_dedicated_error():
    with pytest.raises(NoDataError):
        parse_response("NO DATA", protocol=6)


@pytest.mark.parametrize("text", ["CAN ERROR", "BUS BUSY", "UNABLE TO CONNECT", "?"])
def test_adapter_errors_are_reported(text):
    with pytest.raises(AdapterError):
        parse_response(text, protocol=6)


def test_searching_notice_is_not_data():
    messages = parse_response("SEARCHING...\r7E8034100FF", protocol=6)
    assert len(messages) == 1


def test_clean_lines_splits_on_prompt():
    assert clean_lines("41 00\r\r>") == ["41 00"]
