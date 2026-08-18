"""Чтение и изменение настраиваемых функций автомобиля.

Порядок работы жёстко задан и не обходится флагами:

1. идентификатор ячейки должен быть подтверждён на конкретной машине;
2. перед записью снимается резервная копия всего блока;
3. запись идёт по принципу «прочитал -- изменил только своё поле -- записал»;
4. записанное перечитывается и сверяется с ожидаемым.

Если любой шаг не прошёл, изменение считается несостоявшимся и об этом
сообщается прямо, без попыток «дожать» результат.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from avensis.toyota import Setting, load_settings
from avensis.uds import SESSION_EXTENDED, UdsClient, UdsError

#: Где хранятся подтверждённые пользователем идентификаторы и резервные копии.
USER_DIR = Path(os.environ.get("AVENSIS_HOME", Path.home() / ".avensis"))
CONFIRMED_FILE = USER_DIR / "confirmed_dids.json"
BACKUP_DIR = USER_DIR / "backups"


class SettingsError(RuntimeError):
    """Настройку невозможно прочитать или изменить в текущих условиях."""


# ------------------------------------------------- подтверждённые идентификаторы


def load_confirmed() -> Dict[str, str]:
    """Идентификаторы, подтверждённые пользователем на своей машине."""
    if not CONFIRMED_FILE.exists():
        return {}
    try:
        return json.loads(CONFIRMED_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SettingsError(f"Файл {CONFIRMED_FILE} повреждён: {exc}") from exc


def confirm_did(key: str, did: str) -> None:
    """Запомнить идентификатор ячейки для настройки ``key``."""
    if key not in load_settings():
        raise SettingsError(f"Настройка {key!r} отсутствует в каталоге")
    confirmed = load_confirmed()
    confirmed[key] = did.upper().replace("0X", "")
    USER_DIR.mkdir(parents=True, exist_ok=True)
    CONFIRMED_FILE.write_text(
        json.dumps(confirmed, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def resolve_did(setting: Setting) -> Optional[str]:
    """Итоговый идентификатор: из каталога либо подтверждённый пользователем."""
    return load_confirmed().get(setting.key) or (setting.did or None)


# ----------------------------------------------------------- резервные копии


@dataclass
class Backup:
    """Снимок содержимого блока управления."""

    created: str
    ecu: str
    request_id: str
    response_id: str
    vin: str = ""
    dids: Dict[str, str] = field(default_factory=dict)
    note: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)

    @classmethod
    def from_file(cls, path: Path) -> "Backup":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**raw)


def backup_ecu(
    uds: UdsClient,
    dids: List[str],
    ecu_name: str = "",
    vin: str = "",
    note: str = "",
) -> Backup:
    """Считать перечисленные идентификаторы и собрать из них резервную копию."""
    snapshot: Dict[str, str] = {}
    for did in dids:
        try:
            value = uds.read_did(did)
        except UdsError:
            continue
        if value.raw:
            snapshot[did.upper()] = value.raw.hex().upper()

    if not snapshot:
        raise SettingsError(
            "Не удалось прочитать ни одного идентификатора -- "
            "резервная копия была бы пустой, запись отменена"
        )

    return Backup(
        created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ecu=ecu_name,
        request_id=uds.request_id,
        response_id=uds.response_id,
        vin=vin,
        dids=snapshot,
        note=note,
    )


def save_backup(backup: Backup, directory: Optional[Path] = None) -> Path:
    """Сохранить резервную копию на диск и вернуть путь к файлу."""
    directory = Path(directory) if directory else BACKUP_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stamp = backup.created.replace(":", "").replace("-", "")
    path = directory / f"{backup.response_id}_{stamp}.json"
    path.write_text(backup.to_json(), encoding="utf-8")
    return path


def restore_backup(uds: UdsClient, backup: Backup, dry_run: bool = True) -> List[str]:
    """Вернуть блоку значения из резервной копии.

    Записываются только те идентификаторы, чьё текущее содержимое отличается
    от сохранённого, и только если совпадает длина -- иначе копия сделана с
    другого блока и её применение сломало бы конфигурацию.
    """
    report: List[str] = []
    if not dry_run:
        uds.start_session(SESSION_EXTENDED)

    for did, saved_hex in sorted(backup.dids.items()):
        saved = bytes.fromhex(saved_hex)
        try:
            current = uds.read_did(did).raw
        except UdsError as exc:
            report.append(f"{did}: пропущен, прочитать не удалось ({exc})")
            continue

        if current == saved:
            report.append(f"{did}: уже совпадает с копией")
            continue
        if len(current) != len(saved):
            report.append(
                f"{did}: пропущен, длина не совпадает "
                f"(в блоке {len(current)} байт, в копии {len(saved)})"
            )
            continue

        if dry_run:
            report.append(f"{did}: будет восстановлен {current.hex().upper()} -> {saved_hex}")
            continue

        uds.write_did(did, saved)
        readback = uds.read_did(did).raw
        status = "восстановлен" if readback == saved else "ЗАПИСЬ НЕ ПОДТВЕРДИЛАСЬ"
        report.append(f"{did}: {status} ({current.hex().upper()} -> {readback.hex().upper()})")

    return report


# ------------------------------------------------------------- чтение и запись


@dataclass
class SettingState:
    """Текущее состояние одной настройки в блоке."""

    setting: Setting
    did: str
    raw: bytes
    value: int

    @property
    def value_name(self) -> str:
        return self.setting.name_of(self.value)


def read_setting(uds: UdsClient, setting: Setting) -> SettingState:
    """Прочитать текущее значение настройки из блока."""
    did = resolve_did(setting)
    if not did:
        raise SettingsError(
            f"Для настройки «{setting.title}» не подтверждён идентификатор ячейки.\n"
            f"Найди его командой:  avensis settings discover --ecu {setting.request_id}\n"
            f"и закрепи командой:   avensis settings confirm {setting.key} <DID>"
        )

    try:
        value = uds.read_did(did)
    except UdsError as exc:
        raise SettingsError(f"Блок не отдал идентификатор {did}: {exc}") from exc

    field_value = setting.extract(value.raw)
    if field_value is None:
        raise SettingsError(
            f"Блок вернул {len(value.raw)} байт, а настройка описана в байте "
            f"{setting.byte}. Похоже, идентификатор {did} подтверждён ошибочно."
        )
    return SettingState(setting=setting, did=did, raw=value.raw, value=field_value)


@dataclass
class WriteResult:
    """Итог попытки изменить настройку."""

    setting: Setting
    did: str
    before: bytes
    after: bytes
    applied: bool
    verified: bool
    backup_path: Optional[Path] = None
    message: str = ""


def write_setting(
    uds: UdsClient,
    setting: Setting,
    value_name: str,
    dry_run: bool = True,
    backup_directory: Optional[Path] = None,
    vin: str = "",
) -> WriteResult:
    """Изменить настройку, предварительно сняв резервную копию.

    :param dry_run: при ``True`` (по умолчанию) на шину ничего не пишется,
        а возвращается предполагаемый результат -- это режим проверки.
    """
    if value_name not in setting.values:
        allowed = ", ".join(f"«{name}»" for name in setting.values)
        raise SettingsError(f"Недопустимое значение «{value_name}». Доступны: {allowed}")

    state = read_setting(uds, setting)
    target = setting.values[value_name]
    patched = setting.apply(state.raw, target)

    if state.raw == patched:
        return WriteResult(
            setting=setting, did=state.did, before=state.raw, after=patched,
            applied=False, verified=True,
            message=f"Настройка уже стоит в положении «{value_name}», запись не нужна",
        )

    if dry_run:
        return WriteResult(
            setting=setting, did=state.did, before=state.raw, after=patched,
            applied=False, verified=False,
            message=(
                f"Проверочный прогон: {state.raw.hex().upper()} -> {patched.hex().upper()} "
                f"(«{state.value_name}» -> «{value_name}»). На шину ничего не отправлено."
            ),
        )

    backup = backup_ecu(
        uds, [state.did], ecu_name=setting.ecu, vin=vin,
        note=f"Перед изменением настройки {setting.key}",
    )
    backup_path = save_backup(backup, backup_directory)

    uds.start_session(SESSION_EXTENDED)
    try:
        uds.write_did(state.did, patched)
    except UdsError as exc:
        return WriteResult(
            setting=setting, did=state.did, before=state.raw, after=state.raw,
            applied=False, verified=False, backup_path=backup_path,
            message=f"Блок отказался принять запись: {exc}",
        )

    readback = uds.read_did(state.did).raw
    verified = readback == patched
    return WriteResult(
        setting=setting, did=state.did, before=state.raw, after=readback,
        applied=True, verified=verified, backup_path=backup_path,
        message=(
            f"Записано и подтверждено чтением: «{state.value_name}» -> «{value_name}»"
            if verified
            else (
                "ЗАПИСЬ НЕ ПОДТВЕРДИЛАСЬ: блок вернул "
                f"{readback.hex().upper()} вместо {patched.hex().upper()}. "
                f"Восстанови блок из копии {backup_path}"
            )
        ),
    )
