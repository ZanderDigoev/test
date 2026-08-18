"""Виртуальный ELM327 с виртуальной Toyota Avensis на шине.

Нужен для двух вещей: прогонять программу без доступа к машине и
воспроизводить сценарии, которые на живом автомобиле не создашь по заказу
(например, конкретный набор ошибок).

Симулятор говорит на том же уровне, что и настоящий адаптер: принимает
строку команды, оканчивающуюся возвратом каретки, и отвечает строками
с приглашением ``>`` в конце.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from avensis.dtc import encode_dtc

PROMPT = "\r\r>"


def _pad(data: bytes, size: int = 8) -> bytes:
    """Добить кадр до восьми байт, как это делает реальная шина CAN."""
    return data + bytes(size - len(data)) if len(data) < size else data


@dataclass
class VirtualEcu:
    """Блок управления на виртуальной шине."""

    request_id: str
    response_id: str
    name: str
    obd_capable: bool = False
    supported_pids: Dict[int, bytes] = field(default_factory=dict)
    live: Dict[int, bytes] = field(default_factory=dict)
    stored_dtcs: List[str] = field(default_factory=list)
    pending_dtcs: List[str] = field(default_factory=list)
    permanent_dtcs: List[str] = field(default_factory=list)
    freeze_frame: Dict[int, bytes] = field(default_factory=dict)
    freeze_trigger: Optional[str] = None
    dids: Dict[str, bytearray] = field(default_factory=dict)
    ecu_name: str = ""
    calibration_id: str = ""


def _avensis_t25(protocol: int) -> tuple[str, List[VirtualEcu]]:
    """Собрать конфигурацию, похожую на Avensis T25 с парой типовых неисправностей."""
    engine = VirtualEcu(
        request_id="7E0",
        response_id="7E8",
        name="Двигатель (ECM)",
        obd_capable=True,
        ecu_name="ECM-EngineControl",
        calibration_id="89663-05270",
        supported_pids={
            0x00: bytes.fromhex("BE3FA813"),
            0x20: bytes.fromhex("A005B011"),
            0x40: bytes.fromhex("FED00400"),
            0x60: bytes.fromhex("00000000"),
        },
        live={
            0x01: bytes.fromhex("83070505"),  # Check Engine горит, 3 кода
            0x03: bytes.fromhex("0200"),
            0x04: bytes.fromhex("45"),
            0x05: bytes.fromhex("5A"),
            0x06: bytes.fromhex("8C"),
            0x07: bytes.fromhex("94"),
            0x0B: bytes.fromhex("22"),
            0x0C: bytes.fromhex("0BB8"),
            0x0D: bytes.fromhex("00"),
            0x0E: bytes.fromhex("8C"),
            0x0F: bytes.fromhex("39"),
            0x10: bytes.fromhex("0145"),
            0x11: bytes.fromhex("21"),
            0x1C: bytes.fromhex("06"),
            0x1F: bytes.fromhex("0384"),
            0x21: bytes.fromhex("0158"),
            0x2F: bytes.fromhex("6E"),
            0x31: bytes.fromhex("02A6"),
            0x33: bytes.fromhex("65"),
            0x42: bytes.fromhex("36B0"),
            0x46: bytes.fromhex("3B"),
            0x51: bytes.fromhex("01"),
            0x5C: bytes.fromhex("55"),
        },
        stored_dtcs=["P0301", "P1349", "P0420"],
        pending_dtcs=["P0171"],
        permanent_dtcs=["P0420"],
        freeze_trigger="P0301",
        freeze_frame={
            0x04: bytes.fromhex("62"),
            0x05: bytes.fromhex("58"),
            0x0C: bytes.fromhex("1194"),
            0x0D: bytes.fromhex("32"),
            0x11: bytes.fromhex("40"),
        },
    )

    transmission = VirtualEcu(
        request_id="7E1",
        response_id="7E9",
        name="АКПП / ECT",
        obd_capable=True,
        ecu_name="ECT-Transmission",
        supported_pids={0x00: bytes.fromhex("18000000")},
        live={0x01: bytes.fromhex("00000000"), 0x04: bytes.fromhex("30"), 0x05: bytes.fromhex("58")},
    )

    abs_ecu = VirtualEcu(
        request_id="7B0",
        response_id="7B8",
        name="ABS / VSC (Skid Control)",
        dids={
            "F190": bytearray(b"SB1BJ56L20E095222"),
            "F189": bytearray(b"1.07"),
            "F18C": bytearray(b"SKC0094412"),
        },
    )
    # Ошибка калибровки нуля датчика ускорения -- классика для этого кузова.
    abs_ecu.stored_dtcs = ["C1336"]

    body = VirtualEcu(
        request_id="740",
        response_id="748",
        name="Кузовная электроника (Body ECU)",
        dids={
            "F190": bytearray(b"SB1BJ56L20E095222"),
            "F187": bytearray(b"89221-05010"),
            "F189": bytearray(b"2.11"),
            # Ячейки с настраиваемыми функциями. На реальной машине их адреса
            # заранее неизвестны -- их и ищет команда settings discover.
            "0100": bytearray(bytes.fromhex("01050200")),
            "0101": bytearray(bytes.fromhex("00000000")),
            "0102": bytearray(bytes.fromhex("0A0B0C0D")),
        },
    )

    meter = VirtualEcu(
        request_id="7C0",
        response_id="7C8",
        name="Комбинация приборов",
        dids={"F190": bytearray(b"SB1BJ56L20E095222"), "0100": bytearray(bytes.fromhex("0100"))},
    )

    return "SB1BJ56L20E095222", [engine, transmission, abs_ecu, body, meter]


PROFILES = {
    "avensis_t25": 6,       # CAN 11 бит, 500 кбит/с
    "avensis_t25_kline": 3,  # ISO 9141-2, как на ранних машинах этого кузова
}


class ElmSimulator:
    """Эмулятор адаптера ELM327 вместе с автомобилем на другом конце провода."""

    def __init__(self, profile: str = "avensis_t25"):
        if profile not in PROFILES:
            raise ValueError(f"Неизвестный профиль {profile!r}. Доступны: {', '.join(PROFILES)}")
        self.profile = profile
        self.protocol = PROFILES[profile]
        self.vin, self.ecus = _avensis_t25(self.protocol)

        self.echo = True
        self.headers = False
        self.spaces = True
        self.header_override: Optional[str] = None
        self.rx_filter: Optional[str] = None
        self.session: Dict[str, int] = {}
        self._buffer = ""
        self._last_command = ""

    # ------------------------------------------------------------- ввод/вывод

    def feed(self, data: bytes) -> bytes:
        """Принять байты от программы и вернуть байты ответа."""
        self._buffer += data.decode("ascii", errors="ignore")
        output = ""
        while "\r" in self._buffer:
            line, self._buffer = self._buffer.split("\r", 1)
            output += self._handle(line.strip())
        return output.encode("ascii")

    def _handle(self, command: str) -> str:
        command = command.upper().replace(" ", "")
        if not command:
            # Пустая строка: ELM327 повторяет предыдущую команду.
            command = self._last_command
            if not command:
                return PROMPT
        else:
            self._last_command = command

        echo = f"{command}\r" if self.echo else ""

        if command.startswith("AT"):
            return echo + self._handle_at(command[2:]) + PROMPT
        return echo + self._handle_obd(command) + PROMPT

    # --------------------------------------------------------- AT-команды

    def _handle_at(self, command: str) -> str:
        if command == "Z":
            self.echo, self.headers, self.spaces = True, False, True
            self.header_override = self.rx_filter = None
            self.session.clear()
            return "\rELM327 v1.5\r"
        if command == "I":
            return "ELM327 v1.5\r"
        if command == "RV":
            return "14.1V\r"
        if command == "DPN":
            return f"{self.protocol:X}\r"
        if command == "DP":
            from avensis.elm327 import PROTOCOL_NAMES  # noqa: PLC0415

            return PROTOCOL_NAMES.get(self.protocol, "UNKNOWN") + "\r"
        if command.startswith("E"):
            self.echo = command.endswith("1")
            return "OK\r"
        if command.startswith("H"):
            self.headers = command.endswith("1")
            return "OK\r"
        if command.startswith("S") and command[1:] in ("0", "1"):
            self.spaces = command.endswith("1")
            return "OK\r"
        if command.startswith("SH"):
            self.header_override = command[2:]
            return "OK\r"
        if command.startswith("CRA"):
            self.rx_filter = command[3:] or None
            return "OK\r"
        if command.startswith("SP"):
            self.header_override = self.rx_filter = None
            return "OK\r"
        # Тайминги, управление потоком, длина строк -- принимаем молча.
        if command[:1] in ("L", "A", "T", "M", "C", "F", "P", "R", "W", "D", "B"):
            return "OK\r"
        return "?\r"

    # ------------------------------------------------------ запросы на шину

    def _targets(self) -> List[VirtualEcu]:
        """Кому адресован запрос при текущей настройке заголовков."""
        if self.header_override:
            matched = [e for e in self.ecus if e.request_id == self.header_override]
            return matched
        # Без явного заголовка запрос широковещательный, отвечают блоки OBD-II.
        return [e for e in self.ecus if e.obd_capable]

    def _handle_obd(self, command: str) -> str:
        # Последняя цифра может быть подсказкой «жду N ответов» -- она не часть данных.
        if len(command) % 2 == 1:
            command = command[:-1]
        try:
            request = bytes.fromhex(command)
        except ValueError:
            return "?\r"
        if not request:
            return "?\r"

        lines: List[str] = []
        for ecu in self._targets():
            if self.rx_filter and ecu.response_id != self.rx_filter:
                continue
            for response in self._respond(ecu, request) or []:
                lines.extend(self._frame(ecu.response_id, response))

        return "".join(f"{line}\r" for line in lines) if lines else "NO DATA\r"

    def _respond(self, ecu: VirtualEcu, request: bytes) -> Optional[List[bytes]]:
        """Вернуть сообщения, которыми блок отвечает на запрос.

        Список, а не одно значение: по K-line длинные ответы режима 09
        разбиваются на несколько самостоятельных сообщений шины.
        """
        payload = self._build_response(ecu, request)
        if payload is None:
            return None
        return payload if isinstance(payload, list) else [payload]

    def _build_response(self, ecu: VirtualEcu, request: bytes):
        mode = request[0]

        if mode == 0x01 and ecu.obd_capable and len(request) >= 2:
            pid = request[1]
            table = ecu.supported_pids if pid in ecu.supported_pids else ecu.live
            value = table.get(pid)
            return bytes([0x41, pid]) + value if value else None

        if mode == 0x02 and ecu.obd_capable and len(request) >= 2:
            pid, frame = request[1], request[2] if len(request) > 2 else 0
            if frame != 0 or not ecu.freeze_trigger:
                return None
            if pid == 0x02:
                high, low = encode_dtc(ecu.freeze_trigger)
                return bytes([0x42, 0x02, frame, high, low])
            value = ecu.freeze_frame.get(pid)
            return bytes([0x42, pid, frame]) + value if value else None

        if mode in (0x03, 0x07, 0x0A) and ecu.obd_capable:
            source = {
                0x03: ecu.stored_dtcs, 0x07: ecu.pending_dtcs, 0x0A: ecu.permanent_dtcs
            }[mode]
            payload = b"".join(bytes(encode_dtc(code)) for code in source)
            if self.protocol in (6, 7, 8, 9):
                return bytes([mode + 0x40, len(source)]) + payload
            return bytes([mode + 0x40]) + payload

        if mode == 0x04 and ecu.obd_capable:
            ecu.stored_dtcs.clear()
            ecu.pending_dtcs.clear()
            ecu.freeze_trigger = None
            ecu.freeze_frame.clear()
            ecu.live[0x01] = bytes.fromhex("00074545")  # Check Engine погас, мониторы сброшены
            return b"\x44"

        if mode == 0x09 and ecu.obd_capable and len(request) >= 2:
            return self._vehicle_info(ecu, request[1])

        if mode in (0x10, 0x11, 0x14, 0x19, 0x22, 0x27, 0x2E, 0x3E):
            return self._uds(ecu, request)

        return None

    def _vehicle_info(self, ecu: VirtualEcu, info_type: int):
        if info_type == 0x00:
            return bytes([0x49, 0x00]) + bytes.fromhex("55000000")

        if info_type == 0x02 and ecu.response_id in ("7E8", "10"):
            # VIN дополняется слева до кратности четырём: так он ложится ровно
            # в пять сообщений K-line по четыре байта данных в каждом.
            payload = self.vin.encode().rjust(20, b"\x00")
        elif info_type == 0x04 and ecu.calibration_id:
            payload = ecu.calibration_id.encode().ljust(16, b"\x00")
        elif info_type == 0x0A and ecu.ecu_name:
            payload = ecu.ecu_name.encode().ljust(20, b"\x00")
        else:
            return None

        if self.protocol in (6, 7, 8, 9, 10, 11, 12):
            # По CAN всё уезжает одним многокадровым сообщением ISO-TP.
            return bytes([0x49, info_type, len(payload) // 4]) + payload

        # По K-line -- отдельными сообщениями с порядковым номером в каждом.
        return [
            bytes([0x49, info_type, index + 1]) + payload[offset : offset + 4]
            for index, offset in enumerate(range(0, len(payload), 4))
        ]

    def _uds(self, ecu: VirtualEcu, request: bytes) -> Optional[bytes]:
        # UDS работает только при физической адресации конкретного блока.
        if not self.header_override or self.header_override != ecu.request_id:
            return None
        sid = request[0]

        if sid == 0x3E:
            return bytes([0x7E, 0x00])

        if sid == 0x10 and len(request) >= 2:
            self.session[ecu.request_id] = request[1]
            return bytes([0x50, request[1], 0x00, 0x32, 0x01, 0xF4])

        if sid == 0x11:
            self.session.pop(ecu.request_id, None)
            return bytes([0x51, request[1] if len(request) > 1 else 0x01])

        if sid == 0x19 and len(request) >= 3 and request[1] == 0x02:
            records = b""
            for code in ecu.stored_dtcs:
                high, low = encode_dtc(code)
                records += bytes([high, low, 0x00, 0x09])
            return bytes([0x59, 0x02, 0xFF]) + records

        if sid == 0x14:
            ecu.stored_dtcs.clear()
            return bytes([0x54])

        if sid == 0x22 and len(request) >= 3:
            did = request[1:3].hex().upper()
            stored = ecu.dids.get(did)
            if stored is None:
                return bytes([0x7F, 0x22, 0x31])  # нет такого идентификатора
            return bytes([0x62]) + request[1:3] + bytes(stored)

        if sid == 0x2E and len(request) >= 4:
            did = request[1:3].hex().upper()
            stored = ecu.dids.get(did)
            if stored is None:
                return bytes([0x7F, 0x2E, 0x31])
            if self.session.get(ecu.request_id, 0x01) == 0x01:
                return bytes([0x7F, 0x2E, 0x7F])  # запись только в расширенной сессии
            payload = request[3:]
            if len(payload) != len(stored):
                return bytes([0x7F, 0x2E, 0x13])  # неверная длина
            ecu.dids[did] = bytearray(payload)
            return bytes([0x6E]) + request[1:3]

        return bytes([0x7F, sid, 0x11])

    # ------------------------------------------------- формирование кадров

    def _frame(self, ecu_id: str, data: bytes) -> List[str]:
        """Разложить ответ на кадры так, как их напечатал бы реальный адаптер."""
        if self.protocol in (6, 7, 8, 9, 10, 11, 12):
            return self._frame_can(ecu_id, data)
        return self._frame_kline(ecu_id, data)

    def _frame_can(self, ecu_id: str, data: bytes) -> List[str]:
        header = ecu_id if self.headers else ""
        lines: List[str] = []

        if len(data) <= 7:
            frame = bytes([len(data)]) + data
            lines.append(header + _pad(frame).hex().upper())
        else:
            first = bytes([0x10 | (len(data) >> 8), len(data) & 0xFF]) + data[:6]
            lines.append(header + first.hex().upper())
            sequence = 1
            for offset in range(6, len(data), 7):
                chunk = data[offset : offset + 7]
                frame = bytes([0x20 | (sequence & 0x0F)]) + chunk
                lines.append(header + _pad(frame).hex().upper())
                sequence += 1

        if self.spaces:
            lines = [" ".join(line[i : i + 2] for i in range(0, len(line), 2)) for line in lines]
        return lines

    def _frame_kline(self, ecu_id: str, data: bytes) -> List[str]:
        """K-line: трёхбайтовый заголовок, до семи байт данных и контрольная сумма."""
        source = {"7E8": 0x10, "7E9": 0x18, "7B8": 0x28, "748": 0x40, "7C8": 0x58}.get(ecu_id, 0x10)
        lines = []
        for offset in range(0, len(data), 7):
            chunk = data[offset : offset + 7]
            raw = bytes([0x40 | len(chunk), 0x6B, source]) + chunk
            raw += bytes([sum(raw) & 0xFF])
            text = raw.hex().upper() if self.headers else chunk.hex().upper()
            if self.spaces:
                text = " ".join(text[i : i + 2] for i in range(0, len(text), 2))
            lines.append(text)
        return lines
