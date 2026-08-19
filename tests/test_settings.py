"""Битовые операции над настраиваемыми функциями."""

import pytest

from avensis.toyota import Setting, load_settings


def make_setting(byte: int = 0, mask: int = 0x0C) -> Setting:
    return Setting(
        key="test", title="Проверочная настройка", ecu="Body ECU",
        request_id="740", response_id="748", did="0100",
        byte=byte, mask=mask, values={"выключено": 0, "включено": 1, "третье": 2},
    )


def test_shift_is_derived_from_mask():
    assert make_setting(mask=0x01).shift == 0
    assert make_setting(mask=0x0C).shift == 2
    assert make_setting(mask=0xF0).shift == 4


def test_extract_reads_only_its_own_field():
    setting = make_setting(byte=1, mask=0x0C)
    assert setting.extract(bytes.fromhex("FF08FF")) == 2


def test_apply_preserves_neighbouring_bits():
    """Соседние настройки живут в том же байте и не должны пострадать."""
    setting = make_setting(byte=0, mask=0x0C)
    result = setting.apply(bytes.fromhex("F3AABB"), 1)
    assert result.hex().upper() == "F7AABB"


def test_apply_leaves_other_bytes_alone():
    setting = make_setting(byte=2, mask=0x03)
    assert make_setting(byte=2).apply(bytes.fromhex("11223344"), 0).hex().upper() == "11223344"
    assert setting.apply(bytes.fromhex("11223344"), 2).hex().upper() == "11223244"


def test_value_too_wide_for_mask_is_rejected():
    with pytest.raises(ValueError, match="не помещается"):
        make_setting(mask=0x01).apply(bytes.fromhex("00"), 3)


def test_short_data_is_rejected_rather_than_padded():
    with pytest.raises(ValueError, match="короче"):
        make_setting(byte=5).apply(bytes.fromhex("0011"), 1)


def test_extract_returns_none_when_byte_missing():
    assert make_setting(byte=5).extract(bytes.fromhex("0011")) is None


def test_name_of_maps_back_to_label():
    assert make_setting().name_of(1) == "включено"
    assert "неизвестное" in make_setting().name_of(7)


def test_shipped_catalogue_has_no_confirmed_identifiers():
    """Каталог не должен утверждать то, что не проверено на живой машине."""
    for key, setting in load_settings().items():
        assert setting.did == "", f"{key} содержит непроверенный идентификатор"
        assert setting.verified is False
