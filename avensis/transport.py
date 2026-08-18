"""Байтовые каналы до ELM327-адаптера.

Поддерживаются три вида подключения плюс встроенный симулятор::

    /dev/ttyUSB0              последовательный порт (USB-адаптер)
    /dev/rfcomm0              Bluetooth SPP, привязанный через rfcomm
    COM3                      последовательный порт в Windows
    serial:///dev/ttyUSB0?baud=38400
    tcp://192.168.0.10:35000  Wi-Fi адаптер
    sim://                    виртуальный ЭБУ, железо не нужно
"""

from __future__ import annotations

import abc
import socket
import time
from typing import Optional
from urllib.parse import parse_qs, urlparse

DEFAULT_BAUD = 38400
DEFAULT_TCP_PORT = 35000


class TransportError(RuntimeError):
    """Канал до адаптера не открылся или оборвался."""


class Transport(abc.ABC):
    """Минимальный дуплексный канал: открыть, писать, читать, закрыть."""

    #: человекочитаемое имя, попадает в логи и отчёты
    name: str = "transport"

    @abc.abstractmethod
    def open(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @abc.abstractmethod
    def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    def read(self, size: int = 1024, timeout: float = 1.0) -> bytes:
        """Вернуть до ``size`` байт, ожидая не дольше ``timeout`` секунд.

        Возврат ``b""`` означает «за отведённое время ничего не пришло»,
        это не ошибка -- вызывающий код сам решает, ждать ли дальше.
        """

    def reset_input(self) -> None:
        """Выбросить всё, что уже лежит в приёмном буфере."""

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class SerialTransport(Transport):
    """Последовательный порт через pyserial (USB и Bluetooth-SPP адаптеры)."""

    def __init__(self, device: str, baudrate: int = DEFAULT_BAUD):
        self.device = device
        self.baudrate = baudrate
        self.name = f"serial:{device}@{baudrate}"
        self._port = None

    def open(self) -> None:
        try:
            import serial  # noqa: PLC0415 -- зависимость нужна только этому классу
        except ImportError as exc:  # pragma: no cover - зависит от окружения
            raise TransportError(
                "Для работы с последовательным портом нужен pyserial: pip install pyserial"
            ) from exc

        try:
            self._port = serial.Serial(
                port=self.device,
                baudrate=self.baudrate,
                timeout=0,  # неблокирующее чтение, таймаут держим сами
                write_timeout=2.0,
            )
        except Exception as exc:  # pragma: no cover - зависит от железа
            raise TransportError(f"Не удалось открыть {self.device}: {exc}") from exc

        # Дешёвые китайские клоны ELM327 после открытия порта какое-то время
        # плюются мусором -- дадим им успокоиться и почистим буфер.
        time.sleep(0.2)
        self.reset_input()

    def close(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            finally:
                self._port = None

    def _require_port(self):
        if self._port is None:
            raise TransportError("Порт не открыт")
        return self._port

    def write(self, data: bytes) -> None:
        port = self._require_port()
        try:
            port.write(data)
            port.flush()
        except Exception as exc:  # pragma: no cover - зависит от железа
            raise TransportError(f"Ошибка записи в {self.device}: {exc}") from exc

    def read(self, size: int = 1024, timeout: float = 1.0) -> bytes:
        port = self._require_port()
        deadline = time.monotonic() + timeout
        while True:
            waiting = port.in_waiting
            if waiting:
                return port.read(min(waiting, size))
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.005)

    def reset_input(self) -> None:
        if self._port is not None:
            try:
                self._port.reset_input_buffer()
            except Exception:  # pragma: no cover - не критично
                pass


class TcpTransport(Transport):
    """Wi-Fi адаптеры ELM327 (обычно 192.168.0.10:35000)."""

    def __init__(self, host: str, port: int = DEFAULT_TCP_PORT, connect_timeout: float = 5.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.name = f"tcp:{host}:{port}"
        self._sock: Optional[socket.socket] = None

    def open(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), self.connect_timeout)
        except OSError as exc:
            raise TransportError(f"Не удалось подключиться к {self.host}:{self.port}: {exc}") from exc
        self._sock.setblocking(False)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _require_sock(self) -> socket.socket:
        if self._sock is None:
            raise TransportError("Сокет не открыт")
        return self._sock

    def write(self, data: bytes) -> None:
        sock = self._require_sock()
        sock.setblocking(True)
        try:
            sock.sendall(data)
        except OSError as exc:
            raise TransportError(f"Ошибка записи в {self.name}: {exc}") from exc
        finally:
            sock.setblocking(False)

    def read(self, size: int = 1024, timeout: float = 1.0) -> bytes:
        import select  # noqa: PLC0415 -- нужен только здесь

        sock = self._require_sock()
        ready, _, _ = select.select([sock], [], [], timeout)
        if not ready:
            return b""
        try:
            chunk = sock.recv(size)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise TransportError(f"Ошибка чтения из {self.name}: {exc}") from exc
        if chunk == b"":
            raise TransportError(f"{self.name}: адаптер закрыл соединение")
        return chunk

    def reset_input(self) -> None:
        try:
            while self.read(4096, timeout=0.05):
                pass
        except TransportError:
            pass


class LoopbackTransport(Transport):
    """Канал в объект-симулятор, живущий в этом же процессе.

    ``responder`` -- любой объект с методом ``feed(bytes) -> bytes``.
    """

    def __init__(self, responder):
        self.responder = responder
        self.name = "sim://"
        self._buffer = bytearray()
        self._open = False

    def open(self) -> None:
        self._open = True
        self._buffer.clear()

    def close(self) -> None:
        self._open = False

    def write(self, data: bytes) -> None:
        if not self._open:
            raise TransportError("Симулятор не запущен")
        self._buffer.extend(self.responder.feed(data))

    def read(self, size: int = 1024, timeout: float = 1.0) -> bytes:
        if not self._buffer:
            return b""
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk

    def reset_input(self) -> None:
        self._buffer.clear()


def create_transport(url: str, **kwargs) -> Transport:
    """Собрать транспорт по строке подключения.

    Понимает ``sim://``, ``tcp://host:port``, ``serial://device?baud=N``
    и голый путь к устройству (``/dev/ttyUSB0``, ``COM3``).
    """
    if not url:
        raise TransportError("Пустая строка подключения")

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme == "sim":
        from avensis.simulator import ElmSimulator  # noqa: PLC0415 -- циклический импорт

        profile = parse_qs(parsed.query).get("profile", ["avensis_t25"])[0]
        return LoopbackTransport(ElmSimulator(profile=profile))

    if scheme in ("tcp", "wifi"):
        if not parsed.hostname:
            raise TransportError(f"В адресе {url!r} не указан хост")
        return TcpTransport(parsed.hostname, parsed.port or DEFAULT_TCP_PORT, **kwargs)

    if scheme == "serial":
        device = parsed.path or parsed.netloc
        baud = int(parse_qs(parsed.query).get("baud", [DEFAULT_BAUD])[0])
        return SerialTransport(device, baud)

    # Голый путь: /dev/ttyUSB0, /dev/rfcomm0, COM3. Схема "c" у "COM3:" -- артефакт
    # urlparse на Windows-именах, поэтому такие строки тоже отдаём в serial.
    return SerialTransport(url, kwargs.get("baudrate", DEFAULT_BAUD))
