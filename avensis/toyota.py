"""Специфика Toyota: адреса блоков управления и реестр настраиваемых функций.

Важное предупреждение по адресам. Единого публичного стандарта на адресацию
блоков у Toyota нет: набор ЭБУ и их идентификаторы меняются от кузова, года и
рынка. Поэтому таблица ниже -- не истина, а *список кандидатов для опроса*.
Программа не верит ей на слово, а честно опрашивает каждый адрес и показывает
только те блоки, которые реально ответили.
"""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class EcuCandidate:
    """Адрес, по которому имеет смысл поискать блок управления."""

    name: str
    request: str
    """CAN-идентификатор запроса (физическая адресация, ``ATSH``)."""

    response: str
    """Ожидаемый идентификатор ответа."""

    note: str = ""


#: Кандидаты для опроса по CAN, 11 бит. Пары адресов у Toyota идут со
#: смещением +8: запрос 0x7E0 -- ответ 0x7E8.
CAN_ECU_CANDIDATES: List[EcuCandidate] = [
    EcuCandidate("Двигатель (ECM)", "7E0", "7E8", "отвечает и по стандартному OBD-II"),
    EcuCandidate("АКПП / ECT", "7E1", "7E9"),
    EcuCandidate("Дополнительный блок силового агрегата", "7E2", "7EA"),
    EcuCandidate("Электроусилитель руля (EPS)", "730", "738"),
    EcuCandidate("Кузовная электроника (Body ECU)", "740", "748"),
    EcuCandidate("Шлюз / центральный блок", "750", "758"),
    EcuCandidate("Подушки безопасности (SRS)", "780", "788"),
    EcuCandidate("ABS / VSC (Skid Control)", "7B0", "7B8"),
    EcuCandidate("Кондиционер / климат", "7C4", "7CC"),
    EcuCandidate("Комбинация приборов", "7C0", "7C8"),
]

#: Известные адреса ответов -- для подписи блоков в отчёте.
ECU_DIRECTORY: Dict[str, str] = {
    "7E8": "Двигатель (ECM)",
    "7E9": "АКПП / ECT",
    "7EA": "Дополнительный блок силового агрегата",
    "7EB": "Дополнительный блок силового агрегата",
    "738": "Электроусилитель руля (EPS)",
    "748": "Кузовная электроника (Body ECU)",
    "758": "Шлюз / центральный блок",
    "788": "Подушки безопасности (SRS)",
    "7B8": "ABS / VSC (Skid Control)",
    "7C8": "Комбинация приборов",
    "7CC": "Кондиционер / климат",
    # K-line: адрес источника в заголовке ISO 9141-2 / ISO 14230-4.
    "10": "Двигатель (ECM)",
    "18": "АКПП / ECT",
    "28": "ABS",
    "40": "Кузовная электроника (Body ECU)",
    "58": "Подушки безопасности (SRS)",
}


def describe_ecu(address: str) -> str:
    """Подписать адрес блока человеческим названием, если оно известно."""
    return ECU_DIRECTORY.get(address.upper(), "")


# ------------------------------------------------------------------ настройки


@dataclass(frozen=True)
class Setting:
    """Одна настраиваемая функция автомобиля.

    Описывает, где в памяти блока лежит параметр и какие значения допустимы.
    Поле :attr:`verified` намеренно есть у каждой записи: настройки Toyota
    не описаны публично, и путать проверенное с предполагаемым нельзя.
    """

    key: str
    title: str
    ecu: str
    request_id: str
    response_id: str
    did: str
    """Идентификатор данных (Data Identifier) для сервисов UDS 0x22/0x2E."""

    byte: int
    """Номер байта внутри блока данных, считая от нуля."""

    mask: int
    """Битовая маска изменяемого поля внутри байта."""

    values: Dict[str, int]
    """Соответствие «человеческое название -> значение поля» (уже сдвинутое)."""

    session: str = "03"
    """Диагностическая сессия, в которой блок принимает запись."""

    verified: bool = False
    requires_security: bool = False
    note: str = ""

    @property
    def shift(self) -> int:
        """На сколько бит сдвинуто поле внутри байта."""
        if self.mask == 0:
            return 0
        return (self.mask & -self.mask).bit_length() - 1

    def extract(self, data: bytes) -> Optional[int]:
        """Достать текущее значение поля из прочитанного блока данных."""
        if self.byte >= len(data):
            return None
        return (data[self.byte] & self.mask) >> self.shift

    def apply(self, data: bytes, value: int) -> bytes:
        """Вернуть копию блока данных с изменённым полем.

        Остальные биты сохраняются без изменений: настройки в одном байте
        соседствуют, и запись «начисто» сломала бы соседние функции.
        """
        if self.byte >= len(data):
            raise ValueError(
                f"Блок данных короче ожидаемого: {len(data)} байт, "
                f"а настройка живёт в байте {self.byte}"
            )
        if (value << self.shift) & ~self.mask & 0xFF:
            raise ValueError(f"Значение {value} не помещается в маску 0x{self.mask:02X}")
        patched = bytearray(data)
        patched[self.byte] = (patched[self.byte] & ~self.mask & 0xFF) | ((value << self.shift) & self.mask)
        return bytes(patched)

    def name_of(self, value: int) -> str:
        """Человеческое название для числового значения поля."""
        for name, candidate in self.values.items():
            if candidate == value:
                return name
        return f"неизвестное значение {value}"


@functools.lru_cache(maxsize=1)
def load_settings() -> Dict[str, Setting]:
    """Загрузить реестр настраиваемых функций из ``data/settings.json``."""
    path = DATA_DIR / "settings.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    registry = {}
    for entry in raw["settings"]:
        setting = Setting(
            key=entry["key"],
            title=entry["title"],
            ecu=entry["ecu"],
            request_id=entry["request_id"],
            response_id=entry["response_id"],
            did=entry["did"],
            byte=entry["byte"],
            mask=int(entry["mask"], 16) if isinstance(entry["mask"], str) else entry["mask"],
            values=entry["values"],
            session=entry.get("session", "03"),
            verified=entry.get("verified", False),
            requires_security=entry.get("requires_security", False),
            note=entry.get("note", ""),
        )
        registry[setting.key] = setting
    return registry


def get_setting(key: str) -> Optional[Setting]:
    return load_settings().get(key)
