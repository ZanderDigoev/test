"""Двигатели Avensis и подбор параметров под конкретный мотор.

Тип двигателя определяется не по догадке, а по данным с шины: блок сообщает
тип топлива (параметр 0x51) и признак воспламенения от сжатия в байте
готовности мониторов. Этого достаточно, чтобы выбрать осмысленный набор
параметров -- дизелю нужны давление в рампе и сажевый фильтр, бензину --
лямбды и катализатор.

Конкретный код мотора по шине узнать нельзя: калибровка прошивки его не
называет. Поэтому справочник ниже -- это подсказка «что обычно ломается»,
а не утверждение о том, что стоит под капотом. Мотор можно назвать явно
флагом ``--engine``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

PETROL = "бензиновый"
DIESEL = "дизельный"


@dataclass(frozen=True)
class EngineProfile:
    """Двигатель, встречающийся на этом кузове."""

    code: str
    title: str
    fuel: str
    displacement: str
    years: str
    live_pids: List[int]
    """Параметры, за которыми имеет смысл следить именно на этом моторе."""

    watch_codes: List[str] = field(default_factory=list)
    """Коды, которые на этом моторе встречаются чаще прочих."""

    notes: str = ""

    @property
    def is_diesel(self) -> bool:
        return self.fuel == DIESEL


# Базовый набор -- то, что осмысленно на любом моторе.
_COMMON = [0x0C, 0x0D, 0x05, 0x0F, 0x04, 0x11, 0x42]

# Дизелю важны топливная рампа, наддув и температура ОГ.
_DIESEL_EXTRA = [0x23, 0x22, 0x0B, 0x33, 0x2C, 0x2D, 0x5C, 0x5E, 0x62]

# Бензину -- смесь, лямбды и коррекции.
_PETROL_EXTRA = [0x10, 0x06, 0x07, 0x0E, 0x14, 0x15, 0x44, 0x3C]


ENGINE_PROFILES: Dict[str, EngineProfile] = {
    profile.code: profile
    for profile in [
        EngineProfile(
            code="1ZZ-FE",
            title="1.8 бензин, 129 л.с.",
            fuel=PETROL,
            displacement="1794 см³",
            years="2003–2008",
            live_pids=_COMMON + _PETROL_EXTRA,
            watch_codes=["P0301", "P0302", "P0303", "P0304", "P1349", "P0420", "P0171", "P0505"],
            notes="Слабое место -- расход масла и залегание колец; следи за пропусками "
                  "воспламенения и обеднением смеси.",
        ),
        EngineProfile(
            code="3ZZ-FE",
            title="1.6 бензин, 110 л.с.",
            fuel=PETROL,
            displacement="1598 см³",
            years="2003–2008",
            live_pids=_COMMON + _PETROL_EXTRA,
            watch_codes=["P0300", "P0420", "P0171", "P1349"],
        ),
        EngineProfile(
            code="1AZ-FSE",
            title="2.0 бензин с непосредственным впрыском (D-4), 147 л.с.",
            fuel=PETROL,
            displacement="1998 см³",
            years="2003–2008",
            live_pids=_COMMON + _PETROL_EXTRA + [0x23],
            watch_codes=["P1155", "P1130", "P1133", "P0171", "P2196", "P1251", "P0420", "P2402"],
            notes="Непосредственный впрыск: типичны нагар на впускных клапанах, "
                  "износ ТНВД и загрязнение дроссельного узла.",
        ),
        EngineProfile(
            code="1CD-FTV",
            title="2.0 D-4D, 116 л.с.",
            fuel=DIESEL,
            displacement="1995 см³",
            years="2003–2006",
            live_pids=_COMMON + _DIESEL_EXTRA,
            watch_codes=["P0401", "P0403", "P1251", "P0299", "P0234", "P0087", "P0088",
                         "P0191", "P1229", "P0100", "P0335"],
            notes="Самый частый дизель на этом кузове. Классика -- закоксованный клапан EGR, "
                  "залипание геометрии турбины и износ форсунок.",
        ),
        EngineProfile(
            code="1AD-FTV",
            title="2.0 D-4D, 126 л.с.",
            fuel=DIESEL,
            displacement="1998 см³",
            years="2006–2008",
            live_pids=_COMMON + _DIESEL_EXTRA + [0x3C],
            watch_codes=["P0401", "P2002", "P0299", "P0087", "P2452", "P1251"],
            notes="Уже с сажевым фильтром: следи за его эффективностью и перепадом давления.",
        ),
        EngineProfile(
            code="2AD-FTV",
            title="2.2 D-4D, 150/177 л.с.",
            fuel=DIESEL,
            displacement="2231 см³",
            years="2005–2008",
            live_pids=_COMMON + _DIESEL_EXTRA + [0x3C, 0x3E],
            watch_codes=["P2002", "P2003", "P0401", "P0087", "P0093", "P2452", "P1251", "P0299"],
            notes="Известная беда -- прогар прокладки головки блока и забитый сажевый фильтр. "
                  "Версия 177 л.с. (2AD-FHV) страдает этим заметно чаще.",
        ),
    ]
}


def profiles_for(fuel: str) -> List[EngineProfile]:
    """Моторы этого кузова с указанным типом топлива."""
    return [profile for profile in ENGINE_PROFILES.values() if profile.fuel == fuel]


def get_profile(code: str) -> Optional[EngineProfile]:
    """Найти мотор по коду, без учёта регистра и дефисов."""
    normalised = code.upper().replace("-", "").replace(" ", "")
    for key, profile in ENGINE_PROFILES.items():
        if key.upper().replace("-", "") == normalised:
            return profile
    return None


@dataclass
class EngineDetection:
    """Что удалось выяснить о двигателе по данным с шины."""

    fuel: Optional[str] = None
    source: str = ""
    """Откуда взят вывод -- чтобы было видно, насколько ему верить."""

    candidates: List[EngineProfile] = field(default_factory=list)
    forced: Optional[EngineProfile] = None

    @property
    def profile(self) -> Optional[EngineProfile]:
        """Профиль для подбора параметров: явно заданный либо первый подходящий."""
        if self.forced:
            return self.forced
        return self.candidates[0] if self.candidates else None

    @property
    def live_pids(self) -> List[int]:
        """Набор параметров под этот мотор, без повторов и в стабильном порядке."""
        profile = self.profile
        if profile is None:
            return list(_COMMON)
        seen, ordered = set(), []
        for pid in profile.live_pids:
            if pid not in seen:
                seen.add(pid)
                ordered.append(pid)
        return ordered

    def describe(self) -> str:
        if self.forced:
            return f"{self.forced.code} — {self.forced.title} (задан вручную)"
        if not self.fuel:
            return "тип двигателя определить не удалось"
        names = ", ".join(profile.code for profile in self.candidates)
        return f"{self.fuel}; для этого кузова подходят: {names or 'нет данных'} ({self.source})"


def detect(
    fuel_type_value: Optional[str] = None,
    compression_ignition: Optional[bool] = None,
    forced_code: Optional[str] = None,
) -> EngineDetection:
    """Определить тип двигателя по данным, полученным с шины.

    :param fuel_type_value: расшифрованный параметр 0x51, если блок его отдал.
    :param compression_ignition: признак дизеля из байта готовности мониторов.
    :param forced_code: код мотора, заданный пользователем явно.
    """
    detection = EngineDetection()

    if forced_code:
        profile = get_profile(forced_code)
        if profile is None:
            known = ", ".join(ENGINE_PROFILES)
            raise ValueError(f"Неизвестный код двигателя {forced_code!r}. Известные: {known}")
        detection.forced = profile
        detection.fuel = profile.fuel
        detection.source = "указан флагом --engine"
        detection.candidates = [profile]
        return detection

    # Параметр 0x51 точнее: он называет топливо прямо. Признак из мониторов --
    # запасной вариант, он лишь отличает воспламенение от сжатия от искрового.
    if fuel_type_value:
        lowered = fuel_type_value.lower()
        if "дизель" in lowered:
            detection.fuel, detection.source = DIESEL, "параметр «тип топлива»"
        elif "бензин" in lowered:
            detection.fuel, detection.source = PETROL, "параметр «тип топлива»"

    if detection.fuel is None and compression_ignition is not None:
        detection.fuel = DIESEL if compression_ignition else PETROL
        detection.source = "признак воспламенения в байте мониторов"

    if detection.fuel:
        detection.candidates = profiles_for(detection.fuel)
    return detection
