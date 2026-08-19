"""Запись параметров за поездку и построение графиков по ней.

Файл журнала -- обычный CSV: заголовок с человеческими названиями колонок,
поэтому его можно открыть в любой таблице. Машинное описание колонок
(номер параметра, блок, единицы) лежит в комментариях ``#`` над заголовком,
и по нему строятся графики.

Графики рисуются встроенным SVG без внешних библиотек: готовый файл
открывается в браузере и работает без интернета.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from avensis import pids as pid_module

META_PREFIX = "#"
META_KEY = "# meta: "

#: Колонка с отметкой времени от начала записи.
TIME_COLUMN = "Время (с)"
#: Колонка с состоянием индикатора Check Engine.
MIL_COLUMN = "Check Engine"


@dataclass
class Column:
    """Описание одной колонки журнала."""

    title: str
    pid: Optional[int] = None
    ecu: str = ""
    unit: str = ""


@dataclass
class Trip:
    """Прочитанный журнал поездки."""

    meta: Dict[str, object] = field(default_factory=dict)
    columns: List[Column] = field(default_factory=list)
    rows: List[List[Optional[float]]] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.rows[-1][0] if self.rows and self.rows[0] else 0.0

    def series(self, index: int) -> List[Optional[float]]:
        return [row[index] if index < len(row) else None for row in self.rows]

    def numeric_columns(self) -> List[int]:
        """Индексы колонок, по которым есть смысл строить график."""
        found = []
        for index, column in enumerate(self.columns):
            if index == 0 or column.title == MIL_COLUMN:
                continue
            if any(value is not None for value in self.series(index)):
                found.append(index)
        return found


def column_title(pid: int, ecu: str) -> str:
    """Понятное имя колонки для параметра конкретного блока."""
    entry = pid_module.get_pid(pid)
    name = entry.name if entry else f"Параметр {pid:02X}"
    unit = f" ({entry.unit})" if entry and entry.unit else ""
    return f"{name} [{ecu}]{unit}"


class TripWriter:
    """Пишет журнал поездки в CSV по мере поступления замеров."""

    def __init__(self, path: Path, meta: Optional[Dict[str, object]] = None):
        self.path = Path(path)
        self.meta = dict(meta or {})
        self.meta.setdefault("начало", datetime.now().astimezone().isoformat(timespec="seconds"))
        self._columns: List[Column] = [Column(title=TIME_COLUMN)]
        self._handle = None
        self._writer = None
        self._header_written = False

    def __enter__(self) -> "TripWriter":
        self._handle = open(self.path, "w", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._handle)
        return self

    def __exit__(self, *exc_info) -> None:
        if self._handle:
            self._handle.close()
            self._handle = None

    def _write_header(self, columns: Sequence[Column]) -> None:
        """Записать шапку: комментарии с описанием и строку заголовков.

        Колонки фиксируются по первому замеру -- дальше набор не меняется,
        иначе таблица разъедется.
        """
        self._columns = [Column(title=TIME_COLUMN)] + list(columns)
        for key, value in self.meta.items():
            self._handle.write(f"{META_PREFIX} {key}: {value}\n")
        machine = [
            {"title": c.title, "pid": c.pid, "ecu": c.ecu, "unit": c.unit} for c in self._columns
        ]
        self._handle.write(META_KEY + json.dumps(machine, ensure_ascii=False) + "\n")
        self._writer.writerow(c.title for c in self._columns)
        self._header_written = True

    def append(
        self,
        elapsed: float,
        snapshot: Dict[int, Dict[str, object]],
        mil: Optional[bool] = None,
    ) -> None:
        """Добавить замер.

        :param snapshot: ``{номер параметра: {адрес блока: значение}}``.
        :param mil: состояние Check Engine, если оно отслеживается.
        """
        if not self._header_written:
            columns = [
                Column(
                    title=column_title(pid, ecu),
                    pid=pid,
                    ecu=ecu,
                    unit=(entry.unit if (entry := pid_module.get_pid(pid)) else ""),
                )
                for pid, answers in snapshot.items()
                for ecu in answers
            ]
            if mil is not None:
                columns.append(Column(title=MIL_COLUMN))
            self._write_header(columns)

        row: List[object] = [round(elapsed, 2)]
        for column in self._columns[1:]:
            if column.title == MIL_COLUMN:
                row.append(1 if mil else 0)
                continue
            value = snapshot.get(column.pid, {}).get(column.ecu)
            # Составные параметры (например, кислородный датчик) в таблицу
            # не ложатся -- для графика нужно одно число.
            row.append("" if isinstance(value, (dict, str)) or value is None else value)
        self._writer.writerow(row)
        self._handle.flush()


def read_trip(path: Path) -> Trip:
    """Прочитать журнал поездки обратно."""
    trip = Trip()
    text = Path(path).read_text(encoding="utf-8-sig")
    body_lines = []

    for line in text.splitlines():
        if line.startswith(META_KEY):
            trip.columns = [
                Column(title=item["title"], pid=item.get("pid"),
                       ecu=item.get("ecu", ""), unit=item.get("unit", ""))
                for item in json.loads(line[len(META_KEY):])
            ]
        elif line.startswith(META_PREFIX):
            key, _, value = line[1:].partition(":")
            trip.meta[key.strip()] = value.strip()
        else:
            body_lines.append(line)

    reader = csv.reader(body_lines)
    header = next(reader, None)
    if header and not trip.columns:
        trip.columns = [Column(title=title) for title in header]

    for raw_row in reader:
        if not raw_row:
            continue
        row: List[Optional[float]] = []
        for cell in raw_row:
            try:
                row.append(float(cell))
            except ValueError:
                row.append(None)
        trip.rows.append(row)
    return trip


# ------------------------------------------------------------------- графики

_CHART_WIDTH = 900
_CHART_HEIGHT = 190
_PAD_LEFT = 68
_PAD_RIGHT = 18
_PAD_TOP = 16
_PAD_BOTTOM = 30


def _nice_ticks(low: float, high: float, count: int = 4) -> List[float]:
    """Подобрать круглые значения для делений оси."""
    if math.isclose(low, high):
        return [low]
    step = (high - low) / count
    magnitude = 10 ** math.floor(math.log10(abs(step))) if step else 1
    for multiplier in (1, 2, 2.5, 5, 10):
        if step <= magnitude * multiplier:
            step = magnitude * multiplier
            break
    start = math.floor(low / step) * step
    ticks, value = [], start
    while value <= high + step / 2:
        if low - step / 2 <= value <= high + step / 2:
            ticks.append(round(value, 6))
        value += step
    return ticks or [low, high]


def _format_number(value: float) -> str:
    if abs(value) >= 100 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.1f}"


def _render_chart(trip: Trip, index: int, mil_index: Optional[int]) -> str:
    """Нарисовать один график в виде SVG."""
    column = trip.columns[index]
    times = [row[0] or 0.0 for row in trip.rows]
    values = trip.series(index)
    points = [(t, v) for t, v in zip(times, values) if v is not None]
    if len(points) < 2:
        return ""

    low = min(v for _, v in points)
    high = max(v for _, v in points)
    if math.isclose(low, high):
        low, high = low - 1, high + 1
    span_x = max(times) - min(times) or 1.0
    start_x = min(times)

    def to_x(t: float) -> float:
        return _PAD_LEFT + (t - start_x) / span_x * (_CHART_WIDTH - _PAD_LEFT - _PAD_RIGHT)

    def to_y(v: float) -> float:
        return _PAD_TOP + (high - v) / (high - low) * (_CHART_HEIGHT - _PAD_TOP - _PAD_BOTTOM)

    parts = []

    # Полосы, где горел Check Engine -- сразу видно, при каких условиях.
    if mil_index is not None:
        mil_values = trip.series(mil_index)
        span_start = None
        for time_value, flag in zip(times, mil_values):
            if flag and span_start is None:
                span_start = time_value
            elif not flag and span_start is not None:
                parts.append(
                    f'<rect class="mil" x="{to_x(span_start):.1f}" y="{_PAD_TOP}" '
                    f'width="{max(to_x(time_value) - to_x(span_start), 1):.1f}" '
                    f'height="{_CHART_HEIGHT - _PAD_TOP - _PAD_BOTTOM}"/>'
                )
                span_start = None
        if span_start is not None:
            parts.append(
                f'<rect class="mil" x="{to_x(span_start):.1f}" y="{_PAD_TOP}" '
                f'width="{max(to_x(times[-1]) - to_x(span_start), 1):.1f}" '
                f'height="{_CHART_HEIGHT - _PAD_TOP - _PAD_BOTTOM}"/>'
            )

    for tick in _nice_ticks(low, high):
        y = to_y(tick)
        parts.append(f'<line class="grid" x1="{_PAD_LEFT}" y1="{y:.1f}" '
                     f'x2="{_CHART_WIDTH - _PAD_RIGHT}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{_PAD_LEFT - 8}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{_format_number(tick)}</text>')

    for fraction in (0, 0.25, 0.5, 0.75, 1.0):
        seconds = start_x + span_x * fraction
        x = to_x(seconds)
        parts.append(f'<text class="tick" x="{x:.1f}" y="{_CHART_HEIGHT - 8}" '
                     f'text-anchor="middle">{seconds:.0f} с</text>')

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{to_x(t):.1f},{to_y(v):.1f}" for i, (t, v) in enumerate(points)
    )
    parts.append(f'<path class="line" d="{path}"/>')

    series_values = [v for _, v in points]
    average = sum(series_values) / len(series_values)
    summary = (f"мин {_format_number(low)} · сред {_format_number(average)} "
               f"· макс {_format_number(high)}")

    return f"""    <section class="chart">
      <h2>{_escape(column.title)}</h2>
      <p class="summary">{_escape(summary)}</p>
      <svg viewBox="0 0 {_CHART_WIDTH} {_CHART_HEIGHT}" role="img"
           aria-label="{_escape(column.title)}">
        {''.join(parts)}
      </svg>
    </section>"""


def _escape(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_html(trip: Trip, title: str = "Журнал поездки") -> str:
    """Собрать самодостаточную HTML-страницу с графиками."""
    mil_index = next(
        (i for i, c in enumerate(trip.columns) if c.title == MIL_COLUMN), None
    )
    charts = [_render_chart(trip, index, mil_index) for index in trip.numeric_columns()]
    charts = [chart for chart in charts if chart]

    meta_rows = "".join(
        f"<tr><th>{_escape(key)}</th><td>{_escape(value)}</td></tr>"
        for key, value in trip.meta.items()
    )
    mil_note = ""
    if mil_index is not None and any(trip.series(mil_index)):
        mil_note = ('<p class="note"><span class="swatch"></span>'
                    "розовым отмечены участки, где горел Check Engine</p>")

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)}</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280;
    --grid: #e5e7eb; --line: #2563eb; --mil: rgba(244, 63, 94, 0.16);
    --card: #f9fafb; --border: #e5e7eb;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #111317; --fg: #e8eaed; --muted: #9aa0a6;
      --grid: #2a2e35; --line: #60a5fa; --mil: rgba(244, 63, 94, 0.22);
      --card: #181b20; --border: #2a2e35;
    }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 24px; background: var(--bg); color: var(--fg);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  main {{ max-width: 960px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .note {{ color: var(--muted); font-size: 13px; margin: 4px 0 20px; }}
  .swatch {{
    display: inline-block; width: 22px; height: 11px; vertical-align: -1px;
    background: var(--mil); border: 1px solid var(--border); margin-right: 6px;
  }}
  table.meta {{ border-collapse: collapse; margin: 0 0 24px; font-size: 14px; }}
  table.meta th {{ text-align: left; color: var(--muted); font-weight: 500;
                   padding: 3px 16px 3px 0; }}
  table.meta td {{ padding: 3px 0; }}
  .chart {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px 16px 8px; margin-bottom: 16px;
  }}
  .chart h2 {{ font-size: 15px; margin: 0; font-weight: 600; }}
  .summary {{ color: var(--muted); font-size: 13px; margin: 2px 0 6px; }}
  svg {{ width: 100%; height: auto; display: block; overflow: visible; }}
  .grid {{ stroke: var(--grid); stroke-width: 1; }}
  .line {{ fill: none; stroke: var(--line); stroke-width: 2;
           stroke-linejoin: round; stroke-linecap: round; }}
  .mil {{ fill: var(--mil); }}
  .tick {{ fill: var(--muted); font-size: 11px; }}
  .empty {{ color: var(--muted); }}
</style>
</head>
<body>
<main>
  <h1>{_escape(title)}</h1>
  {mil_note}
  <table class="meta">{meta_rows}</table>
{chr(10).join(charts) if charts else '  <p class="empty">В журнале нет числовых данных для графиков.</p>'}
</main>
</body>
</html>
"""
