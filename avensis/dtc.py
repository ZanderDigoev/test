"""Коды неисправностей: кодирование, декодирование и человеческие описания.

Диагностический код занимает на шине два байта. Старшие два бита первого
байта задают букву (P/C/B/U), следующие два -- первую цифру, оставшиеся
двенадцать бит -- три шестнадцатеричные цифры.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

DATA_DIR = Path(__file__).parent / "data"

#: Буква кода по старшим двум битам.
SYSTEM_LETTERS = {0: "P", 1: "C", 2: "B", 3: "U"}
LETTER_TO_BITS = {letter: bits for bits, letter in SYSTEM_LETTERS.items()}

SYSTEM_NAMES = {
    "P": "Силовой агрегат",
    "C": "Шасси",
    "B": "Кузов",
    "U": "Сеть / обмен данными",
}

#: Подсистема по номеру кода, когда точного описания в базе нет.
_SUBSYSTEM_RANGES = (
    (0x000, 0x0FF, "дозирование топлива и воздуха"),
    (0x100, 0x1FF, "дозирование топлива и воздуха"),
    (0x200, 0x2FF, "система впрыска топлива"),
    (0x300, 0x3FF, "система зажигания и пропуски воспламенения"),
    (0x400, 0x4FF, "система снижения токсичности"),
    (0x500, 0x5FF, "холостой ход и вспомогательные системы"),
    (0x600, 0x6FF, "блок управления и его выходы"),
    (0x700, 0x7FF, "трансмиссия"),
    (0x800, 0x8FF, "трансмиссия"),
    (0x900, 0x9FF, "трансмиссия"),
    (0xA00, 0xFFF, "гибридная установка и прочее"),
)

DTC_PATTERN = re.compile(r"^[PCBU][0-3][0-9A-F]{3}$")


class DtcStatus:
    """Откуда получен код -- от этого зависит, как его читать."""

    STORED = "сохранённый"
    PENDING = "неподтверждённый"
    PERMANENT = "постоянный"


@dataclass(frozen=True)
class Dtc:
    """Один код неисправности с описанием и происхождением."""

    code: str
    description: str
    status: str = DtcStatus.STORED
    ecu: str = ""
    known: bool = True

    @property
    def system(self) -> str:
        return SYSTEM_NAMES.get(self.code[0], "неизвестно")

    def __str__(self) -> str:
        return f"{self.code}  {self.description}"


@functools.lru_cache(maxsize=1)
def _load_descriptions() -> Dict[str, str]:
    """Загрузить базу описаний: общие коды плюс уточнения Toyota."""
    descriptions: Dict[str, str] = {}
    for name in ("dtc_generic.json", "dtc_toyota.json"):
        path = DATA_DIR / name
        if path.exists():
            # Toyota-файл читается вторым и намеренно перекрывает общие
            # описания там, где у марки своя трактовка кода.
            descriptions.update(json.loads(path.read_text(encoding="utf-8")))
    return descriptions


def decode_dtc(high: int, low: int) -> Optional[str]:
    """Превратить два байта с шины в код вида ``P0301``.

    Пара ``00 00`` означает пустую ячейку и даёт ``None``.
    """
    if high == 0 and low == 0:
        return None
    letter = SYSTEM_LETTERS[(high >> 6) & 0b11]
    first_digit = (high >> 4) & 0b11
    return f"{letter}{first_digit}{high & 0x0F:X}{low:02X}"


def encode_dtc(code: str) -> tuple[int, int]:
    """Обратное преобразование: ``P0301`` -> ``(0x03, 0x01)``."""
    code = code.strip().upper()
    if not DTC_PATTERN.match(code):
        raise ValueError(f"{code!r} не похож на код неисправности (ожидается вида P0301)")
    high = (LETTER_TO_BITS[code[0]] << 6) | (int(code[1]) << 4) | int(code[2], 16)
    return high, int(code[3:5], 16)


def describe(code: str) -> tuple[str, bool]:
    """Вернуть описание кода и признак того, что оно найдено в базе.

    Для незнакомых кодов собирается описание по подсистеме, чтобы отчёт
    оставался осмысленным, а не показывал голый номер.
    """
    code = code.upper()
    known = _load_descriptions().get(code)
    if known:
        return known, True

    system = SYSTEM_NAMES.get(code[0], "неизвестная система")
    try:
        number = int(code[1:], 16)
    except ValueError:
        return f"{system}: описание отсутствует в базе", False

    subsystem = next(
        (name for low, high, name in _SUBSYSTEM_RANGES if low <= number <= high),
        "неопределённая подсистема",
    )
    manufacturer = " (код производителя)" if code[1] in "12" else ""
    return f"{system}, {subsystem}{manufacturer} — описания в базе нет", False


def make_dtc(code: str, status: str = DtcStatus.STORED, ecu: str = "") -> Dtc:
    """Собрать :class:`Dtc` с подтянутым из базы описанием."""
    description, known = describe(code)
    return Dtc(code=code, description=description, status=status, ecu=ecu, known=known)


def parse_dtc_bytes(data: bytes, status: str = DtcStatus.STORED, ecu: str = "") -> list[Dtc]:
    """Разобрать поток пар байт из ответа режима 03/07/0A в список кодов."""
    codes = []
    for index in range(0, len(data) - 1, 2):
        code = decode_dtc(data[index], data[index + 1])
        if code:
            codes.append(make_dtc(code, status=status, ecu=ecu))
    return codes
