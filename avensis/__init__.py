"""Диагностика Toyota Avensis через OBD-II адаптер ELM327.

Пакет даёт три слоя:

* :mod:`avensis.transport` -- байтовый канал до адаптера (USB/Bluetooth/Wi-Fi/симулятор);
* :mod:`avensis.elm327`    -- драйвер ELM327: инициализация, протокол, сборка ISO-TP;
* :mod:`avensis.obd`       -- сервисы OBD-II (режимы 01..0A) и разбор ответов.

Поверх них живут :mod:`avensis.uds` (расширенная диагностика ЭБУ),
:mod:`avensis.toyota` (адреса блоков и реестр настроек) и :mod:`avensis.cli`.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
