"""Журнал поездки: запись, чтение и построение графиков."""

from avensis.triplog import MIL_COLUMN, TripWriter, read_trip, render_html


def write_sample(path, samples=6, mil=None):
    with TripWriter(path, {"автомобиль": "SB1BJ56L20E095222"}) as writer:
        for step in range(samples):
            writer.append(
                step * 1.0,
                {0x0C: {"7E8": 800 + step * 100}, 0x05: {"7E8": 60 + step}},
                mil=mil if mil is None else (step >= samples // 2),
            )
    return path


def test_round_trip(tmp_path):
    trip = read_trip(write_sample(tmp_path / "t.csv"))
    assert len(trip.rows) == 6
    assert trip.meta["автомобиль"] == "SB1BJ56L20E095222"
    assert trip.duration == 5.0


def test_column_titles_carry_name_and_unit(tmp_path):
    trip = read_trip(write_sample(tmp_path / "t.csv"))
    titles = [c.title for c in trip.columns]
    assert titles[0] == "Время (с)"
    assert "Обороты двигателя [7E8] (об/мин)" in titles


def test_machine_readable_column_metadata_survives(tmp_path):
    trip = read_trip(write_sample(tmp_path / "t.csv"))
    rpm = next(c for c in trip.columns if c.pid == 0x0C)
    assert rpm.ecu == "7E8"
    assert rpm.unit == "об/мин"


def test_values_are_preserved(tmp_path):
    trip = read_trip(write_sample(tmp_path / "t.csv"))
    index = next(i for i, c in enumerate(trip.columns) if c.pid == 0x0C)
    assert trip.series(index) == [800, 900, 1000, 1100, 1200, 1300]


def test_mil_column_is_recorded_but_not_plotted(tmp_path):
    trip = read_trip(write_sample(tmp_path / "t.csv", mil=True))
    assert MIL_COLUMN in [c.title for c in trip.columns]
    plotted = [trip.columns[i].title for i in trip.numeric_columns()]
    assert MIL_COLUMN not in plotted
    assert "Время (с)" not in plotted


def test_csv_opens_as_a_plain_table(tmp_path):
    """Файл должен читаться таблицей, а не только этой программой."""
    import csv

    path = write_sample(tmp_path / "t.csv")
    with open(path, encoding="utf-8-sig") as handle:
        rows = [r for r in csv.reader(handle) if r and not r[0].startswith("#")]
    assert rows[0][0] == "Время (с)"
    assert len(rows) == 7  # заголовок и шесть замеров


def test_html_is_self_contained(tmp_path):
    html = render_html(read_trip(write_sample(tmp_path / "t.csv")))
    assert "<svg" in html
    assert "Обороты двигателя" in html
    # Никаких внешних загрузок: страница должна работать без интернета.
    assert "http://" not in html.replace('xmlns="http://www.w3.org', "")
    assert "<script src" not in html


def test_mil_spans_are_drawn(tmp_path):
    html = render_html(read_trip(write_sample(tmp_path / "t.csv", mil=True)))
    assert 'class="mil"' in html
    assert "горел Check Engine" in html


def test_single_sample_produces_no_broken_chart(tmp_path):
    html = render_html(read_trip(write_sample(tmp_path / "t.csv", samples=1)))
    assert "нет числовых данных" in html


def test_titles_are_escaped(tmp_path):
    path = tmp_path / "t.csv"
    with TripWriter(path, {"заметка": "<script>alert(1)</script>"}) as writer:
        writer.append(0.0, {0x0C: {"7E8": 800}})
        writer.append(1.0, {0x0C: {"7E8": 900}})
    html = render_html(read_trip(path))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
