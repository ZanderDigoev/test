"""Командный интерфейс диагностики Toyota Avensis."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence

from avensis import __version__
from avensis import pids as pid_module
from avensis import settings_ops
from avensis.dtc import DtcStatus
from avensis.elm327 import Elm327, Elm327Error
from avensis.framing import AdapterError
from avensis.obd import ObdSession
from avensis.report import VehicleReport, render_markdown, render_text
from avensis.toyota import CAN_ECU_CANDIDATES, load_settings
from avensis.transport import TransportError, create_transport
from avensis.uds import IDENTIFICATION_DIDS, UdsClient, UdsError
from avensis.vin import describe_vin

DEFAULT_PORT = "sim://"

DESTRUCTIVE_NOTICE = """
Эта операция изменяет состояние автомобиля. Перед запуском:
  • двигатель заглушен, зажигание включено (положение ON);
  • аккумулятор заряжен, напряжение не ниже 12 В;
  • адаптер надёжно вставлен в разъём и не будет выдернут в процессе.
Прерывание записи на середине способно повредить конфигурацию блока.
"""


# --------------------------------------------------------------- вспомогательное


def _setup_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _connect(args) -> Elm327:
    """Открыть адаптер и привести его в рабочее состояние."""
    def trace(direction: str, text: str) -> None:
        arrow = "->" if direction == "tx" else "<-"
        print(f"  {arrow} {text.strip()!r}", file=sys.stderr)

    transport = create_transport(args.port)
    elm = Elm327(
        transport,
        timeout=args.timeout,
        on_traffic=trace if getattr(args, "trace", False) else None,
    )
    elm.open(protocol=args.protocol)
    return elm


def _confirm(prompt: str, assume_yes: bool) -> bool:
    """Спросить подтверждение, если оно не выдано заранее флагом."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("Требуется подтверждение, но ввод недоступен. Повтори с флагом --yes.")
        return False
    answer = input(f"{prompt} [напиши «да» для продолжения]: ").strip().lower()
    return answer in ("да", "yes", "y")


def _emit(text: str, output: Optional[str]) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"Сохранено: {output}")
    else:
        print(text)


# ------------------------------------------------------------------- сбор данных


def _collect_report(
    elm: Elm327,
    args,
    include_live: bool = True,
    include_extended: bool = False,
) -> VehicleReport:
    """Провести полный опрос автомобиля и собрать отчёт."""
    obd = ObdSession(elm)
    report = VehicleReport(
        adapter=elm.adapter_id,
        connection=elm.transport.name,
        protocol=elm.protocol_name,
        vin_expected=args.vin,
    )

    try:
        report.voltage = elm.read_voltage()
    except (Elm327Error, AdapterError):
        report.warnings.append("Адаптер не сообщил напряжение бортовой сети")

    if elm.protocol == 0:
        report.warnings.append(
            "Протокол шины не определён. Включи зажигание в положение ON "
            "и убедись, что адаптер до конца вставлен в разъём."
        )

    report.vin_from_ecu = obd.read_vin()
    report.ecus = obd.discover_ecus()
    if not report.ecus:
        report.warnings.append(
            "Ни один блок не ответил на стандартный запрос OBD-II. "
            "Чаще всего причина -- выключенное зажигание."
        )

    report.monitors = obd.read_monitor_status()
    report.dtcs = obd.read_all_dtcs()
    report.freeze_frames = obd.read_freeze_frame()
    report.calibration_ids = obd.read_calibration_ids()

    if include_live:
        report.live = obd.read_live_snapshot(pid_module.LIVE_DEFAULT)

    if include_extended:
        report.extended_ecus = _probe_extended(elm, report)

    permanent = report.by_status(DtcStatus.PERMANENT)
    if permanent:
        report.warnings.append(
            "Есть постоянные коды -- они не стираются командой сброса и погаснут "
            "только после того, как блок сам убедится в исправности за несколько поездок."
        )
    if report.monitors and any(status.not_ready for status in report.monitors.values()):
        report.warnings.append(
            "Часть проверок самодиагностики не завершена. Если ошибки сбрасывали "
            "недавно, машина не пройдёт инструментальный контроль до окончания ездового цикла."
        )
    return report


def _probe_extended(elm: Elm327, report: VehicleReport) -> List[dict]:
    """Опросить блоки, недоступные по стандартному OBD-II (ABS, SRS, кузов)."""
    if not elm.is_can:
        report.warnings.append(
            "Расширенный опрос реализован для шины CAN; на этой машине активен "
            f"протокол «{elm.protocol_name}», блоки вне силового агрегата пропущены."
        )
        return []

    found = []
    for candidate in CAN_ECU_CANDIDATES:
        entry = {
            "name": candidate.name,
            "request_id": candidate.request,
            "response_id": candidate.response,
            "identification": [],
            "dtcs": [],
        }
        try:
            with UdsClient(elm, candidate.request, candidate.response) as uds:
                if not uds.probe():
                    continue
                for value in uds.read_identification():
                    entry["identification"].append(str(value))
                try:
                    entry["dtcs"] = [f"{c.code}  {c.description}" for c in uds.read_dtcs()]
                except UdsError:
                    pass
        except (Elm327Error, AdapterError, UdsError):
            continue
        if entry["identification"] or entry["dtcs"]:
            found.append(entry)
    return found


# ----------------------------------------------------------------------- команды


def cmd_scan(args) -> int:
    elm = _connect(args)
    try:
        report = _collect_report(elm, args, include_live=not args.no_live, include_extended=args.extended)
    finally:
        elm.close()

    if args.json:
        _emit(report.to_json(), args.output)
    elif args.markdown:
        _emit(render_markdown(report), args.output)
    else:
        _emit(render_text(report), args.output)
    return 1 if report.mil_on or report.dtcs else 0


def cmd_dtc(args) -> int:
    elm = _connect(args)
    try:
        obd = ObdSession(elm)
        codes = []
        if args.stored or not (args.pending or args.permanent):
            codes += obd.read_stored_dtcs()
        if args.pending:
            codes += obd.read_pending_dtcs()
        if args.permanent:
            codes += obd.read_permanent_dtcs()
    finally:
        elm.close()

    if not codes:
        print("Ошибок не найдено.")
        return 0

    print(f"Найдено кодов: {len(codes)}\n")
    for code in codes:
        print(f"  {code.code}  [{code.status}, блок {code.ecu}]")
        print(f"        {code.description}")
    return 1


def cmd_clear(args) -> int:
    print(DESTRUCTIVE_NOTICE)
    print("Сброс сотрёт коды, стоп-кадры и обнулит мониторы готовности.")
    print("Сама неисправность при этом никуда не денется -- код вернётся.\n")
    if not _confirm("Стереть коды неисправностей?", args.yes):
        print("Отменено.")
        return 2

    elm = _connect(args)
    try:
        obd = ObdSession(elm)
        before = obd.read_all_dtcs()
        print(f"До сброса в памяти было кодов: {len(before)}")
        result = obd.clear_dtcs()
        for ecu, ok in result.items():
            print(f"  Блок {ecu}: {'сброшено' if ok else 'ОТКАЗ'}")
        after = obd.read_all_dtcs()
        print(f"После сброса осталось кодов: {len(after)}")
        for code in after:
            print(f"  {code.code} [{code.status}] {code.description}")
        if after:
            print("\nОставшиеся коды -- либо постоянные, либо неисправность активна прямо сейчас.")
    finally:
        elm.close()
    return 0


def cmd_live(args) -> int:
    requested = [int(p, 16) for p in args.pids] if args.pids else pid_module.LIVE_DEFAULT
    elm = _connect(args)
    writer = None
    handle = None
    try:
        obd = ObdSession(elm)
        if args.csv:
            handle = open(args.csv, "w", newline="", encoding="utf-8")
            writer = csv.writer(handle)
            header = ["время"] + [
                (entry.name if (entry := pid_module.get_pid(p)) else f"PID {p:02X}") for p in requested
            ]
            writer.writerow(header)

        started = time.monotonic()
        iteration = 0
        while True:
            snapshot = obd.read_live_snapshot(requested)
            elapsed = time.monotonic() - started

            print(f"\n─── {elapsed:6.1f} с ───")
            row = [f"{elapsed:.2f}"]
            for pid in requested:
                entry = pid_module.get_pid(pid)
                name = entry.name if entry else f"PID {pid:02X}"
                values = snapshot.get(pid, {})
                shown = ", ".join(
                    f"{pid_module.format_value(pid, value)}" for value in values.values()
                ) or "—"
                print(f"  {name:<44} {shown:>24}")
                row.append(shown)
            if writer:
                writer.writerow(row)
                handle.flush()

            iteration += 1
            if args.count and iteration >= args.count:
                break
            if args.duration and elapsed >= args.duration:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        if handle:
            handle.close()
            print(f"Записано в {args.csv}")
        elm.close()
    return 0


def cmd_freeze(args) -> int:
    elm = _connect(args)
    try:
        frames = ObdSession(elm).read_freeze_frame(args.frame)
    finally:
        elm.close()

    if not frames:
        print("Стоп-кадр отсутствует. Он записывается только при фиксации ошибки.")
        return 0
    for frame in frames:
        print(f"\nБлок {frame.ecu}, ошибка {frame.trigger_dtc or 'не указана'}:")
        for pid, value in frame.values.items():
            entry = pid_module.get_pid(pid)
            name = entry.name if entry else f"PID {pid:02X}"
            print(f"  {name:<44} {pid_module.format_value(pid, value)}")
    return 0


def cmd_monitors(args) -> int:
    elm = _connect(args)
    try:
        statuses = ObdSession(elm).read_monitor_status()
    finally:
        elm.close()

    if not statuses:
        print("Блоки не отдали статус мониторов.")
        return 1
    for ecu, status in statuses.items():
        print(f"\nБлок {ecu} — двигатель {status.engine_kind}")
        print(f"  Check Engine: {'ГОРИТ' if status.mil_on else 'не горит'}")
        print(f"  Кодов в памяти: {status.dtc_count}")
        for name, state in status.monitors.items():
            print(f"    {name:<40} {state}")
    return 0


def cmd_vin(args) -> int:
    if args.decode:
        # Расшифровка без подключения к машине.
        for key, value in describe_vin(args.decode):
            print(f"  {key:<28} {value}")
        return 0

    elm = _connect(args)
    try:
        obd = ObdSession(elm)
        vin = obd.read_vin()
        calibrations = obd.read_calibration_ids()
        cvn = obd.read_cvn()
    finally:
        elm.close()

    if not vin:
        print("Блок не отдал VIN. Машины до 2008 года часто не поддерживают этот запрос.")
        if args.vin:
            print("\nРасшифровка номера, указанного вручную:")
            for key, value in describe_vin(args.vin):
                print(f"  {key:<28} {value}")
        return 1

    for key, value in describe_vin(vin):
        print(f"  {key:<28} {value}")
    if args.vin and args.vin.upper() != vin.upper():
        print(f"\n  ВНИМАНИЕ: ожидался {args.vin}, а в блоке записан {vin}")
    if calibrations:
        print("\n  Калибровки прошивок:")
        for ecu, ids in calibrations.items():
            print(f"    {ecu}: {', '.join(ids)}")
    if cvn:
        print("\n  Контрольные суммы калибровок:")
        for ecu, sums in cvn.items():
            print(f"    {ecu}: {', '.join(sums)}")
    return 0


def cmd_ecus(args) -> int:
    elm = _connect(args)
    try:
        obd = ObdSession(elm)
        print(f"Протокол шины: {elm.protocol_name}\n")
        print("Блоки, отвечающие по стандартному OBD-II:")
        standard = obd.discover_ecus()
        if not standard:
            print("  (ни один не ответил -- проверь зажигание)")
        for ecu in standard:
            print(f"  {ecu.address}  {ecu.name or 'назначение не определено'}"
                  f"   параметров: {len(ecu.supported_pids)}")

        if not elm.is_can:
            print("\nРасширенный опрос доступен только на шине CAN.")
            return 0

        print("\nОпрос остальных блоков по расширенному протоколу:")
        for candidate in CAN_ECU_CANDIDATES:
            try:
                with UdsClient(elm, candidate.request, candidate.response) as uds:
                    alive = uds.probe()
                    identification = uds.read_identification() if alive else []
            except (Elm327Error, AdapterError, UdsError):
                alive, identification = False, []
            status = "отвечает" if alive else "не отвечает"
            print(f"  {candidate.response}  {candidate.name:<44} {status}")
            for value in identification:
                print(f"        {value}")
    finally:
        elm.close()
    return 0


def cmd_raw(args) -> int:
    elm = _connect(args)
    try:
        payload = args.command.replace(" ", "").upper()
        if payload.startswith("AT"):
            for line in elm.at(payload):
                print(f"  {line}")
        else:
            for message in elm.query(payload):
                print(f"  {message.ecu}: {message.data.hex().upper()}")
    except (AdapterError, Elm327Error) as exc:
        print(f"  {exc}")
        return 1
    finally:
        elm.close()
    return 0


# ------------------------------------------------------------ команды настроек


def cmd_settings_list(args) -> int:
    registry = load_settings()
    confirmed = settings_ops.load_confirmed()
    print("Каталог настраиваемых функций:\n")
    for key, setting in registry.items():
        did = confirmed.get(key) or setting.did
        state = f"идентификатор {did}" if did else "идентификатор не подтверждён"
        print(f"  {key}")
        print(f"      {setting.title}")
        print(f"      блок: {setting.ecu} ({setting.request_id}) — {state}")
        print(f"      значения: {', '.join(setting.values)}")
        if setting.note:
            print(f"      примечание: {setting.note}")
        print()
    if not any(confirmed.get(k) or s.did for k, s in registry.items()):
        print("Ни один идентификатор пока не подтверждён. Порядок действий описан")
        print("в docs/settings.md — начни с команды: avensis settings discover")
    return 0


def cmd_settings_discover(args) -> int:
    elm = _connect(args)
    try:
        response = args.response or f"{int(args.ecu, 16) + 8:03X}"
        start, _, end = args.range.partition("-")
        start_value, end_value = int(start, 16), int(end or start, 16)
        total_span = end_value - start_value + 1
        print(f"Опрос блока {args.ecu} -> {response}, идентификаторы {start_value:04X}..{end_value:04X}")
        print(f"Всего запросов: {total_span}. Операция только читающая.\n")

        def progress(done: int, total: int) -> None:
            print(f"\r  проверено {done}/{total}", end="", flush=True)

        with UdsClient(elm, args.ecu, response) as uds:
            uds.start_session(0x03)
            found = uds.scan_dids(start_value, end_value, on_progress=progress)
        print()
    except (Elm327Error, AdapterError, UdsError) as exc:
        print(f"\nОпрос не удался: {exc}")
        return 1
    finally:
        elm.close()

    if not found:
        print("\nБлок не отдал ни одного идентификатора в этом диапазоне.")
        return 1

    print(f"\nОтветили {len(found)} идентификаторов:\n")
    for did, raw in sorted(found.items()):
        text = "".join(chr(b) for b in raw if 0x20 <= b < 0x7F)
        suffix = f"  «{text}»" if len(text) >= max(3, len(raw) // 2) else ""
        print(f"  {did}  {raw.hex().upper()}{suffix}")

    if args.save:
        import json  # noqa: PLC0415

        Path(args.save).write_text(
            json.dumps({did: raw.hex().upper() for did, raw in found.items()},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nДамп сохранён: {args.save}")
        print("Измени нужную функцию штатным способом, повтори опрос и сравни дампы —")
        print("отличие покажет, в каком идентификаторе живёт настройка.")
    return 0


def cmd_settings_confirm(args) -> int:
    try:
        settings_ops.confirm_did(args.key, args.did)
    except settings_ops.SettingsError as exc:
        print(exc)
        return 1
    print(f"Для настройки «{args.key}» закреплён идентификатор {args.did.upper()}.")
    print(f"Запись сохранена в {settings_ops.CONFIRMED_FILE}")
    return 0


def cmd_settings_read(args) -> int:
    registry = load_settings()
    keys = [args.key] if args.key else list(registry)
    elm = _connect(args)
    failures = 0
    try:
        for key in keys:
            setting = registry.get(key)
            if setting is None:
                print(f"{key}: нет такой настройки в каталоге")
                failures += 1
                continue
            try:
                with UdsClient(elm, setting.request_id, setting.response_id) as uds:
                    uds.start_session(0x03)
                    state = settings_ops.read_setting(uds, setting)
            except (settings_ops.SettingsError, UdsError, AdapterError, Elm327Error) as exc:
                print(f"{key}: {exc}\n")
                failures += 1
                continue
            print(f"{key} — {setting.title}")
            print(f"    сейчас: «{state.value_name}»")
            print(f"    ячейка {state.did}, содержимое {state.raw.hex().upper()}\n")
    finally:
        elm.close()
    return 1 if failures and len(keys) == 1 else 0


def cmd_settings_write(args) -> int:
    setting = load_settings().get(args.key)
    if setting is None:
        print(f"Нет настройки {args.key!r}. Список: avensis settings list")
        return 1
    if args.value not in setting.values:
        print(f"Недопустимое значение. Доступны: {', '.join(setting.values)}")
        return 1

    if not args.apply:
        print("Проверочный прогон (на шину ничего не пишется). "
              "Для реальной записи добавь --apply.\n")
    else:
        print(DESTRUCTIVE_NOTICE)
        if not setting.verified:
            print("Эта настройка помечена как неподтверждённая: её расположение в памяти")
            print("установлено опытным путём и может отличаться на твоей машине.")
            print("Резервная копия ячейки будет снята автоматически.\n")
        if not _confirm(f"Записать «{args.value}» в настройку «{setting.title}»?", args.yes):
            print("Отменено.")
            return 2

    elm = _connect(args)
    try:
        with UdsClient(elm, setting.request_id, setting.response_id) as uds:
            uds.start_session(0x03)
            result = settings_ops.write_setting(
                uds, setting, args.value,
                dry_run=not args.apply,
                backup_directory=Path(args.backup_dir) if args.backup_dir else None,
                vin=args.vin or "",
            )
    except (settings_ops.SettingsError, UdsError, AdapterError, Elm327Error) as exc:
        print(f"Не выполнено: {exc}")
        return 1
    finally:
        elm.close()

    print(f"Настройка: {result.setting.title}")
    print(f"Ячейка:    {result.did}")
    print(f"Было:      {result.before.hex().upper()}")
    print(f"Стало:     {result.after.hex().upper()}")
    if result.backup_path:
        print(f"Копия:     {result.backup_path}")
    print(f"\n{result.message}")
    return 0 if (result.verified or not args.apply) else 1


def cmd_settings_backup(args) -> int:
    elm = _connect(args)
    try:
        response = args.response or f"{int(args.ecu, 16) + 8:03X}"
        dids = args.dids.split(",") if args.dids else list(IDENTIFICATION_DIDS)
        with UdsClient(elm, args.ecu, response) as uds:
            uds.start_session(0x03)
            backup = settings_ops.backup_ecu(uds, dids, ecu_name=args.ecu, vin=args.vin or "")
        path = settings_ops.save_backup(backup, Path(args.output) if args.output else None)
    except (settings_ops.SettingsError, UdsError, AdapterError, Elm327Error) as exc:
        print(f"Копия не создана: {exc}")
        return 1
    finally:
        elm.close()
    print(f"Сохранено идентификаторов: {len(backup.dids)}")
    print(f"Файл: {path}")
    return 0


def cmd_settings_restore(args) -> int:
    backup = settings_ops.Backup.from_file(Path(args.file))
    print(f"Копия от {backup.created}, блок {backup.ecu} ({backup.response_id}), "
          f"идентификаторов: {len(backup.dids)}")
    if backup.vin and args.vin and backup.vin.upper() != args.vin.upper():
        print(f"\nКопия снята с машины {backup.vin}, а указан {args.vin}. Восстановление отменено.")
        return 1

    if args.apply:
        print(DESTRUCTIVE_NOTICE)
        if not _confirm("Восстановить блок из копии?", args.yes):
            print("Отменено.")
            return 2
    else:
        print("Проверочный прогон. Для реальной записи добавь --apply.\n")

    elm = _connect(args)
    try:
        with UdsClient(elm, backup.request_id, backup.response_id) as uds:
            lines = settings_ops.restore_backup(uds, backup, dry_run=not args.apply)
    except (UdsError, AdapterError, Elm327Error) as exc:
        print(f"Восстановление прервано: {exc}")
        return 1
    finally:
        elm.close()
    for line in lines:
        print(f"  {line}")
    return 0


# ------------------------------------------------------------------- разбор строки


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="avensis",
        description="Диагностика Toyota Avensis через адаптер OBD-II (ELM327).",
        epilog="Без указания --port программа работает с встроенным симулятором.",
    )
    parser.add_argument("--version", action="version", version=f"avensis {__version__}")
    parser.add_argument(
        "-p", "--port", default=DEFAULT_PORT,
        help="адаптер: /dev/ttyUSB0, /dev/rfcomm0, COM3, tcp://192.168.0.10:35000 или sim://",
    )
    parser.add_argument(
        "--protocol", default="0",
        help="номер протокола ELM327; 0 -- автоопределение (по умолчанию)",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="таймаут ответа адаптера, с")
    parser.add_argument("--vin", help="ожидаемый VIN -- программа сверит его с записанным в блоке")
    parser.add_argument("--trace", action="store_true", help="печатать весь обмен с адаптером")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="подробный журнал")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="полная диагностика и отчёт")
    scan.add_argument("--json", action="store_true", help="вывести отчёт в JSON")
    scan.add_argument("--markdown", action="store_true", help="вывести отчёт в Markdown")
    scan.add_argument("--extended", action="store_true", help="опросить также ABS, SRS и кузов")
    scan.add_argument("--no-live", action="store_true", help="не снимать текущие параметры")
    scan.add_argument("-o", "--output", help="сохранить отчёт в файл")
    scan.set_defaults(func=cmd_scan)

    dtc = sub.add_parser("dtc", help="прочитать коды неисправностей")
    dtc.add_argument("--stored", action="store_true", help="сохранённые (по умолчанию)")
    dtc.add_argument("--pending", action="store_true", help="неподтверждённые")
    dtc.add_argument("--permanent", action="store_true", help="постоянные")
    dtc.set_defaults(func=cmd_dtc)

    clear = sub.add_parser("clear", help="стереть коды неисправностей")
    clear.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    clear.set_defaults(func=cmd_clear)

    live = sub.add_parser("live", help="показывать параметры в реальном времени")
    live.add_argument("pids", nargs="*", help="номера параметров в hex, например 0C 0D 05")
    live.add_argument("--interval", type=float, default=1.0, help="пауза между опросами, с")
    live.add_argument("--count", type=int, help="сколько снимков сделать")
    live.add_argument("--duration", type=float, help="сколько секунд писать")
    live.add_argument("--csv", help="дополнительно записывать в CSV-файл")
    live.set_defaults(func=cmd_live)

    freeze = sub.add_parser("freeze", help="показать стоп-кадр")
    freeze.add_argument("--frame", type=int, default=0, help="номер кадра")
    freeze.set_defaults(func=cmd_freeze)

    monitors = sub.add_parser("monitors", help="готовность систем самодиагностики")
    monitors.set_defaults(func=cmd_monitors)

    vin = sub.add_parser("vin", help="прочитать и расшифровать VIN")
    vin.add_argument("--decode", help="расшифровать указанный номер без подключения к машине")
    vin.set_defaults(func=cmd_vin)

    ecus = sub.add_parser("ecus", help="найти блоки управления на шине")
    ecus.set_defaults(func=cmd_ecus)

    raw = sub.add_parser("raw", help="отправить произвольную команду адаптеру")
    raw.add_argument("command", help="например 010C или ATRV")
    raw.set_defaults(func=cmd_raw)

    settings = sub.add_parser("settings", help="работа с настраиваемыми функциями")
    settings_sub = settings.add_subparsers(dest="settings_command", required=True)

    s_list = settings_sub.add_parser("list", help="каталог настроек")
    s_list.set_defaults(func=cmd_settings_list)

    s_discover = settings_sub.add_parser("discover", help="найти идентификаторы данных в блоке")
    s_discover.add_argument("--ecu", default="740", help="адрес запроса, например 740")
    s_discover.add_argument("--response", help="адрес ответа; по умолчанию адрес запроса + 8")
    s_discover.add_argument("--range", default="0100-01FF", help="диапазон в hex, например 0100-01FF")
    s_discover.add_argument("--save", help="сохранить дамп в файл для последующего сравнения")
    s_discover.set_defaults(func=cmd_settings_discover)

    s_confirm = settings_sub.add_parser("confirm", help="закрепить найденный идентификатор")
    s_confirm.add_argument("key", help="ключ настройки из каталога")
    s_confirm.add_argument("did", help="идентификатор данных, например 0100")
    s_confirm.set_defaults(func=cmd_settings_confirm)

    s_read = settings_sub.add_parser("read", help="прочитать текущее значение настройки")
    s_read.add_argument("key", nargs="?", help="ключ настройки; без него читаются все")
    s_read.set_defaults(func=cmd_settings_read)

    s_write = settings_sub.add_parser("write", help="изменить настройку")
    s_write.add_argument("key", help="ключ настройки")
    s_write.add_argument("value", help="новое значение из каталога")
    s_write.add_argument("--apply", action="store_true", help="действительно записать, а не проверить")
    s_write.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    s_write.add_argument("--backup-dir", help="куда положить резервную копию")
    s_write.set_defaults(func=cmd_settings_write)

    s_backup = settings_sub.add_parser("backup", help="снять резервную копию блока")
    s_backup.add_argument("--ecu", default="740", help="адрес запроса")
    s_backup.add_argument("--response", help="адрес ответа")
    s_backup.add_argument("--dids", help="список идентификаторов через запятую")
    s_backup.add_argument("-o", "--output", help="каталог для копии")
    s_backup.set_defaults(func=cmd_settings_backup)

    s_restore = settings_sub.add_parser("restore", help="восстановить блок из копии")
    s_restore.add_argument("file", help="файл резервной копии")
    s_restore.add_argument("--apply", action="store_true", help="действительно записать")
    s_restore.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    s_restore.set_defaults(func=cmd_settings_restore)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        return args.func(args)
    except (TransportError, Elm327Error) as exc:
        print(f"\nНет связи с адаптером: {exc}")
        print("\nЧто проверить:")
        print("  • адаптер до конца вставлен в разъём OBD-II под рулевой колонкой;")
        print("  • зажигание включено в положение ON (двигатель можно не заводить);")
        print("  • указан правильный порт (--port), для Bluetooth он привязан через rfcomm;")
        print("  • у пользователя есть доступ к порту (в Linux -- группа dialout).")
        return 1
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
