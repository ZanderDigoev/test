"""Сквозные проверки: программа против виртуального автомобиля.

Симулятор отвечает на том же уровне, что настоящий адаптер, поэтому эти
тесты проходят весь путь -- от AT-команд до разобранных кодов и записи
настройки в блок.
"""

import pytest

from avensis.dtc import DtcStatus
from avensis.elm327 import Elm327
from avensis.obd import ObdSession
from avensis.settings_ops import read_setting, write_setting
from avensis.simulator import ElmSimulator
from avensis.toyota import Setting
from avensis.transport import LoopbackTransport, create_transport
from avensis.uds import UdsClient, UdsError

VIN = "SB1BJ56L20E095222"


@pytest.fixture(params=["avensis_t25", "avensis_t25_kline"])
def elm(request):
    """Соединение с виртуальной машиной по обоим типам шины."""
    connection = Elm327(LoopbackTransport(ElmSimulator(profile=request.param)))
    connection.open()
    yield connection
    connection.close()


@pytest.fixture
def can_elm():
    """Только CAN -- для расширенной диагностики, которой нет на K-line."""
    connection = Elm327(LoopbackTransport(ElmSimulator(profile="avensis_t25")))
    connection.open()
    yield connection
    connection.close()


# ------------------------------------------------------------------ соединение


def test_adapter_is_identified(elm):
    assert "ELM327" in elm.adapter_id


def test_protocol_is_detected(elm):
    assert elm.protocol in (3, 6)
    assert elm.protocol_name


def test_voltage_is_read(elm):
    assert elm.read_voltage() == pytest.approx(14.1)


def test_sim_url_builds_a_working_transport():
    assert isinstance(create_transport("sim://"), LoopbackTransport)


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError):
        create_transport("sim://?profile=nonexistent")


# ------------------------------------------------------------ стандартный OBD


def test_stored_codes_are_read_on_both_buses(elm):
    codes = ObdSession(elm).read_stored_dtcs()
    assert [c.code for c in codes] == ["P0301", "P1349", "P0420"]
    assert all(c.description for c in codes)


def test_pending_and_permanent_are_separate_categories(elm):
    obd = ObdSession(elm)
    assert [c.code for c in obd.read_pending_dtcs()] == ["P0171"]
    permanent = obd.read_permanent_dtcs()
    assert permanent[0].status == DtcStatus.PERMANENT


def test_vin_survives_multi_frame_reassembly(elm):
    assert ObdSession(elm).read_vin() == VIN


def test_live_parameters_are_decoded(elm):
    values = ObdSession(elm).read_pid(0x0C)
    assert values["7E8" if elm.is_can else "10"] == 750


def test_monitor_status_reports_the_lamp(elm):
    statuses = ObdSession(elm).read_monitor_status()
    engine = statuses["7E8" if elm.is_can else "10"]
    assert engine.mil_on is True
    assert engine.dtc_count == 3


def test_freeze_frame_carries_its_trigger_code(elm):
    frames = ObdSession(elm).read_freeze_frame()
    assert frames[0].trigger_dtc == "P0301"
    assert frames[0].values[0x0C] == 1125


def test_calibration_id_is_read(elm):
    ids = ObdSession(elm).read_calibration_ids()
    assert "89663-05270" in next(iter(ids.values()))


def test_clearing_removes_stored_codes(elm):
    obd = ObdSession(elm)
    assert obd.read_stored_dtcs()
    assert all(obd.clear_dtcs().values())
    assert obd.read_stored_dtcs() == []


def test_clearing_leaves_permanent_codes_alone(elm):
    """Постоянные коды стирает только сам блок -- это не наша неудача."""
    obd = ObdSession(elm)
    obd.clear_dtcs()
    assert [c.code for c in obd.read_permanent_dtcs()] == ["P0420"]


def test_discovery_finds_engine_and_transmission(can_elm):
    addresses = {ecu.address for ecu in ObdSession(can_elm).discover_ecus()}
    assert addresses == {"7E8", "7E9"}


# ---------------------------------------------------------- расширенный доступ


def test_body_ecu_answers_only_over_uds(can_elm):
    """Кузовной блок не виден стандартному OBD-II, но отвечает по UDS."""
    assert "748" not in {e.address for e in ObdSession(can_elm).discover_ecus()}
    with UdsClient(can_elm, "740", "748") as uds:
        assert uds.probe()
        assert uds.read_did("F190").raw.decode() == VIN


def test_absent_address_is_reported_as_absent(can_elm):
    with UdsClient(can_elm, "7AA", "7B2") as uds:
        assert uds.probe() is False


def test_missing_identifier_raises_out_of_range(can_elm):
    with UdsClient(can_elm, "740", "748") as uds:
        with pytest.raises(UdsError) as caught:
            uds.read_did("BEEF")
        assert caught.value.nrc == 0x31


def test_abs_codes_come_through_extended_service(can_elm):
    with UdsClient(can_elm, "7B0", "7B8") as uds:
        codes = uds.read_dtcs()
    assert [c.code for c in codes] == ["C1336"]


def test_did_scan_finds_configuration_cells(can_elm):
    with UdsClient(can_elm, "740", "748") as uds:
        uds.start_session(0x03)
        found = uds.scan_dids(0x0100, 0x0105)
    assert set(found) == {"0100", "0101", "0102"}


def test_addressing_is_restored_after_uds(can_elm):
    """Если физическая адресация останется, обычные запросы уйдут не туда."""
    with UdsClient(can_elm, "740", "748") as uds:
        uds.probe()
    assert ObdSession(can_elm).read_pid(0x0C)


# ------------------------------------------------------------------- настройки


@pytest.fixture
def door_lock_setting():
    return Setting(
        key="auto-door-lock", title="Автоблокировка дверей", ecu="Body ECU",
        request_id="740", response_id="748", did="0100",
        byte=0, mask=0x03,
        values={"выключено": 0, "при скорости выше 20 км/ч": 1, "при переводе селектора из P": 2},
    )


def test_setting_is_read_from_the_block(can_elm, door_lock_setting):
    with UdsClient(can_elm, "740", "748") as uds:
        state = read_setting(uds, door_lock_setting)
    assert state.value == 1
    assert state.value_name == "при скорости выше 20 км/ч"


def test_dry_run_touches_nothing(can_elm, door_lock_setting, tmp_path):
    with UdsClient(can_elm, "740", "748") as uds:
        result = write_setting(uds, door_lock_setting, "выключено", dry_run=True)
        assert not result.applied
        # Блок обязан остаться в прежнем состоянии.
        assert read_setting(uds, door_lock_setting).value == 1


def test_write_is_verified_by_reading_back(can_elm, door_lock_setting, tmp_path):
    with UdsClient(can_elm, "740", "748") as uds:
        uds.start_session(0x03)
        result = write_setting(
            uds, door_lock_setting, "при переводе селектора из P",
            dry_run=False, backup_directory=tmp_path,
        )
    assert result.applied and result.verified
    assert result.before.hex().upper() == "01050200"
    assert result.after.hex().upper() == "02050200"


def test_write_only_disturbs_its_own_bits(can_elm, door_lock_setting, tmp_path):
    neighbour = Setting(
        key="auto-door-unlock", title="Авторазблокировка", ecu="Body ECU",
        request_id="740", response_id="748", did="0100",
        byte=0, mask=0x0C, values={"выключено": 0, "включено": 1},
    )
    with UdsClient(can_elm, "740", "748") as uds:
        uds.start_session(0x03)
        before = read_setting(uds, neighbour).value
        write_setting(uds, door_lock_setting, "выключено", dry_run=False, backup_directory=tmp_path)
        assert read_setting(uds, neighbour).value == before


def test_backup_file_is_written_before_the_change(can_elm, door_lock_setting, tmp_path):
    with UdsClient(can_elm, "740", "748") as uds:
        uds.start_session(0x03)
        result = write_setting(
            uds, door_lock_setting, "выключено", dry_run=False, backup_directory=tmp_path
        )
    assert result.backup_path.exists()
    assert "01050200" in result.backup_path.read_text(encoding="utf-8")


def test_writing_the_current_value_is_a_no_op(can_elm, door_lock_setting, tmp_path):
    with UdsClient(can_elm, "740", "748") as uds:
        result = write_setting(
            uds, door_lock_setting, "при скорости выше 20 км/ч",
            dry_run=False, backup_directory=tmp_path,
        )
    assert not result.applied
    assert "уже стоит" in result.message


def test_write_refused_outside_extended_session(can_elm, door_lock_setting, tmp_path):
    """Блок принимает запись только в расширенной сессии -- отказ должен дойти."""
    with UdsClient(can_elm, "740", "748") as uds:
        uds.ecu_reset()  # сбрасывает сессию к обычной
        with pytest.raises(UdsError):
            uds.write_did("0100", bytes.fromhex("00050200"))
