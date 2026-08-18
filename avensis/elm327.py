"""Драйвер адаптера ELM327 и совместимых клонов.

Инкапсулирует всю возню с AT-командами: сброс, выключение эха, выбор
протокола, установка заголовков и таймаутов. Наружу отдаёт один метод
:meth:`Elm327.query`, который отправляет запрос на шину и возвращает уже
разобранные сообщения от ЭБУ.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, List, Optional

from avensis.framing import (
    AdapterError,
    EcuMessage,
    NoDataError,
    clean_lines,
    parse_response,
)
from avensis.transport import Transport

log = logging.getLogger(__name__)

PROMPT = b">"

#: Расшифровка номеров протоколов из ответа ``ATDPN``.
PROTOCOL_NAMES = {
    0: "автоопределение",
    1: "SAE J1850 PWM (41.6 кбит/с)",
    2: "SAE J1850 VPW (10.4 кбит/с)",
    3: "ISO 9141-2 (K-line, 5 бод)",
    4: "ISO 14230-4 KWP2000 (медленная инициализация)",
    5: "ISO 14230-4 KWP2000 (быстрая инициализация)",
    6: "ISO 15765-4 CAN (11 бит, 500 кбит/с)",
    7: "ISO 15765-4 CAN (29 бит, 500 кбит/с)",
    8: "ISO 15765-4 CAN (11 бит, 250 кбит/с)",
    9: "ISO 15765-4 CAN (29 бит, 250 кбит/с)",
    10: "SAE J1939 CAN (29 бит, 250 кбит/с)",
    11: "USER1 CAN (11 бит, 125 кбит/с)",
    12: "USER2 CAN (11 бит, 50 кбит/с)",
}


class Elm327Error(RuntimeError):
    """Адаптер не отвечает или ведёт себя не по протоколу."""


class Elm327:
    """Соединение с адаптером ELM327.

    :param transport: открытый или ещё не открытый байтовый канал.
    :param timeout: сколько секунд ждать приглашение ``>`` на обычную команду.
    :param on_traffic: колбэк ``(direction, text)`` для протоколирования обмена.
    """

    def __init__(
        self,
        transport: Transport,
        timeout: float = 5.0,
        on_traffic: Optional[Callable[[str, str], None]] = None,
    ):
        self.transport = transport
        self.timeout = timeout
        self.on_traffic = on_traffic
        self.protocol: int = 0
        self.adapter_id: str = "неизвестен"
        self._current_header: Optional[str] = None
        self._opened = False

    # ------------------------------------------------------------------ связь

    def open(self, protocol: str = "0", fast_init: bool = True) -> None:
        """Открыть канал и привести адаптер в рабочее состояние."""
        if not self._opened:
            self.transport.open()
            self._opened = True
        self.initialize(protocol=protocol, fast_init=fast_init)

    def close(self) -> None:
        if self._opened:
            try:
                self.transport.close()
            finally:
                self._opened = False

    def __enter__(self) -> "Elm327":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _read_until_prompt(self, timeout: float) -> str:
        """Читать, пока адаптер не напечатает приглашение ``>``."""
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        while time.monotonic() < deadline:
            chunk = self.transport.read(1024, timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            if chunk:
                buffer.extend(chunk)
                if PROMPT in buffer:
                    break
        text = buffer.decode("ascii", errors="replace")
        if self.on_traffic:
            self.on_traffic("rx", text)
        log.debug("rx %r", text)
        if PROMPT not in buffer:
            raise Elm327Error(
                f"Адаптер не ответил за {timeout:.1f} с. "
                f"Получено: {text.strip()!r} -- проверь питание адаптера и зажигание."
            )
        return text

    def send(self, command: str, timeout: Optional[float] = None) -> str:
        """Отправить строку адаптеру и вернуть сырой ответ до приглашения."""
        payload = command.strip().upper()
        if self.on_traffic:
            self.on_traffic("tx", payload)
        log.debug("tx %r", payload)
        self.transport.write(payload.encode("ascii") + b"\r")
        return self._read_until_prompt(timeout if timeout is not None else self.timeout)

    def at(self, command: str, timeout: Optional[float] = None) -> List[str]:
        """Выполнить AT-команду и вернуть строки ответа без эха команды."""
        raw = self.send(command, timeout=timeout)
        lines = clean_lines(raw)
        cmd_compact = command.replace(" ", "").upper()
        return [ln for ln in lines if ln.replace(" ", "").upper() != cmd_compact]

    # --------------------------------------------------------- инициализация

    def initialize(self, protocol: str = "0", fast_init: bool = True) -> None:
        """Сбросить адаптер и настроить режим вывода.

        Заголовки включаются намеренно (``ATH1``): без них невозможно понять,
        какой именно блок ответил, а на Avensis на один функциональный запрос
        отвечают сразу несколько ЭБУ.
        """
        self.transport.reset_input()

        # ATZ -- полный сброс, отвечает не сразу, ему нужен свой таймаут.
        reset = self.at("ATZ", timeout=max(self.timeout, 6.0))
        ident = [ln for ln in reset if "ELM" in ln.upper() or "OBD" in ln.upper()]
        if ident:
            self.adapter_id = ident[-1]

        for command, description in (
            ("ATE0", "выключить эхо"),
            ("ATL0", "убрать переводы строк"),
            ("ATS0", "убрать пробелы"),
            ("ATH1", "показывать заголовки"),
        ):
            answer = self.at(command)
            if not any("OK" in ln.upper() for ln in answer):
                log.warning("Адаптер не подтвердил %s (%s): %s", command, description, answer)

        # Адаптивные тайминги: ELM сам подбирает паузу ожидания ответа.
        self.at("ATAT1" if fast_init else "ATAT0")

        if self.adapter_id == "неизвестен":
            described = self.at("ATI")
            if described:
                self.adapter_id = described[0]

        self.set_protocol(protocol)

    def set_protocol(self, protocol: str = "0") -> int:
        """Выбрать протокол шины и определить, какой в итоге установился."""
        self.at(f"ATSP{protocol}")

        # Автоопределение у ELM происходит лениво -- только на первом реальном
        # запросе. Пинаем его стандартным 0100, ошибку здесь глотаем: она может
        # означать просто выключенное зажигание, о чём скажем позже и понятнее.
        try:
            self.send("0100", timeout=max(self.timeout, 10.0))
        except Elm327Error:
            pass

        self.protocol = self._read_protocol_number()
        return self.protocol

    def _read_protocol_number(self) -> int:
        for line in self.at("ATDPN"):
            token = line.strip().upper().lstrip("A")  # "A6" = авто-найденный протокол 6
            if not token:
                continue
            try:
                return int(token[0], 16)
            except ValueError:
                continue
        return 0

    @property
    def protocol_name(self) -> str:
        return PROTOCOL_NAMES.get(self.protocol, f"неизвестный ({self.protocol})")

    @property
    def is_can(self) -> bool:
        from avensis.framing import CAN_PROTOCOLS  # noqa: PLC0415

        return self.protocol in CAN_PROTOCOLS

    # -------------------------------------------------------------- адресация

    def set_header(self, header: Optional[str]) -> None:
        """Адресовать запросы конкретному ЭБУ (``ATSH``) или вернуть умолчание."""
        if header is None:
            if self._current_header is not None:
                # Вернуть заголовок по умолчанию отдельной командой нельзя --
                # у ELM327 это делается повторным выбором текущего протокола.
                self.at(f"ATSP{self.protocol:X}" if self.protocol else "ATSP0")
                self._current_header = None
            return
        header = header.upper()
        if header == self._current_header:
            return
        self.at(f"ATSH{header}")
        self._current_header = header

    def set_rx_filter(self, address: Optional[str]) -> None:
        """Принимать только кадры с указанным CAN-идентификатором (``ATCRA``)."""
        if address is None:
            self.at("ATCRA")
        else:
            self.at(f"ATCRA{address.upper()}")

    def set_flow_control(self, header: str) -> None:
        """Задать заголовок кадра управления потоком для нестандартных адресов."""
        self.at(f"ATFCSH{header.upper()}")
        self.at("ATFCSD300000")
        self.at("ATFCSM1")

    def reset_addressing(self) -> None:
        """Вернуть стандартную адресацию OBD-II после работы с конкретным ЭБУ."""
        self._current_header = None
        self.at("ATCRA")
        self.at("ATFCSM0")
        self.at(f"ATSP{format(self.protocol, 'X')}" if self.protocol else "ATSP0")

    # ---------------------------------------------------------------- запросы

    def query(
        self,
        payload: str,
        expected_responses: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> List[EcuMessage]:
        """Отправить запрос на шину и вернуть разобранные ответы ЭБУ.

        :param payload: байты запроса в hex, например ``"0100"`` или ``"22F190"``.
        :param expected_responses: если известно, сколько блоков ответят,
            ELM327 вернёт управление сразу после N-го ответа, не досиживая
            таймаут. Для функциональных запросов оставить ``None``.
        :raises NoDataError: ни один блок не ответил.
        """
        command = payload.replace(" ", "").upper()
        if expected_responses is not None:
            if not 1 <= expected_responses <= 15:
                raise ValueError("expected_responses должен быть в диапазоне 1..15")
            command += format(expected_responses, "X")

        raw = self.send(command, timeout=timeout)
        return parse_response(raw, self.protocol)

    def try_query(self, payload: str, **kwargs) -> List[EcuMessage]:
        """Как :meth:`query`, но при молчании шины возвращает пустой список."""
        try:
            return self.query(payload, **kwargs)
        except (NoDataError, AdapterError) as exc:
            log.debug("Запрос %s без ответа: %s", payload, exc)
            return []

    # ------------------------------------------------------------- диагностика

    def read_voltage(self) -> Optional[float]:
        """Напряжение на разъёме OBD, В. Полезно как проверка живости связи."""
        for line in self.at("ATRV"):
            token = line.upper().replace("V", "").strip()
            try:
                return float(token)
            except ValueError:
                continue
        return None
