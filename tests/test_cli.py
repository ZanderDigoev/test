"""Проверки командной строки поверх симулятора."""

import json

from avensis.cli import main

VIN = "SB1BJ56L20E095222"


def run(capsys, *args) -> tuple[int, str]:
    code = main(list(args))
    return code, capsys.readouterr().out


def test_scan_reports_codes_and_exits_nonzero(capsys):
    code, out = run(capsys, "scan")
    assert code == 1  # найдены ошибки -- признак для скриптов
    assert "P0301" in out and "Пропуски воспламенения" in out
    assert VIN in out


def test_scan_json_is_machine_readable(capsys):
    _, out = run(capsys, "scan", "--json")
    data = json.loads(out)
    assert data["vin"]["from_ecu"] == VIN
    assert {c["code"] for c in data["dtcs"]} >= {"P0301", "P1349", "P0420"}
    assert data["monitors"]["7E8"]["mil_on"] is True


def test_scan_markdown_has_tables(capsys):
    _, out = run(capsys, "scan", "--markdown")
    assert out.startswith("# Отчёт диагностики")
    assert "| Код | Категория |" in out


def test_scan_writes_to_file(capsys, tmp_path):
    target = tmp_path / "report.md"
    run(capsys, "scan", "--markdown", "-o", str(target))
    assert "P0301" in target.read_text(encoding="utf-8")


def test_vin_mismatch_is_announced(capsys):
    _, out = run(capsys, "--vin", "SB1BJ56L20E000000", "scan")
    assert "ВНИМАНИЕ" in out


def test_matching_vin_is_confirmed(capsys):
    _, out = run(capsys, "--vin", VIN, "scan")
    assert "совпадает с ожидаемым" in out


def test_dtc_command_lists_codes(capsys):
    code, out = run(capsys, "dtc", "--pending", "--permanent")
    assert code == 1
    assert "P0171" in out and "P0420" in out


def test_clear_requires_confirmation(capsys):
    code, out = run(capsys, "clear")
    assert code == 2
    assert "Отменено" in out


def test_clear_with_yes_removes_codes(capsys):
    code, out = run(capsys, "clear", "--yes")
    assert code == 0
    assert "сброшено" in out


def test_live_honours_count(capsys):
    _, out = run(capsys, "live", "0C", "0D", "--count", "2", "--interval", "0")
    assert out.count("Обороты двигателя") == 2


def test_live_writes_csv(capsys, tmp_path):
    target = tmp_path / "log.csv"
    run(capsys, "live", "0C", "--count", "2", "--interval", "0", "--csv", str(target))
    rows = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3  # заголовок и два замера
    assert "Обороты двигателя" in rows[0]


def test_vin_decode_needs_no_car(capsys):
    _, out = run(capsys, "vin", "--decode", VIN)
    assert "Бёрнастон" in out
    assert "совпадает с расчётным" in out


def test_monitors_lists_readiness(capsys):
    _, out = run(capsys, "monitors")
    assert "Check Engine: ГОРИТ" in out
    assert "Катализатор" in out


def test_freeze_shows_trigger(capsys):
    _, out = run(capsys, "freeze")
    assert "P0301" in out


def test_ecus_finds_body_module_over_uds(capsys):
    _, out = run(capsys, "ecus")
    assert "Кузовная электроника" in out
    assert "отвечает" in out


def test_raw_passes_command_through(capsys):
    _, out = run(capsys, "raw", "010C")
    assert "410C0BB8" in out.replace(" ", "")


def test_settings_list_warns_about_unconfirmed(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("AVENSIS_HOME", str(tmp_path))
    _, out = run(capsys, "settings", "list")
    assert "идентификатор не подтверждён" in out
    assert "auto-door-lock" in out


def test_settings_write_refuses_without_confirmed_identifier(capsys, tmp_path, monkeypatch):
    """Без подтверждённой ячейки запись невозможна -- и объясняет, что делать."""
    monkeypatch.setattr("avensis.settings_ops.CONFIRMED_FILE", tmp_path / "none.json")
    code, out = run(capsys, "settings", "write", "auto-door-lock", "выключено")
    assert code == 1
    assert "не подтверждён идентификатор" in out
    assert "settings discover" in out


def test_settings_discover_lists_cells(capsys):
    code, out = run(capsys, "settings", "discover", "--ecu", "740", "--range", "0100-0102")
    assert code == 0
    assert "0100" in out and "01050200" in out


def test_unknown_setting_is_rejected(capsys):
    code, out = run(capsys, "settings", "write", "no-such-key", "выключено")
    assert code == 1


def test_bad_port_gives_actionable_advice(capsys):
    code, out = run(capsys, "--port", "/dev/definitely-not-here", "dtc")
    assert code == 1
    assert "зажигание" in out
