"""Сервисы OBD-II: чтение параметров, ошибок, стоп-кадра, VIN.

Слой поверх :class:`~avensis.elm327.Elm327`. Здесь учитывается главное
отличие шин: в ответе режима 03 по CAN первым идёт байт с числом кодов,
а по K-line его нет и коды приходят несколькими сообщениями по три.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from avensis import pids as pid_module
from avensis.dtc import Dtc, DtcStatus, parse_dtc_bytes
from avensis.elm327 import Elm327
from avensis.framing import EcuMessage

log = logging.getLogger(__name__)

MODE_LIVE = 0x01
MODE_FREEZE = 0x02
MODE_STORED_DTC = 0x03
MODE_CLEAR = 0x04
MODE_PENDING_DTC = 0x07
MODE_VEHICLE_INFO = 0x09
MODE_PERMANENT_DTC = 0x0A

NEGATIVE_RESPONSE = 0x7F

#: Расшифровка кодов отказа в отрицательном ответе.
NRC_MEANINGS = {
    0x10: "общий отказ",
    0x11: "сервис не поддерживается",
    0x12: "подфункция не поддерживается",
    0x13: "неверная длина запроса",
    0x22: "условия не выполнены (например, двигатель должен работать)",
    0x31: "запрос вне допустимого диапазона",
    0x33: "доступ запрещён (требуется авторизация)",
    0x78: "запрос принят, ответ будет позже",
}


class ObdError(RuntimeError):
    """Блок управления отказал в обслуживании запроса."""


@dataclass
class EcuInfo:
    """Обнаруженный на шине блок управления."""

    address: str
    name: str = ""
    supported_pids: List[int] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.address} — {self.name}" if self.name else self.address


@dataclass
class FreezeFrame:
    """Стоп-кадр: снимок параметров в момент фиксации ошибки."""

    ecu: str
    trigger_dtc: Optional[str]
    values: Dict[int, object] = field(default_factory=dict)


def _is_negative(message: EcuMessage) -> Optional[str]:
    """Если сообщение -- отрицательный ответ, вернуть его расшифровку."""
    if len(message.data) >= 3 and message.data[0] == NEGATIVE_RESPONSE:
        nrc = message.data[2]
        return NRC_MEANINGS.get(nrc, f"код отказа 0x{nrc:02X}")
    return None


def _matches(message: EcuMessage, mode: int, *prefix: int) -> bool:
    """Отвечает ли сообщение именно на наш запрос."""
    data = message.data
    expected = bytes([mode + 0x40, *prefix])
    return data.startswith(expected)


class ObdSession:
    """Диагностическая сессия по стандартным сервисам OBD-II."""

    def __init__(self, elm: Elm327):
        self.elm = elm

    # ------------------------------------------------------- режим 01: онлайн

    def read_pid(self, pid: int, ecu: Optional[str] = None) -> Dict[str, object]:
        """Прочитать параметр режима 01 у всех ответивших блоков.

        :return: словарь ``{адрес ЭБУ: пересчитанное значение}``.
        """
        messages = self.elm.try_query(f"01{pid:02X}", expected_responses=1 if ecu else None)
        result: Dict[str, object] = {}
        for message in messages:
            if ecu and message.ecu != ecu:
                continue
            if not _matches(message, MODE_LIVE, pid):
                continue
            value = pid_module.decode_value(pid, message.data[2:])
            if value is not None:
                result[message.ecu] = value
        return result

    def read_supported_pids(self, ecu: Optional[str] = None) -> Dict[str, List[int]]:
        """Опросить битовые маски поддержки и собрать список доступных PID'ов."""
        supported: Dict[str, List[int]] = {}
        for base in pid_module.SUPPORT_PIDS:
            messages = self.elm.try_query(f"01{base:02X}")
            if not messages:
                break
            any_continuation = False
            for message in messages:
                if ecu and message.ecu != ecu:
                    continue
                if not _matches(message, MODE_LIVE, base):
                    continue
                found = pid_module.decode_supported(base, message.data[2:])
                supported.setdefault(message.ecu, []).extend(found)
                # Следующая маска запрашивается, только если текущая объявила
                # её PID поддерживаемым -- иначе опрос уходит в пустоту.
                if (base + 0x20) in found:
                    any_continuation = True
            if not any_continuation:
                break
        return {ecu_addr: sorted(set(items)) for ecu_addr, items in supported.items()}

    def read_monitor_status(self) -> Dict[str, pid_module.MonitorStatus]:
        """Состояние Check Engine и готовность мониторов по каждому блоку."""
        messages = self.elm.try_query("0101")
        result = {}
        for message in messages:
            if not _matches(message, MODE_LIVE, 0x01):
                continue
            status = pid_module.decode_monitor_status(message.data[2:])
            if status is not None:
                result[message.ecu] = status
        return result

    def read_live_snapshot(self, pid_list: Sequence[int], ecu: Optional[str] = None) -> Dict[int, Dict[str, object]]:
        """Снять значения сразу нескольких параметров."""
        return {pid: self.read_pid(pid, ecu=ecu) for pid in pid_list}

    # ------------------------------------------------ режимы 03/07/0A: ошибки

    def _read_dtc_mode(self, mode: int, status: str) -> List[Dtc]:
        messages = self.elm.try_query(f"{mode:02X}")
        codes: List[Dtc] = []
        for message in messages:
            problem = _is_negative(message)
            if problem:
                log.debug("ЭБУ %s отказал в режиме %02X: %s", message.ecu, mode, problem)
                continue
            if not _matches(message, mode):
                continue
            payload = message.data[1:]
            # По CAN первым идёт счётчик кодов, по K-line его нет.
            if self.elm.is_can and payload:
                payload = payload[1:]
            codes.extend(parse_dtc_bytes(payload, status=status, ecu=message.ecu))
        return codes

    def read_stored_dtcs(self) -> List[Dtc]:
        """Сохранённые ошибки (режим 03) -- те, что зажгли Check Engine."""
        return self._read_dtc_mode(MODE_STORED_DTC, DtcStatus.STORED)

    def read_pending_dtcs(self) -> List[Dtc]:
        """Неподтверждённые ошибки (режим 07) -- сбой был, но пока однократно."""
        return self._read_dtc_mode(MODE_PENDING_DTC, DtcStatus.PENDING)

    def read_permanent_dtcs(self) -> List[Dtc]:
        """Постоянные ошибки (режим 0A) -- сбрасываются только самим ЭБУ."""
        return self._read_dtc_mode(MODE_PERMANENT_DTC, DtcStatus.PERMANENT)

    def read_all_dtcs(self) -> List[Dtc]:
        """Все три категории ошибок одним списком."""
        return self.read_stored_dtcs() + self.read_pending_dtcs() + self.read_permanent_dtcs()

    def clear_dtcs(self) -> Dict[str, bool]:
        """Стереть ошибки и стоп-кадры (режим 04).

        Внимание: вместе с кодами обнуляются мониторы готовности, и до
        завершения ездового цикла машина не пройдёт инструментальный контроль.
        """
        messages = self.elm.try_query("04")
        if not messages:
            raise ObdError("ЭБУ не подтвердил стирание ошибок")
        result = {}
        for message in messages:
            problem = _is_negative(message)
            if problem:
                result[message.ecu] = False
                log.warning("ЭБУ %s отказал в стирании: %s", message.ecu, problem)
            else:
                result[message.ecu] = message.data[:1] == b"\x44"
        return result

    # ------------------------------------------------ режим 02: стоп-кадр

    def read_freeze_frame(self, frame: int = 0) -> List[FreezeFrame]:
        """Прочитать стоп-кадр -- параметры на момент фиксации ошибки."""
        frames: Dict[str, FreezeFrame] = {}

        # PID 02 стоп-кадра хранит код, из-за которого кадр был записан.
        for message in self.elm.try_query(f"0202{frame:02X}"):
            # 42 <pid> <кадр> <старший байт кода> <младший байт кода>
            if _matches(message, MODE_FREEZE, 0x02) and len(message.data) >= 5:
                from avensis.dtc import decode_dtc  # noqa: PLC0415

                trigger = decode_dtc(message.data[3], message.data[4])
                frames[message.ecu] = FreezeFrame(ecu=message.ecu, trigger_dtc=trigger)

        if not frames:
            return []

        for pid in pid_module.LIVE_DEFAULT + [0x03, 0x0B, 0x0E, 0x1F]:
            for message in self.elm.try_query(f"02{pid:02X}{frame:02X}"):
                if not _matches(message, MODE_FREEZE, pid):
                    continue
                value = pid_module.decode_value(pid, message.data[3:])
                if value is not None and message.ecu in frames:
                    frames[message.ecu].values[pid] = value

        return list(frames.values())

    # --------------------------------------------- режим 09: данные о машине

    def _read_info(self, info_type: int) -> Dict[str, bytes]:
        """Собрать полезную нагрузку ответа режима 09 по каждому ЭБУ.

        По CAN приходит одно ISO-TP сообщение ``49 xx NN <данные>``, по K-line --
        несколько сообщений ``49 xx <индекс> <данные>``. В обоих случаях третий
        байт служебный, поэтому склеиваем всё, что за ним.
        """
        chunks: Dict[str, bytearray] = {}
        for message in self.elm.try_query(f"09{info_type:02X}"):
            if not _matches(message, MODE_VEHICLE_INFO, info_type):
                continue
            if len(message.data) <= 3:
                continue
            chunks.setdefault(message.ecu, bytearray()).extend(message.data[3:])
        return {ecu: bytes(buf) for ecu, buf in chunks.items()}

    @staticmethod
    def _to_text(raw: bytes) -> str:
        """Отбросить набивку и превратить байты в печатную строку."""
        return "".join(chr(b) for b in raw if 0x20 <= b < 0x7F).strip()

    def read_vin(self) -> Optional[str]:
        """Прочитать VIN из ЭБУ (режим 09, PID 02)."""
        for raw in self._read_info(0x02).values():
            vin = self._to_text(raw)
            if len(vin) >= 17:
                return vin[:17]
            if vin:
                return vin
        return None

    def read_calibration_ids(self) -> Dict[str, List[str]]:
        """Идентификаторы калибровки прошивки (режим 09, PID 04)."""
        result = {}
        for ecu, raw in self._read_info(0x04).items():
            ids = [self._to_text(raw[i : i + 16]) for i in range(0, len(raw), 16)]
            result[ecu] = [item for item in ids if item]
        return result

    def read_cvn(self) -> Dict[str, List[str]]:
        """Контрольные суммы калибровки (режим 09, PID 06)."""
        result = {}
        for ecu, raw in self._read_info(0x06).items():
            result[ecu] = [raw[i : i + 4].hex().upper() for i in range(0, len(raw), 4) if raw[i : i + 4]]
        return result

    def read_ecu_name(self) -> Dict[str, str]:
        """Имя блока управления (режим 09, PID 0A)."""
        return {ecu: self._to_text(raw) for ecu, raw in self._read_info(0x0A).items() if raw}

    # ------------------------------------------------------- обзор шины

    def discover_ecus(self) -> List[EcuInfo]:
        """Найти блоки, отвечающие на стандартные запросы OBD-II.

        Опрашивается только легальный минимум: широковещательный ``0100``.
        Блоки, не поддерживающие OBD-II (ABS, кузов, подушки), так не видны --
        для них есть :mod:`avensis.uds`.
        """
        from avensis.toyota import describe_ecu  # noqa: PLC0415

        found: Dict[str, EcuInfo] = {}
        for message in self.elm.try_query("0100"):
            if _matches(message, MODE_LIVE, 0x00):
                found[message.ecu] = EcuInfo(
                    address=message.ecu,
                    name=describe_ecu(message.ecu),
                    supported_pids=pid_module.decode_supported(0x00, message.data[2:]),
                )

        for ecu, name in self.read_ecu_name().items():
            if ecu in found and name:
                found[ecu].name = f"{found[ecu].name} ({name})" if found[ecu].name else name

        return list(found.values())
