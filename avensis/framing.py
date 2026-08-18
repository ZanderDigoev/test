"""Разбор сырых строк ELM327 в сообщения ЭБУ.

Адаптер печатает ответы по-разному в зависимости от протокола на шине,
и один функциональный запрос может собрать ответы сразу от нескольких блоков.
Здесь это приводится к общему виду: список :class:`EcuMessage`.

Для CAN (ISO 15765-4) выполняется сборка ISO-TP: одиночные кадры, а также
first/consecutive frame склеиваются в одно сообщение на каждый адрес.
Для K-line (ISO 9141-2, ISO 14230-4) снимается трёхбайтовый заголовок и
контрольная сумма, каждая строка -- отдельное сообщение.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List

#: Номера протоколов ELM327, работающие поверх CAN.
CAN_PROTOCOLS = frozenset({6, 7, 8, 9, 10, 11, 12})
#: Из них -- использующие 29-битные идентификаторы (заголовок 4 байта).
EXTENDED_CAN_PROTOCOLS = frozenset({7, 9, 10})

#: Служебные ответы адаптера, которые не являются данными с шины.
ADAPTER_NOTICES = (
    "SEARCHING",
    "BUS INIT",
    "BUS INIT:",
    "OK",
)

#: Сообщения адаптера, означающие неудачу запроса.
ADAPTER_ERRORS = (
    "NO DATA",
    "UNABLE TO CONNECT",
    "BUS ERROR",
    "BUS BUSY",
    "CAN ERROR",
    "DATA ERROR",
    "FB ERROR",
    "LV RESET",
    "BUFFER FULL",
    "RX ERROR",
    "STOPPED",
    "ACT ALERT",
    "?",
)

_HEX_LINE = re.compile(r"^[0-9A-F\s]+$")


class FramingError(ValueError):
    """Строку с шины не удалось разобрать."""


class AdapterError(RuntimeError):
    """Адаптер вернул сообщение об ошибке вместо данных."""


class NoDataError(AdapterError):
    """Ни один ЭБУ не ответил на запрос."""


@dataclass
class EcuMessage:
    """Одно собранное сообщение от конкретного блока управления."""

    ecu: str
    """Адрес отвечающего ЭБУ в hex, как его напечатал адаптер (``7E8``, ``18DAF110``, ``10``)."""

    data: bytes
    """Полезная нагрузка: SID ответа и далее, без PCI/заголовка/контрольной суммы."""

    def __repr__(self) -> str:  # pragma: no cover - для отладки
        return f"EcuMessage({self.ecu}, {self.data.hex().upper()})"


@dataclass
class _CanAssembly:
    """Состояние сборки многокадрового ISO-TP сообщения от одного ЭБУ."""

    expected: int = 0
    buffer: bytearray = field(default_factory=bytearray)
    complete: bool = False


def clean_lines(raw: str) -> List[str]:
    """Разбить сырой ответ адаптера на непустые строки без приглашения ``>``."""
    text = raw.replace("\r", "\n").replace(">", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def is_hex_string(line: str) -> bool:
    """Состоит ли строка только из hex-символов и пробелов."""
    stripped = line.replace(" ", "")
    return bool(stripped) and bool(_HEX_LINE.match(line.upper()))


def is_hex_payload(line: str) -> bool:
    """Похожа ли строка на целое число hex-байт.

    Применимо к K-line, где заголовок занимает целые байты. У 11-битного CAN
    заголовок -- 3 ниббла, поэтому там длина строки нечётная и эта проверка
    не годится: см. :func:`is_hex_string`.
    """
    stripped = line.replace(" ", "")
    if len(stripped) % 2:
        return False
    return is_hex_string(line)


def check_adapter_errors(lines: List[str]) -> None:
    """Бросить исключение, если среди строк есть сообщение об ошибке адаптера."""
    for line in lines:
        upper = line.upper().strip()
        if upper.startswith("NO DATA"):
            raise NoDataError("NO DATA -- ЭБУ не ответил на запрос")
        for err in ADAPTER_ERRORS:
            if upper.startswith(err):
                raise AdapterError(f"Адаптер вернул: {line}")


def _strip_line_index(payload: str) -> str:
    """Убрать порядковый номер строки, который ELM печатает при ATH0.

    Длинные ответы без заголовков адаптер нумерует как ``0:``, ``1:`` и т.д.
    """
    if len(payload) > 1 and payload[1] == ":":
        return payload[2:]
    return payload


def _split_can_header(payload: str, extended: bool) -> tuple[str, bytes]:
    header_len = 8 if extended else 3
    if len(payload) <= header_len:
        raise FramingError(f"Строка {payload!r} короче CAN-заголовка")
    header = payload[:header_len]
    body_hex = payload[header_len:]
    if len(body_hex) % 2:
        # 11-битный заголовок занимает 3 нибла, поэтому тело всегда чётное.
        # Нечёт означает обрезанную строку -- отбрасываем хвостовой ниббл.
        body_hex = body_hex[:-1]
    try:
        return header, bytes.fromhex(body_hex)
    except ValueError as exc:
        raise FramingError(f"Не hex в строке {payload!r}") from exc


def parse_can(lines: List[str], extended: bool) -> List[EcuMessage]:
    """Собрать ISO-TP сообщения из строк CAN-ответа."""
    assemblies: Dict[str, _CanAssembly] = {}
    order: List[str] = []

    for line in lines:
        payload = _strip_line_index(line.replace(" ", "").upper())
        if not is_hex_string(payload):
            continue
        try:
            ecu, body = _split_can_header(payload, extended)
        except FramingError:
            continue
        if not body:
            continue

        if ecu not in assemblies:
            assemblies[ecu] = _CanAssembly()
            order.append(ecu)
        asm = assemblies[ecu]

        pci_type = body[0] >> 4
        if pci_type == 0x0:  # Single Frame
            length = body[0] & 0x0F
            asm.buffer = bytearray(body[1 : 1 + length])
            asm.expected = length
            asm.complete = True
        elif pci_type == 0x1:  # First Frame
            if len(body) < 2:
                continue
            asm.expected = ((body[0] & 0x0F) << 8) | body[1]
            asm.buffer = bytearray(body[2:])
            asm.complete = False
        elif pci_type == 0x2:  # Consecutive Frame
            asm.buffer.extend(body[1:])
        elif pci_type == 0x3:  # Flow Control -- адаптер шлёт его сам, нам не нужен
            continue
        else:
            continue

        if asm.expected and len(asm.buffer) >= asm.expected:
            asm.complete = True

    messages = []
    for ecu in order:
        asm = assemblies[ecu]
        if not asm.buffer:
            continue
        data = bytes(asm.buffer[: asm.expected]) if asm.expected else bytes(asm.buffer)
        messages.append(EcuMessage(ecu=ecu, data=data))
    return messages


def parse_kline(lines: List[str]) -> List[EcuMessage]:
    """Разобрать строки ISO 9141-2 / ISO 14230-4.

    Формат строки: ``FMT TGT SRC <данные...> CS``. Контрольная сумма --
    8-битная сумма предыдущих байт; если сходится, байт отбрасывается.
    """
    messages = []
    for line in lines:
        payload = line.replace(" ", "").upper()
        if not is_hex_payload(payload):
            continue
        try:
            raw = bytes.fromhex(payload)
        except ValueError:
            continue
        if len(raw) < 4:
            continue

        source = f"{raw[2]:02X}"
        body = raw[3:]

        # Снимаем контрольную сумму, если она сходится либо если длина
        # совпадает с той, что заявлена в байте формата.
        declared = raw[0] & 0x3F
        checksum_ok = (sum(raw[:-1]) & 0xFF) == raw[-1]
        length_ok = declared > 0 and len(raw) == 3 + declared + 1
        if len(body) > 1 and (checksum_ok or length_ok):
            body = body[:-1]

        if body:
            messages.append(EcuMessage(ecu=source, data=body))
    return messages


def parse_response(raw: str, protocol: int) -> List[EcuMessage]:
    """Разобрать сырой ответ адаптера с учётом активного протокола.

    :param raw: всё, что адаптер напечатал до приглашения ``>``.
    :param protocol: номер протокола ELM327 (``ATDPN``).
    :raises NoDataError: если ЭБУ промолчал.
    :raises AdapterError: при иной ошибке адаптера.
    """
    lines = clean_lines(raw)
    # Эхо команды и служебные пометки данными не являются.
    lines = [ln for ln in lines if not any(ln.upper().startswith(n) for n in ADAPTER_NOTICES)]
    check_adapter_errors(lines)

    if protocol in CAN_PROTOCOLS:
        messages = parse_can(lines, extended=protocol in EXTENDED_CAN_PROTOCOLS)
    else:
        messages = parse_kline(lines)

    if not messages:
        raise NoDataError("Ответ с шины не получен или не распознан")
    return messages
