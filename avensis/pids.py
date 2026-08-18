"""Параметры реального времени OBD-II (режим 01) и их пересчёт в физические величины.

Формулы взяты из SAE J1979. Каждый параметр знает свою длину в байтах,
поэтому усечённый ответ отбраковывается, а не превращается в мусорное число.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

FUEL_TYPES = {
    0: "не задано",
    1: "бензин",
    2: "метанол",
    3: "этанол",
    4: "дизель",
    5: "сжиженный газ (LPG)",
    6: "сжатый газ (CNG)",
    7: "пропан",
    8: "электро",
    9: "бифуэл: бензин",
    10: "бифуэл: метанол",
    11: "бифуэл: этанол",
    12: "бифуэл: LPG",
    13: "бифуэл: CNG",
    14: "бифуэл: пропан",
    15: "бифуэл: электро",
}

FUEL_SYSTEM_STATUS = {
    0: "выключена",
    1: "разомкнутый контур: двигатель не прогрет",
    2: "замкнутый контур: по датчику кислорода",
    4: "разомкнутый контур: нагрузка или отсечка",
    8: "разомкнутый контур: неисправность в системе",
    16: "замкнутый контур, но датчик кислорода неисправен",
}

OBD_STANDARDS = {
    1: "OBD-II (Калифорния ARB)",
    2: "OBD (федеральный EPA)",
    3: "OBD и OBD-II",
    4: "OBD-I",
    5: "без соответствия OBD",
    6: "EOBD (Европа)",
    7: "EOBD и OBD-II",
    8: "EOBD и OBD",
    9: "EOBD, OBD и OBD-II",
    10: "JOBD (Япония)",
}


@dataclass(frozen=True)
class Pid:
    """Описание одного параметра режима 01."""

    pid: int
    name: str
    unit: str
    length: int
    decode: Callable[[bytes], object]

    @property
    def hex(self) -> str:
        return f"{self.pid:02X}"


def _u16(d: bytes) -> int:
    return (d[0] << 8) | d[1]


def _percent(d: bytes) -> float:
    return round(d[0] * 100 / 255, 1)


def _trim(d: bytes) -> float:
    return round((d[0] - 128) * 100 / 128, 1)


def _temp(d: bytes) -> int:
    return d[0] - 40


def _o2_voltage(d: bytes) -> dict:
    return {
        "напряжение, В": round(d[0] / 200, 3),
        "коррекция, %": round((d[1] - 128) * 100 / 128, 1) if d[1] != 0xFF else None,
    }


#: Основная таблица параметров. Порядок задаёт вывод в живом мониторинге.
PID_TABLE: Dict[int, Pid] = {}


def _register(pid: int, name: str, unit: str, length: int, decode: Callable[[bytes], object]) -> None:
    PID_TABLE[pid] = Pid(pid=pid, name=name, unit=unit, length=length, decode=decode)


_register(0x03, "Статус топливной системы", "", 2,
          lambda d: FUEL_SYSTEM_STATUS.get(d[0], f"неизвестно (0x{d[0]:02X})"))
_register(0x04, "Расчётная нагрузка на двигатель", "%", 1, _percent)
_register(0x05, "Температура охлаждающей жидкости", "°C", 1, _temp)
_register(0x06, "Кратковременная топливная коррекция, банк 1", "%", 1, _trim)
_register(0x07, "Долговременная топливная коррекция, банк 1", "%", 1, _trim)
_register(0x08, "Кратковременная топливная коррекция, банк 2", "%", 1, _trim)
_register(0x09, "Долговременная топливная коррекция, банк 2", "%", 1, _trim)
_register(0x0A, "Давление топлива", "кПа", 1, lambda d: d[0] * 3)
_register(0x0B, "Абсолютное давление во впуске", "кПа", 1, lambda d: d[0])
_register(0x0C, "Обороты двигателя", "об/мин", 2, lambda d: round(_u16(d) / 4))
_register(0x0D, "Скорость автомобиля", "км/ч", 1, lambda d: d[0])
_register(0x0E, "Угол опережения зажигания", "°", 1, lambda d: round(d[0] / 2 - 64, 1))
_register(0x0F, "Температура воздуха на впуске", "°C", 1, _temp)
_register(0x10, "Массовый расход воздуха", "г/с", 2, lambda d: round(_u16(d) / 100, 2))
_register(0x11, "Положение дроссельной заслонки", "%", 1, _percent)
_register(0x14, "Кислородный датчик B1S1", "", 2, _o2_voltage)
_register(0x15, "Кислородный датчик B1S2", "", 2, _o2_voltage)
_register(0x1C, "Стандарт OBD", "", 1,
          lambda d: OBD_STANDARDS.get(d[0], f"неизвестно (0x{d[0]:02X})"))
_register(0x1F, "Время работы после пуска", "с", 2, _u16)
_register(0x21, "Пробег с горящим Check Engine", "км", 2, _u16)
_register(0x22, "Давление в топливной рампе (отн. впуска)", "кПа", 2,
          lambda d: round(_u16(d) * 0.079, 1))
_register(0x23, "Давление в топливной рампе", "кПа", 2, lambda d: _u16(d) * 10)
_register(0x2C, "Заданная степень рециркуляции ОГ", "%", 1, _percent)
_register(0x2D, "Ошибка регулирования EGR", "%", 1, _trim)
_register(0x2E, "Заданная продувка адсорбера", "%", 1, _percent)
_register(0x2F, "Уровень топлива в баке", "%", 1, _percent)
_register(0x30, "Прогревов с момента сброса ошибок", "", 1, lambda d: d[0])
_register(0x31, "Пробег с момента сброса ошибок", "км", 2, _u16)
_register(0x33, "Барометрическое давление", "кПа", 1, lambda d: d[0])
_register(0x3C, "Температура катализатора B1S1", "°C", 2, lambda d: round(_u16(d) / 10 - 40, 1))
_register(0x3E, "Температура катализатора B1S2", "°C", 2, lambda d: round(_u16(d) / 10 - 40, 1))
_register(0x42, "Напряжение питания блока управления", "В", 2, lambda d: round(_u16(d) / 1000, 3))
_register(0x43, "Абсолютная нагрузка на двигатель", "%", 2, lambda d: round(_u16(d) * 100 / 255, 1))
_register(0x44, "Заданный коэффициент избытка воздуха (лямбда)", "", 2,
          lambda d: round(_u16(d) / 32768, 3))
_register(0x45, "Относительное положение дросселя", "%", 1, _percent)
_register(0x46, "Температура наружного воздуха", "°C", 1, _temp)
_register(0x47, "Абсолютное положение дросселя B", "%", 1, _percent)
_register(0x49, "Положение педали акселератора D", "%", 1, _percent)
_register(0x4A, "Положение педали акселератора E", "%", 1, _percent)
_register(0x4C, "Заданное положение привода дросселя", "%", 1, _percent)
_register(0x4D, "Время работы с горящим Check Engine", "мин", 2, _u16)
_register(0x4E, "Время с момента сброса ошибок", "мин", 2, _u16)
_register(0x51, "Тип топлива", "", 1,
          lambda d: FUEL_TYPES.get(d[0], f"неизвестно (0x{d[0]:02X})"))
_register(0x5C, "Температура моторного масла", "°C", 1, _temp)
_register(0x5E, "Расход топлива двигателем", "л/ч", 2, lambda d: round(_u16(d) / 20, 2))
_register(0x62, "Фактический крутящий момент", "%", 1, lambda d: d[0] - 125)
_register(0x63, "Опорный крутящий момент двигателя", "Н·м", 2, _u16)

#: Компактный набор для живого мониторинга -- то, на что смотрят в первую очередь.
LIVE_DEFAULT = [0x0C, 0x0D, 0x05, 0x0F, 0x04, 0x11, 0x10, 0x06, 0x07, 0x42]

#: PID'ы, по которым запрашиваются битовые маски поддержки.
SUPPORT_PIDS = [0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0]


def get_pid(pid: int) -> Optional[Pid]:
    return PID_TABLE.get(pid)


def decode_value(pid: int, data: bytes):
    """Пересчитать сырые байты параметра в физическую величину.

    Возвращает ``None``, если параметр неизвестен или ответ короче, чем нужно.
    """
    entry = PID_TABLE.get(pid)
    if entry is None or len(data) < entry.length:
        return None
    return entry.decode(data[: entry.length])


def decode_supported(base_pid: int, data: bytes) -> List[int]:
    """Разобрать битовую маску поддерживаемых параметров.

    Ответ на ``0100`` описывает PID'ы 0x01..0x20, на ``0120`` -- 0x21..0x40 и так далее.
    Старший бит первого байта соответствует ``base_pid + 1``.
    """
    supported = []
    for byte_index, byte in enumerate(data[:4]):
        for bit in range(8):
            if byte & (0x80 >> bit):
                supported.append(base_pid + byte_index * 8 + bit + 1)
    return supported


# ----------------------------------------------------------- готовность систем

#: Мониторы бензинового двигателя (байты C/D ответа на PID 01).
SPARK_MONITORS = [
    "Катализатор",
    "Подогрев катализатора",
    "Система улавливания паров (EVAP)",
    "Система подачи вторичного воздуха",
    "Хладагент кондиционера",
    "Кислородные датчики",
    "Подогрев кислородных датчиков",
    "Рециркуляция ОГ (EGR)",
]

#: Мониторы дизельного двигателя -- те же биты, другой смысл.
COMPRESSION_MONITORS = [
    "Катализатор NMHC",
    "Нейтрализация NOx / SCR",
    "(зарезервировано)",
    "Система наддува",
    "(зарезервировано)",
    "Датчики отработавших газов",
    "Сажевый фильтр (DPF)",
    "EGR / система изменения фаз",
]

#: Мониторы, обязательные для любого двигателя (байт B, младшие биты).
CONTINUOUS_MONITORS = ["Пропуски воспламенения", "Топливная система", "Компоненты"]


@dataclass
class MonitorStatus:
    """Результат разбора PID 01: индикатор Check Engine и готовность мониторов."""

    mil_on: bool
    dtc_count: int
    compression_ignition: bool
    monitors: Dict[str, str]

    @property
    def engine_kind(self) -> str:
        return "дизельный" if self.compression_ignition else "бензиновый"

    @property
    def not_ready(self) -> List[str]:
        return [name for name, state in self.monitors.items() if state == "не завершён"]


def decode_monitor_status(data: bytes) -> Optional[MonitorStatus]:
    """Разобрать ответ на PID 01 (статус индикатора и готовность мониторов)."""
    if len(data) < 4:
        return None
    a, b, c, d = data[0], data[1], data[2], data[3]

    compression = bool(b & 0x08)
    monitors: Dict[str, str] = {}

    for bit, name in enumerate(CONTINUOUS_MONITORS):
        if not b & (1 << bit):
            monitors[name] = "не поддерживается"
        else:
            monitors[name] = "не завершён" if b & (1 << (bit + 4)) else "завершён"

    names = COMPRESSION_MONITORS if compression else SPARK_MONITORS
    for bit, name in enumerate(names):
        if name.startswith("("):
            continue
        if not c & (1 << bit):
            monitors[name] = "не поддерживается"
        else:
            monitors[name] = "не завершён" if d & (1 << bit) else "завершён"

    return MonitorStatus(
        mil_on=bool(a & 0x80),
        dtc_count=a & 0x7F,
        compression_ignition=compression,
        monitors=monitors,
    )


def format_value(pid: int, value) -> str:
    """Привести значение параметра к виду, пригодному для вывода в консоль."""
    entry = PID_TABLE.get(pid)
    if value is None:
        return "—"
    if isinstance(value, dict):
        parts = [f"{k}: {v}" for k, v in value.items() if v is not None]
        return ", ".join(parts) if parts else "—"
    unit = f" {entry.unit}" if entry and entry.unit else ""
    return f"{value}{unit}"


def known_pids() -> Sequence[Pid]:
    return [PID_TABLE[p] for p in sorted(PID_TABLE)]
