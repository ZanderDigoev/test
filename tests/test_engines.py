"""Определение двигателя и подбор параметров под него."""

import pytest

from avensis.elm327 import Elm327
from avensis.engines import DIESEL, PETROL, ENGINE_PROFILES, detect, get_profile
from avensis.obd import ObdSession
from avensis.simulator import ElmSimulator
from avensis.transport import LoopbackTransport


def test_fuel_parameter_wins_over_monitor_bit():
    """Параметр «тип топлива» называет топливо прямо и потому точнее."""
    detection = detect(fuel_type_value="дизель", compression_ignition=False)
    assert detection.fuel == DIESEL
    assert "тип топлива" in detection.source


def test_monitor_bit_is_the_fallback():
    detection = detect(fuel_type_value=None, compression_ignition=True)
    assert detection.fuel == DIESEL
    assert "мониторов" in detection.source


def test_nothing_known_yields_no_verdict():
    detection = detect()
    assert detection.fuel is None
    assert detection.profile is None
    assert "не удалось" in detection.describe()


def test_candidates_match_the_fuel():
    assert all(p.fuel == DIESEL for p in detect(fuel_type_value="дизель").candidates)
    assert all(p.fuel == PETROL for p in detect(fuel_type_value="бензин").candidates)


def test_forced_code_overrides_everything():
    detection = detect(fuel_type_value="бензин", compression_ignition=False, forced_code="1CD-FTV")
    assert detection.profile.code == "1CD-FTV"
    assert detection.fuel == DIESEL
    assert "вручную" in detection.describe()


def test_forced_code_is_case_and_dash_insensitive():
    assert get_profile("1cdftv").code == "1CD-FTV"
    assert get_profile("2AD-FTV").code == "2AD-FTV"


def test_unknown_code_is_rejected_with_a_hint():
    with pytest.raises(ValueError, match="Известные"):
        detect(forced_code="2JZ-GTE")


def test_diesel_gets_rail_pressure_petrol_gets_lambda():
    diesel = detect(forced_code="1CD-FTV").live_pids
    petrol = detect(forced_code="1ZZ-FE").live_pids
    assert 0x23 in diesel and 0x23 not in petrol      # давление в рампе
    assert 0x44 in petrol and 0x44 not in diesel      # лямбда


def test_live_pid_list_has_no_duplicates():
    for code in ENGINE_PROFILES:
        pids = detect(forced_code=code).live_pids
        assert len(pids) == len(set(pids))


def test_every_watch_code_has_a_description():
    """Подсказка «типовая болячка» бесполезна без расшифровки кода."""
    from avensis.dtc import describe

    for profile in ENGINE_PROFILES.values():
        for code in profile.watch_codes:
            _, known = describe(code)
            assert known, f"{profile.code}: код {code} без описания"


def test_detection_over_the_bus():
    elm = Elm327(LoopbackTransport(ElmSimulator()))
    elm.open()
    try:
        detection = ObdSession(elm).detect_engine()
    finally:
        elm.close()
    assert detection.fuel == PETROL
    assert detection.live_pids
