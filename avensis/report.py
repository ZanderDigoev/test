"""Сбор результатов диагностики и их представление в разных форматах."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from avensis import pids as pid_module
from avensis.dtc import Dtc, DtcStatus
from avensis.engines import EngineDetection
from avensis.obd import EcuInfo, FreezeFrame
from avensis.vin import describe_vin

SEPARATOR = "─" * 72


@dataclass
class VehicleReport:
    """Полный результат сеанса диагностики."""

    created: str = field(
        default_factory=lambda: datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    )
    adapter: str = ""
    connection: str = ""
    protocol: str = ""
    voltage: Optional[float] = None
    vin_from_ecu: Optional[str] = None
    vin_expected: Optional[str] = None
    engine: Optional[EngineDetection] = None
    ecus: List[EcuInfo] = field(default_factory=list)
    extended_ecus: List[Dict[str, Any]] = field(default_factory=list)
    monitors: Dict[str, pid_module.MonitorStatus] = field(default_factory=dict)
    dtcs: List[Dtc] = field(default_factory=list)
    freeze_frames: List[FreezeFrame] = field(default_factory=list)
    calibration_ids: Dict[str, List[str]] = field(default_factory=dict)
    live: Dict[int, Dict[str, object]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    # ------------------------------------------------------------- свойства

    @property
    def mil_on(self) -> bool:
        return any(status.mil_on for status in self.monitors.values())

    @property
    def vin_matches(self) -> Optional[bool]:
        """Совпадает ли VIN из блока с ожидаемым. ``None``, если сравнивать не с чем."""
        if not self.vin_from_ecu or not self.vin_expected:
            return None
        return self.vin_from_ecu.upper() == self.vin_expected.upper()

    def by_status(self, status: str) -> List[Dtc]:
        return [code for code in self.dtcs if code.status == status]

    @property
    def typical_faults(self) -> List[Dtc]:
        """Найденные коды, которые для этого мотора считаются типовыми.

        Совпадение ничего не доказывает, но подсказывает, с чего начинать
        проверку: такие неисправности на этом двигателе встречаются чаще прочих.
        """
        profile = self.engine.profile if self.engine else None
        if profile is None:
            return []
        watch = set(profile.watch_codes)
        # Один код может прийти и как сохранённый, и как постоянный --
        # в подсказке он нужен один раз.
        seen, unique = set(), []
        for code in self.dtcs:
            if code.code in watch and code.code not in seen:
                seen.add(code.code)
                unique.append(code)
        return unique

    # -------------------------------------------------------- сериализация

    def to_dict(self) -> Dict[str, Any]:
        return {
            "created": self.created,
            "adapter": self.adapter,
            "connection": self.connection,
            "protocol": self.protocol,
            "voltage": self.voltage,
            "vin": {
                "from_ecu": self.vin_from_ecu,
                "expected": self.vin_expected,
                "matches": self.vin_matches,
                "decoded": dict(describe_vin(self.vin_from_ecu or self.vin_expected))
                if (self.vin_from_ecu or self.vin_expected)
                else {},
            },
            "engine": {
                "fuel": self.engine.fuel,
                "source": self.engine.source,
                "description": self.engine.describe(),
                "candidates": [p.code for p in self.engine.candidates],
                "selected": self.engine.profile.code if self.engine.profile else None,
            }
            if self.engine
            else None,
            "typical_faults": [code.code for code in self.typical_faults],
            "ecus": [asdict(ecu) for ecu in self.ecus],
            "extended_ecus": self.extended_ecus,
            "monitors": {
                ecu: {
                    "mil_on": status.mil_on,
                    "dtc_count": status.dtc_count,
                    "engine_kind": status.engine_kind,
                    "monitors": status.monitors,
                }
                for ecu, status in self.monitors.items()
            },
            "dtcs": [asdict(code) for code in self.dtcs],
            "freeze_frames": [
                {
                    "ecu": frame.ecu,
                    "trigger_dtc": frame.trigger_dtc,
                    "values": {
                        f"{pid:02X}": {
                            "name": (p.name if (p := pid_module.get_pid(pid)) else f"PID {pid:02X}"),
                            "value": value,
                        }
                        for pid, value in frame.values.items()
                    },
                }
                for frame in self.freeze_frames
            ],
            "calibration_ids": self.calibration_ids,
            "live": {
                f"{pid:02X}": {
                    "name": (p.name if (p := pid_module.get_pid(pid)) else f"PID {pid:02X}"),
                    "values": values,
                }
                for pid, values in self.live.items()
            },
            "warnings": self.warnings,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str)


# ----------------------------------------------------------------- текстовый вид


def _section(title: str) -> str:
    return f"\n{SEPARATOR}\n {title}\n{SEPARATOR}"


def render_text(report: VehicleReport) -> str:
    """Отчёт для чтения в терминале."""
    out: List[str] = []
    out.append(_section("ПОДКЛЮЧЕНИЕ"))
    out.append(f"  Время сеанса      {report.created}")
    out.append(f"  Адаптер           {report.adapter}")
    out.append(f"  Канал связи       {report.connection}")
    out.append(f"  Протокол шины     {report.protocol}")
    if report.voltage is not None:
        note = "" if report.voltage >= 12.0 else "  ← низкое, проверь аккумулятор"
        out.append(f"  Напряжение сети   {report.voltage} В{note}")

    if report.vin_from_ecu or report.vin_expected:
        out.append(_section("АВТОМОБИЛЬ"))
        for key, value in describe_vin(report.vin_from_ecu or report.vin_expected):
            out.append(f"  {key:<26} {value}")
        if report.vin_matches is True:
            out.append("  VIN из блока совпадает с ожидаемым")
        elif report.vin_matches is False:
            out.append(
                f"  ВНИМАНИЕ: в блоке записан {report.vin_from_ecu}, "
                f"а ожидался {report.vin_expected}"
            )
        elif report.vin_expected and not report.vin_from_ecu:
            out.append("  Блок не отдал VIN (обычное дело для машин до 2008 года)")

    if report.engine:
        out.append(_section("ДВИГАТЕЛЬ"))
        out.append(f"  {report.engine.describe()}")
        profile = report.engine.profile
        if profile and report.engine.forced:
            out.append(f"  {profile.displacement}, годы выпуска {profile.years}")
            if profile.notes:
                out.append(f"  {profile.notes}")
        elif report.engine.candidates:
            out.append("  Точный код мотора по шине не передаётся — если знаешь его,")
            out.append("  укажи флагом --engine, и подбор параметров станет точнее.")

    if report.ecus:
        out.append(_section("БЛОКИ УПРАВЛЕНИЯ НА ШИНЕ"))
        for ecu in report.ecus:
            title = ecu.name or "назначение не определено"
            out.append(f"  {ecu.address}  {title}")
            if ecu.supported_pids:
                out.append(f"        поддерживает параметров: {len(ecu.supported_pids)}")

    if report.extended_ecus:
        out.append(_section("БЛОКИ, ОПРОШЕННЫЕ ПО РАСШИРЕННОМУ ПРОТОКОЛУ"))
        for entry in report.extended_ecus:
            out.append(f"  {entry['response_id']}  {entry['name']}")
            for line in entry.get("identification", []):
                out.append(f"        {line}")
            for code in entry.get("dtcs", []):
                out.append(f"        [ошибка] {code}")

    out.append(_section("СОСТОЯНИЕ ИНДИКАТОРА И ГОТОВНОСТЬ СИСТЕМ"))
    if not report.monitors:
        out.append("  Блоки не отдали статус мониторов")
    for ecu, status in report.monitors.items():
        lamp = "ГОРИТ" if status.mil_on else "не горит"
        out.append(f"  {ecu}: Check Engine {lamp}, кодов в памяти {status.dtc_count}, "
                   f"двигатель {status.engine_kind}")
        not_ready = status.not_ready
        if not_ready:
            out.append(f"        не завершены проверки: {', '.join(not_ready)}")
        else:
            out.append("        все поддерживаемые проверки завершены")

    out.append(_section("КОДЫ НЕИСПРАВНОСТЕЙ"))
    if not report.dtcs:
        out.append("  Ошибок не найдено")
    for label, status in (
        ("Сохранённые (зажгли Check Engine)", DtcStatus.STORED),
        ("Неподтверждённые (сбой был однократно)", DtcStatus.PENDING),
        ("Постоянные (стираются только самим блоком)", DtcStatus.PERMANENT),
    ):
        group = report.by_status(status)
        if not group:
            continue
        out.append(f"\n  {label}:")
        for code in group:
            mark = "" if code.known else "  [описания в базе нет]"
            out.append(f"    {code.code}  [{code.ecu}]  {code.description}{mark}")

    typical = report.typical_faults
    if typical:
        out.append(_section("ТИПОВЫЕ ДЛЯ ЭТОГО МОТОРА"))
        out.append("  Эти коды на таком двигателе встречаются чаще прочих —")
        out.append("  разумно начать проверку с них:")
        for code in typical:
            out.append(f"    {code.code}  {code.description}")

    if report.freeze_frames:
        out.append(_section("СТОП-КАДР (параметры в момент фиксации ошибки)"))
        for frame in report.freeze_frames:
            trigger = frame.trigger_dtc or "код не указан"
            out.append(f"  Блок {frame.ecu}, ошибка {trigger}:")
            for pid, value in frame.values.items():
                entry = pid_module.get_pid(pid)
                name = entry.name if entry else f"PID {pid:02X}"
                out.append(f"    {name:<40} {pid_module.format_value(pid, value)}")

    if report.calibration_ids:
        out.append(_section("КАЛИБРОВКИ ПРОШИВОК"))
        for ecu, ids in report.calibration_ids.items():
            out.append(f"  {ecu}: {', '.join(ids)}")

    if report.live:
        out.append(_section("ТЕКУЩИЕ ПАРАМЕТРЫ"))
        for pid, values in report.live.items():
            entry = pid_module.get_pid(pid)
            name = entry.name if entry else f"PID {pid:02X}"
            for ecu, value in values.items():
                out.append(f"  {name:<42} {pid_module.format_value(pid, value):>22}  [{ecu}]")

    if report.warnings:
        out.append(_section("ЗАМЕЧАНИЯ"))
        for warning in report.warnings:
            out.append(f"  • {warning}")

    return "\n".join(out) + "\n"


def render_markdown(report: VehicleReport) -> str:
    """Отчёт для сохранения в файл или отправки мастеру."""
    out: List[str] = ["# Отчёт диагностики Toyota Avensis", ""]
    out.append(f"**Дата сеанса:** {report.created}  ")
    out.append(f"**Адаптер:** {report.adapter} через {report.connection}  ")
    out.append(f"**Протокол:** {report.protocol}  ")
    if report.voltage is not None:
        out.append(f"**Напряжение бортовой сети:** {report.voltage} В  ")
    out.append("")

    vin = report.vin_from_ecu or report.vin_expected
    if vin:
        out += ["## Автомобиль", "", "| Параметр | Значение |", "| --- | --- |"]
        out += [f"| {key} | {value} |" for key, value in describe_vin(vin)]
        if report.vin_matches is False:
            out.append(f"| ⚠ Расхождение | в блоке {report.vin_from_ecu}, ожидался {report.vin_expected} |")
        out.append("")

    if report.engine:
        out += ["## Двигатель", "", report.engine.describe(), ""]
        profile = report.engine.profile
        if profile and report.engine.forced and profile.notes:
            out += [f"> {profile.notes}", ""]

    if report.ecus:
        out += ["## Блоки управления", "", "| Адрес | Назначение | Параметров |", "| --- | --- | --- |"]
        out += [
            f"| `{ecu.address}` | {ecu.name or '—'} | {len(ecu.supported_pids) or '—'} |"
            for ecu in report.ecus
        ]
        out.append("")

    out += ["## Коды неисправностей", ""]
    if not report.dtcs:
        out += ["Ошибок не обнаружено.", ""]
    else:
        out += ["| Код | Категория | Блок | Описание |", "| --- | --- | --- | --- |"]
        out += [
            f"| **{code.code}** | {code.status} | `{code.ecu}` | {code.description} |"
            for code in report.dtcs
        ]
        out.append("")

    if report.typical_faults:
        out += ["## Типовые для этого мотора", "",
                "Эти коды на таком двигателе встречаются чаще прочих:", ""]
        out += [f"- **{c.code}** — {c.description}" for c in report.typical_faults]
        out.append("")

    if report.monitors:
        out += ["## Готовность систем самодиагностики", ""]
        for ecu, status in report.monitors.items():
            lamp = "горит" if status.mil_on else "не горит"
            out.append(f"**Блок `{ecu}`** — Check Engine {lamp}, двигатель {status.engine_kind}")
            out.append("")
            out += ["| Проверка | Состояние |", "| --- | --- |"]
            out += [f"| {name} | {state} |" for name, state in status.monitors.items()]
            out.append("")

    if report.freeze_frames:
        out += ["## Стоп-кадр", ""]
        for frame in report.freeze_frames:
            out.append(f"**Блок `{frame.ecu}`, ошибка {frame.trigger_dtc or '—'}**")
            out.append("")
            out += ["| Параметр | Значение |", "| --- | --- |"]
            for pid, value in frame.values.items():
                entry = pid_module.get_pid(pid)
                name = entry.name if entry else f"PID {pid:02X}"
                out.append(f"| {name} | {pid_module.format_value(pid, value)} |")
            out.append("")

    if report.live:
        out += ["## Текущие параметры", "", "| Параметр | Значение | Блок |", "| --- | --- | --- |"]
        for pid, values in report.live.items():
            entry = pid_module.get_pid(pid)
            name = entry.name if entry else f"PID {pid:02X}"
            for ecu, value in values.items():
                out.append(f"| {name} | {pid_module.format_value(pid, value)} | `{ecu}` |")
        out.append("")

    if report.warnings:
        out += ["## Замечания", ""] + [f"- {w}" for w in report.warnings] + [""]

    return "\n".join(out)
