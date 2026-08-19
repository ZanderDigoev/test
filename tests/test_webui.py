"""Веб-интерфейс: сервер поднимается на симуляторе и отвечает по-настоящему."""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from avensis.elm327 import Elm327
from avensis.simulator import ElmSimulator
from avensis.transport import LoopbackTransport
from avensis.webui import PAGE, DiagnosticService, make_handler


@pytest.fixture
def service():
    elm = Elm327(LoopbackTransport(ElmSimulator()))
    elm.open()
    yield DiagnosticService(elm, allow_write=False)
    elm.close()


@pytest.fixture
def writable_service():
    elm = Elm327(LoopbackTransport(ElmSimulator()))
    elm.open()
    yield DiagnosticService(elm, allow_write=True)
    elm.close()


@pytest.fixture
def server(service):
    """Живой сервер на случайном порту -- проверяем именно HTTP, а не вызовы."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, json.loads(response.read())


# --------------------------------------------------------------- сервисный слой


def test_status_reports_the_lamp(service):
    status = service.status()
    assert status["mil_on"] is True
    assert status["vin"] == "SB1BJ56L20E095222"
    assert "CAN" in status["protocol"]
    assert status["write_allowed"] is False


def test_dtcs_come_with_descriptions(service):
    codes = service.dtcs()["codes"]
    assert {c["code"] for c in codes} >= {"P0301", "P1349", "P0420"}
    assert all(c["description"] for c in codes)


def test_live_values_are_labelled(service):
    values = service.live()["values"]
    names = {v["name"] for v in values}
    assert "Обороты двигателя" in names
    rpm = next(v for v in values if v["name"] == "Обороты двигателя")
    assert rpm["unit"] == "об/мин"
    assert rpm["ecu"] == "7E8"


def test_write_is_refused_when_not_allowed(service):
    with pytest.raises(PermissionError, match="--allow-write"):
        service.clear()


def test_clear_works_when_allowed(writable_service):
    result = writable_service.clear()
    assert "Сброшено блоков" in result["message"]
    # Постоянный код обязан пережить сброс.
    assert result["remaining"] == 1
    assert writable_service.status()["mil_on"] is False


def test_bus_access_is_serialised(service):
    """Шина одна: параллельные запросы не должны перемешать обмен."""
    results, errors = [], []

    def worker():
        try:
            results.append(service.live()["values"])
        except Exception as exc:  # noqa: BLE001 -- нужен сам факт сбоя
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert len(results) == 6
    assert all(batch for batch in results)


# ---------------------------------------------------------------------- по HTTP


def test_page_is_served(server):
    with urllib.request.urlopen(server + "/", timeout=10) as response:
        body = response.read().decode()
    assert response.status == 200
    assert body == PAGE
    assert "Диагностика Toyota Avensis" in body


def test_page_is_self_contained(server):
    with urllib.request.urlopen(server + "/", timeout=10) as response:
        body = response.read().decode()
    assert "<script src" not in body
    assert "<link" not in body


def test_api_endpoints_answer(server):
    for path in ("/api/status", "/api/dtc", "/api/live"):
        status, payload = get(server + path)
        assert status == 200
        assert payload


def test_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(server + "/api/nonexistent", timeout=10)
    assert caught.value.code == 404


def test_clear_over_http_is_403_without_permission(server):
    request = urllib.request.Request(server + "/api/clear", method="POST")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=10)
    assert caught.value.code == 403
    assert "--allow-write" in json.loads(caught.value.read())["error"]
