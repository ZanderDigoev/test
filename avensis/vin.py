"""Разбор идентификационного номера автомобиля (VIN).

Расшифровывается только то, что задано стандартом ISO 3779 и общедоступными
таблицами: изготовитель, страна, завод, серийный номер и контрольный разряд.
Средняя часть номера (позиции 4-8) кодируется каждым производителем по
собственной схеме, публично не описанной, поэтому модель определяется по
семейству кузова и помечается как предположение -- точный ответ даёт только
чтение калибровок из блока управления.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: Буквы I, O и Q в VIN не используются, чтобы не путать их с 1 и 0.
FORBIDDEN_LETTERS = set("IOQ")
VIN_LENGTH = 17

#: Вес каждой позиции при расчёте контрольного разряда (позиция 9 -- сам разряд).
CHECK_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

#: Числовые значения букв для контрольного разряда.
CHECK_VALUES = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}

#: Идентификаторы изготовителя, относящиеся к Toyota.
WMI_TABLE: Dict[str, str] = {
    "SB1": "Toyota Motor Manufacturing UK (Бёрнастон, Великобритания)",
    "VNK": "Toyota Motor Manufacturing France",
    "JTD": "Toyota Motor Corporation (Япония)",
    "JTE": "Toyota Motor Corporation (Япония)",
    "JTM": "Toyota Motor Corporation (Япония)",
    "JTN": "Toyota Motor Corporation (Япония)",
    "NMT": "Toyota Motor Manufacturing Turkey",
    "4T1": "Toyota Motor Manufacturing (США)",
    "5TD": "Toyota Motor Manufacturing Indiana (США)",
    "TW1": "Toyota Caetano Portugal",
}

#: Первая буква VIN задаёт географическую зону сборки.
REGION_BY_FIRST_CHAR = {
    "J": "Япония", "K": "Республика Корея", "L": "Китай",
    "S": "Великобритания", "T": "Швейцария / Чехия", "U": "Испания / Румыния",
    "V": "Франция / Испания", "W": "Германия", "X": "Россия",
    "Y": "Швеция / Финляндия", "Z": "Италия",
    "1": "США", "2": "Канада", "3": "Мексика", "4": "США", "5": "США",
    "9": "Бразилия",
}

#: Коды заводов Toyota, встречающиеся в 11-й позиции европейских VIN.
PLANT_CODES = {
    "E": "Бёрнастон, Великобритания (TMUK)",
    "D": "Валансьен, Франция (TMMF)",
    "W": "Колин, Чехия (TPCA)",
    "0": "Такаока, Япония",
    "2": "Цуцуми, Япония",
    "4": "Мотомати, Япония",
}

#: Семейства кузовов Avensis по годам выпуска -- для сопоставления с VIN.
AVENSIS_GENERATIONS = [
    ("T22", 1997, 2003, "Avensis первого поколения"),
    ("T25", 2003, 2009, "Avensis второго поколения"),
    ("T27", 2009, 2018, "Avensis третьего поколения"),
]


@dataclass
class VinInfo:
    """Результат разбора VIN."""

    vin: str
    valid_format: bool
    wmi: str
    manufacturer: str
    region: str
    vds: str
    check_digit: str
    check_digit_expected: str
    check_digit_ok: bool
    plant_code: str
    plant: str
    serial: str
    assumptions: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def is_toyota(self) -> bool:
        return self.wmi in WMI_TABLE


def compute_check_digit(vin: str) -> Optional[str]:
    """Рассчитать контрольный разряд (9-я позиция) по ISO 3779.

    Возвращает ``None``, если в номере есть символ вне алфавита VIN.
    """
    if len(vin) != VIN_LENGTH:
        return None
    total = 0
    for char, weight in zip(vin.upper(), CHECK_WEIGHTS):
        value = CHECK_VALUES.get(char)
        if value is None:
            return None
        total += value * weight
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def parse_vin(vin: str) -> VinInfo:
    """Разобрать VIN на составляющие и отметить, что достоверно, а что нет."""
    vin = vin.strip().upper().replace(" ", "").replace("-", "")
    problems: List[str] = []
    assumptions: List[str] = []

    if len(vin) != VIN_LENGTH:
        problems.append(f"Длина {len(vin)} символов вместо {VIN_LENGTH}")
    bad_letters = sorted(set(vin) & FORBIDDEN_LETTERS)
    if bad_letters:
        problems.append(f"Недопустимые для VIN символы: {', '.join(bad_letters)}")

    padded = vin.ljust(VIN_LENGTH)
    wmi = padded[0:3]
    expected = compute_check_digit(vin) or "?"
    actual = padded[8]

    # В Европе контрольный разряд обязательным не является, поэтому его
    # несовпадение -- повод присмотреться, но ещё не приговор.
    check_ok = expected == actual

    plant_code = padded[10]
    manufacturer = WMI_TABLE.get(wmi, "изготовитель не найден в справочнике")
    if wmi not in WMI_TABLE:
        assumptions.append("WMI отсутствует в справочнике программы")

    info = VinInfo(
        vin=vin,
        valid_format=not problems,
        wmi=wmi,
        manufacturer=manufacturer,
        region=REGION_BY_FIRST_CHAR.get(padded[0], "регион не определён"),
        vds=padded[3:8],
        check_digit=actual,
        check_digit_expected=expected,
        check_digit_ok=check_ok,
        plant_code=plant_code,
        plant=PLANT_CODES.get(plant_code, "завод не найден в справочнике"),
        serial=padded[11:17].strip(),
        assumptions=assumptions,
        problems=problems,
    )
    return info


def describe_vin(vin: str) -> List[tuple[str, str]]:
    """Готовые пары «поле -- значение» для вывода в отчёте."""
    info = parse_vin(vin)
    rows = [
        ("VIN", info.vin),
        ("Изготовитель (WMI)", f"{info.wmi} — {info.manufacturer}"),
        ("Регион сборки", info.region),
        ("Описание модели (VDS)", f"{info.vds} — схема кодирования Toyota не публикуется"),
        (
            "Контрольный разряд",
            f"{info.check_digit} — "
            + ("совпадает с расчётным" if info.check_digit_ok
               else f"расчётный {info.check_digit_expected}; для европейских Toyota разряд необязателен"),
        ),
        ("Завод", f"{info.plant_code} — {info.plant}"),
        ("Серийный номер кузова", info.serial),
    ]
    if info.problems:
        rows.append(("Замечания к номеру", "; ".join(info.problems)))
    return rows
