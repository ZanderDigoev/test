"""Пересчёт параметров режима 01 в физические величины."""

import pytest

from avensis.pids import (
    decode_monitor_status,
    decode_supported,
    decode_value,
    format_value,
)


@pytest.mark.parametrize(
    "pid,raw,expected",
    [
        (0x0C, "1AF8", 1726),      # обороты = ((A*256)+B)/4
        (0x0D, "50", 80),          # скорость
        (0x05, "5A", 50),          # температура ОЖ = A-40
        (0x0F, "28", 0),
        (0x04, "FF", 100.0),       # нагрузка = A*100/255
        (0x06, "80", 0.0),         # коррекция = (A-128)*100/128
        (0x06, "70", -12.5),
        (0x10, "0145", 3.25),      # расход воздуха
        (0x42, "36B0", 14.0),      # напряжение
        (0x0E, "8C", 6.0),         # угол опережения = A/2-64
        (0x5C, "55", 45),          # температура масла
    ],
)
def test_decoders(pid, raw, expected):
    assert decode_value(pid, bytes.fromhex(raw)) == expected


def test_truncated_answer_yields_nothing_rather_than_garbage():
    assert decode_value(0x0C, b"\x1a") is None


def test_unknown_pid_yields_nothing():
    assert decode_value(0xFE, b"\x00\x00") is None


def test_format_adds_unit():
    assert format_value(0x0C, 1726) == "1726 об/мин"
    assert format_value(0x0C, None) == "—"


def test_supported_mask_starts_at_next_pid():
    supported = decode_supported(0x00, bytes.fromhex("80000000"))
    assert supported == [0x01]
    assert decode_supported(0x20, bytes.fromhex("80000000")) == [0x21]


def test_monitor_status_reads_lamp_and_count():
    status = decode_monitor_status(bytes.fromhex("83070505"))
    assert status.mil_on is True
    assert status.dtc_count == 3
    assert status.compression_ignition is False
    assert "Катализатор" in status.not_ready


def test_diesel_flag_switches_monitor_names():
    status = decode_monitor_status(bytes.fromhex("000F0000"))
    assert status.compression_ignition is True
    assert status.engine_kind == "дизельный"
    assert "Сажевый фильтр (DPF)" in status.monitors


def test_lamp_off_when_high_bit_clear():
    status = decode_monitor_status(bytes.fromhex("00070000"))
    assert status.mil_on is False
    assert status.dtc_count == 0


def test_short_answer_is_rejected():
    assert decode_monitor_status(b"\x83\x07") is None
