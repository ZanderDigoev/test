"""Расширенная диагностика по UDS (ISO 14229) / KWP2000.

Стандартный OBD-II видит только силовой агрегат. ABS, подушки, кузовная
электроника и комбинация приборов общаются по UDS с физической адресацией --
для них нужен этот модуль.

Все операции записи здесь построены по принципу «прочитал -- изменил один
бит -- записал -- перечитал и сверил». Слепая запись не поддерживается
намеренно.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from avensis.dtc import Dtc, DtcStatus, decode_dtc, make_dtc
from avensis.elm327 import Elm327
from avensis.framing import AdapterError, EcuMessage, NoDataError

log = logging.getLogger(__name__)

# --------------------------------------------------------------- константы UDS

SID_DIAGNOSTIC_SESSION = 0x10
SID_ECU_RESET = 0x11
SID_CLEAR_DTC = 0x14
SID_READ_DTC = 0x19
SID_READ_DID = 0x22
SID_SECURITY_ACCESS = 0x27
SID_WRITE_DID = 0x2E
SID_ROUTINE_CONTROL = 0x31
SID_TESTER_PRESENT = 0x3E

SESSION_DEFAULT = 0x01
SESSION_PROGRAMMING = 0x02
SESSION_EXTENDED = 0x03

RESET_HARD = 0x01
RESET_KEY_OFF_ON = 0x02
RESET_SOFT = 0x03

NRC_RESPONSE_PENDING = 0x78

NRC_TEXT = {
    0x10: "общий отказ",
    0x11: "сервис не поддерживается этим блоком",
    0x12: "подфункция не поддерживается",
    0x13: "неверная длина или формат запроса",
    0x14: "ответ слишком длинный",
    0x21: "блок занят, повторите позже",
    0x22: "условия не выполнены",
    0x24: "нарушена последовательность запросов",
    0x31: "запрос вне допустимого диапазона (нет такого идентификатора)",
    0x33: "доступ запрещён -- требуется авторизация (SecurityAccess)",
    0x35: "неверный ключ авторизации",
    0x36: "превышено число попыток авторизации",
    0x37: "требуется выдержать паузу перед новой попыткой",
    0x72: "ошибка записи в память блока",
    0x78: "запрос принят, ответ готовится",
    0x7E: "подфункция не поддерживается в текущей сессии",
    0x7F: "сервис не поддерживается в текущей сессии",
}

#: Стандартные идентификаторы данных: их поддерживает большинство блоков.
IDENTIFICATION_DIDS = {
    "F186": "Активная диагностическая сессия",
    "F187": "Номер запчасти производителя",
    "F188": "Номер ПО блока",
    "F189": "Версия ПО блока",
    "F18A": "Идентификатор поставщика",
    "F18C": "Серийный номер блока",
    "F190": "VIN, записанный в блоке",
    "F191": "Номер аппаратной части блока",
    "F194": "Номер ПО поставщика",
    "F195": "Версия ПО поставщика",
    "F197": "Название системы / тип двигателя",
}

#: Маски статусов для сервиса 0x19 подфункции 0x02.
DTC_STATUS_CONFIRMED = 0x08
DTC_STATUS_ANY = 0xFF


class UdsError(RuntimeError):
    """Блок отказал в обслуживании запроса UDS."""

    def __init__(self, sid: int, nrc: int, message: str = ""):
        self.sid = sid
        self.nrc = nrc
        meaning = NRC_TEXT.get(nrc, f"неизвестный код 0x{nrc:02X}")
        super().__init__(
            message or f"Сервис 0x{sid:02X} отклонён: {meaning} (NRC 0x{nrc:02X})"
        )


class UdsNoResponse(UdsError):
    """По адресу никто не отозвался.

    Отделено от :class:`UdsError` намеренно: молчание означает «блока нет»,
    а любой, даже отрицательный, ответ означает «блок есть, но отказал».
    Путать эти случаи нельзя -- на них строится поиск блоков на шине.
    """

    def __init__(self, sid: int = 0x00):
        super().__init__(sid, 0x00, "Ответ от блока не получен")


@dataclass
class DidValue:
    """Прочитанное значение идентификатора данных."""

    did: str
    raw: bytes
    label: str = ""

    @property
    def text(self) -> str:
        """Печатное представление, если содержимое похоже на строку."""
        printable = "".join(chr(b) for b in self.raw if 0x20 <= b < 0x7F)
        return printable.strip() if len(printable) >= max(3, len(self.raw) // 2) else ""

    def __str__(self) -> str:
        shown = self.text or self.raw.hex().upper()
        return f"{self.did} {self.label}: {shown}".strip()


class UdsClient:
    """Сессия UDS с одним конкретным блоком управления.

    Используется как менеджер контекста: на входе выставляется физическая
    адресация, на выходе она гарантированно возвращается к стандартной,
    иначе последующие запросы OBD-II уйдут не туда.
    """

    def __init__(
        self,
        elm: Elm327,
        request_id: str,
        response_id: str,
        timeout: float = 3.0,
    ):
        self.elm = elm
        self.request_id = request_id.upper()
        self.response_id = response_id.upper()
        self.timeout = timeout
        self._active = False

    def __enter__(self) -> "UdsClient":
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def open(self) -> None:
        self.elm.set_header(self.request_id)
        # Блоки вне силового агрегата отвечают заметно медленнее ЭБУ двигателя,
        # поэтому поднимаем время ожидания ответа в самом адаптере (ATST, шаг 4 мс).
        self.elm.at("ATST64")
        if self.elm.is_can:
            self.elm.set_rx_filter(self.response_id)
            # Кадр управления потоком должен уходить на адрес запроса, иначе
            # блок не отдаст многокадровый ответ.
            self.elm.set_flow_control(self.request_id)
        self._active = True

    def close(self) -> None:
        if self._active:
            self.elm.at("ATST32")
            self.elm.reset_addressing()
            self._active = False

    # ------------------------------------------------------------ низкий уровень

    #: Сколько раз повторять запрос, пока блок отвечает «ответ готовится».
    PENDING_RETRIES = 3

    def request(self, payload: str, timeout: Optional[float] = None) -> bytes:
        """Отправить запрос UDS и вернуть полезную нагрузку положительного ответа.

        Ответ 0x78 («запрос принят, ответ готовится») ошибкой не считается:
        блок просит подождать. Адаптер ELM327 к этому моменту уже вернул
        приглашение, поэтому единственный способ забрать отложенный ответ --
        повторить запрос. Все используемые здесь сервисы идемпотентны
        (чтение, а также запись одного и того же значения), поэтому повтор
        безопасен.
        """
        sid = int(payload[:2], 16)

        for attempt in range(self.PENDING_RETRIES + 1):
            pending = False
            for message in self._collect(payload, timeout):
                data = message.data
                if not data:
                    continue
                if data[0] == 0x7F and len(data) >= 3:
                    if data[2] == NRC_RESPONSE_PENDING:
                        pending = True
                        continue
                    raise UdsError(sid, data[2])
                if data[0] == sid + 0x40:
                    return data[1:]

            if not pending:
                raise UdsNoResponse(sid)
            if attempt < self.PENDING_RETRIES:
                log.debug("Блок %s просит подождать, повтор %s", self.response_id, attempt + 1)
                time.sleep(0.3)

        raise UdsError(sid, NRC_RESPONSE_PENDING)

    def _collect(self, payload: str, timeout: Optional[float]) -> List[EcuMessage]:
        try:
            return self.elm.query(payload, timeout=timeout or self.timeout)
        except (NoDataError, AdapterError) as exc:
            raise UdsNoResponse() from exc

    # ------------------------------------------------------------ сервисы UDS

    def start_session(self, level: int = SESSION_EXTENDED) -> bytes:
        """Переключить блок в нужную диагностическую сессию (сервис 0x10)."""
        return self.request(f"10{level:02X}")

    def tester_present(self, suppress_response: bool = False) -> None:
        """Сообщить блоку, что тестер на связи -- иначе сессия отвалится (0x3E)."""
        try:
            self.request("3E80" if suppress_response else "3E00", timeout=1.0)
        except UdsError:
            pass

    def read_did(self, did: str) -> DidValue:
        """Прочитать идентификатор данных (сервис 0x22)."""
        did = did.upper().replace("0X", "")
        payload = self.request(f"22{did}")
        # Ответ повторяет идентификатор: 62 <DID_hi> <DID_lo> <данные>.
        if len(payload) >= 2 and payload[:2].hex().upper() == did:
            payload = payload[2:]
        return DidValue(did=did, raw=payload, label=IDENTIFICATION_DIDS.get(did, ""))

    def write_did(self, did: str, data: bytes) -> None:
        """Записать идентификатор данных (сервис 0x2E).

        Метод намеренно низкоуровневый и «тупой»: проверку допустимости и
        сверку записанного делает :mod:`avensis.settings_ops`.
        """
        did = did.upper().replace("0X", "")
        self.request(f"2E{did}{data.hex().upper()}")

    def read_dtcs(self, status_mask: int = DTC_STATUS_ANY) -> List[Dtc]:
        """Прочитать ошибки блока (сервис 0x19, подфункция 0x02)."""
        payload = self.request(f"1902{status_mask:02X}")
        if len(payload) < 2:
            return []
        # 59 02 <маска доступности> затем записи по 4 байта: 3 байта кода + статус.
        records = payload[2:]
        codes: List[Dtc] = []
        for index in range(0, len(records) - 3, 4):
            high, mid, low, status = records[index : index + 4]
            code = decode_dtc(high, mid)
            if code is None:
                continue
            if low:
                # Третий байт -- тип отказа; у Toyota он часто несёт подкод.
                code = f"{code}-{low:02X}"
            state = DtcStatus.STORED if status & DTC_STATUS_CONFIRMED else DtcStatus.PENDING
            codes.append(make_dtc(code.split("-")[0], status=state, ecu=self.response_id))
        return codes

    def clear_dtcs(self, group: int = 0xFFFFFF) -> None:
        """Стереть ошибки блока (сервис 0x14). По умолчанию -- все группы."""
        self.request(f"14{group:06X}")

    def ecu_reset(self, kind: int = RESET_SOFT) -> None:
        """Перезагрузить блок (сервис 0x11)."""
        self.request(f"11{kind:02X}")

    def security_access(self, level: int, key_from_seed: Callable[[bytes], bytes]) -> None:
        """Пройти авторизацию (сервис 0x27) с внешним алгоритмом ключа.

        Алгоритм расчёта ключа Toyota не публикуется, поэтому он не зашит в
        программу, а передаётся вызывающим кодом. Без него блоки, требующие
        авторизации, отдадут NRC 0x33.
        """
        seed = self.request(f"27{level:02X}")[1:]
        if not any(seed):
            return  # Нулевой seed означает, что доступ уже открыт.
        key = key_from_seed(bytes(seed))
        self.request(f"27{level + 1:02X}{key.hex().upper()}")

    # --------------------------------------------------------- разведка блока

    def probe(self) -> bool:
        """Есть ли блок по этому адресу.

        Проверяется самым безобидным запросом -- TesterPresent. Даже
        отрицательный ответ означает, что кто-то на адресе живёт.
        """
        try:
            self.request("3E00", timeout=1.5)
        except UdsNoResponse:
            return False
        except UdsError:
            # Блок ответил отказом -- значит, он на шине есть.
            return True
        return True

    def read_identification(self) -> List[DidValue]:
        """Собрать паспорт блока по стандартным идентификаторам."""
        found = []
        for did, label in IDENTIFICATION_DIDS.items():
            try:
                value = self.read_did(did)
            except UdsError:
                continue
            if value.raw:
                value.label = label
                found.append(value)
        return found

    def scan_dids(
        self,
        start: int = 0x0000,
        end: int = 0x00FF,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, bytes]:
        """Перебрать диапазон идентификаторов и вернуть те, что отвечают.

        Это основной инструмент для поиска настроек: заводские карты Toyota
        закрыты, и адрес нужной ячейки находят именно перебором с последующим
        сравнением дампов «до» и «после» изменения функции штатным способом.

        Операция только читающая и потому безопасная, но небыстрая: каждый
        идентификатор -- отдельный запрос на шину.
        """
        found: Dict[str, bytes] = {}
        total = end - start + 1
        for offset, did_number in enumerate(range(start, end + 1)):
            did = f"{did_number:04X}"
            try:
                value = self.read_did(did)
            except UdsError:
                continue
            except (NoDataError, AdapterError):
                continue
            if value.raw:
                found[did] = value.raw
            if on_progress and offset % 16 == 0:
                on_progress(offset, total)
        if on_progress:
            on_progress(total, total)
        return found
