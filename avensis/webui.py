"""Локальный веб-интерфейс поверх диагностической сессии.

Поднимает небольшой сервер на своей машине и отдаёт одну страницу: состояние
Check Engine, список ошибок и параметры, обновляющиеся сами. Страница
самодостаточна -- ни одного внешнего файла, интернет не нужен.

Про доступ. Сервер по умолчанию слушает только ``127.0.0.1``, то есть виден
исключительно с этого компьютера. Пароля нет, поэтому открывать его наружу
не следует: любой, кто до него дотянется, получит доступ к машине. Операции
записи (сброс ошибок) выключены, пока их явно не разрешили.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional

from avensis import pids as pid_module
from avensis.elm327 import Elm327
from avensis.framing import AdapterError
from avensis.obd import ObdSession

log = logging.getLogger(__name__)

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Диагностика Toyota Avensis</title>
<style>
  :root {
    color-scheme: light dark;
    --bg:#f4f5f7; --card:#fff; --fg:#16181d; --muted:#6b7280; --border:#e2e5ea;
    --ok:#0f9d58; --warn:#e8710a; --bad:#d93025; --accent:#1a73e8;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0f1115; --card:#171a20; --fg:#e8eaed; --muted:#9aa0a6; --border:#282c34;
      --ok:#5bb974; --warn:#fdd663; --bad:#f28b82; --accent:#8ab4f8;
    }
  }
  *{box-sizing:border-box}
  body{margin:0;padding:20px;background:var(--bg);color:var(--fg);
       font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  main{max-width:980px;margin:0 auto}
  h1{font-size:20px;margin:0 0 14px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:12px;
        padding:16px 18px;margin-bottom:14px}
  .card h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600;
           text-transform:uppercase;letter-spacing:.04em}
  .lamp{display:flex;align-items:center;gap:12px;font-size:17px;font-weight:600}
  .dot{width:14px;height:14px;border-radius:50%;background:var(--muted);flex:none}
  .dot.on{background:var(--bad);box-shadow:0 0 10px var(--bad)}
  .dot.off{background:var(--ok)}
  .facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:2px 24px}
  .facts div{display:flex;justify-content:space-between;align-items:baseline;gap:14px;
             padding:5px 0;border-bottom:1px solid var(--border)}
  .facts span:first-child{color:var(--muted);white-space:nowrap}
  .facts span:last-child{text-align:right}
  /* Длинные значения (описание двигателя) занимают строку целиком и
     выравниваются по левому краю -- иначе перенос выглядит рвано. */
  .facts div.wide{grid-column:1/-1;display:block}
  .facts div.wide span:last-child{display:block;text-align:left;margin-top:1px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
  .tile{border:1px solid var(--border);border-radius:9px;padding:10px 12px}
  .tile .name{color:var(--muted);font-size:12px;line-height:1.3;min-height:2.6em}
  .tile .value{font-size:21px;font-weight:600;margin-top:4px;
               font-variant-numeric:tabular-nums}
  .tile .unit{font-size:13px;font-weight:400;color:var(--muted);margin-left:3px}
  ul.dtc{list-style:none;margin:0;padding:0}
  ul.dtc li{padding:9px 0;border-bottom:1px solid var(--border)}
  ul.dtc li:last-child{border-bottom:0}
  .code{font-weight:700;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
  .badge{font-size:11px;padding:2px 7px;border-radius:20px;border:1px solid var(--border);
         color:var(--muted);margin-left:8px;white-space:nowrap}
  .desc{color:var(--muted);font-size:14px;margin-top:2px}
  button{font:inherit;padding:8px 15px;border-radius:8px;border:1px solid var(--border);
         background:var(--card);color:var(--fg);cursor:pointer}
  button:hover:not(:disabled){border-color:var(--accent);color:var(--accent)}
  button:disabled{opacity:.45;cursor:not-allowed}
  button.danger:hover:not(:disabled){border-color:var(--bad);color:var(--bad)}
  .bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .status{color:var(--muted);font-size:13px}
  .error{color:var(--bad)}
  .empty{color:var(--muted)}
</style>
</head>
<body>
<main>
  <h1>Диагностика Toyota Avensis</h1>

  <div class="card">
    <div class="lamp"><span id="dot" class="dot"></span><span id="lamp">подключение…</span></div>
  </div>

  <div class="card">
    <h2>Автомобиль</h2>
    <div class="facts" id="facts"></div>
  </div>

  <div class="card">
    <h2>Коды неисправностей</h2>
    <ul class="dtc" id="dtc"><li class="empty">читаю…</li></ul>
    <div class="bar" style="margin-top:14px">
      <button id="reread">Перечитать ошибки</button>
      <button id="clear" class="danger" hidden>Стереть ошибки</button>
      <span class="status" id="dtcStatus"></span>
    </div>
  </div>

  <div class="card">
    <h2>Параметры</h2>
    <div class="grid" id="live"></div>
    <div class="bar" style="margin-top:14px">
      <label><input type="checkbox" id="auto" checked> обновлять автоматически</label>
      <span class="status" id="liveStatus"></span>
    </div>
  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function renderStatus(data) {
  $("dot").className = "dot " + (data.mil_on ? "on" : "off");
  $("lamp").textContent = data.mil_on
    ? "Check Engine горит — есть активные неисправности"
    : "Check Engine не горит";

  const rows = [
    ["VIN", data.vin || "не прочитан"],
    ["Напряжение сети", data.voltage != null ? data.voltage + " В" : "—"],
    ["Протокол шины", data.protocol],
    ["Адаптер", data.adapter],
    ["Блоки на шине", (data.ecus || []).join(", ") || "—"],
    ["Двигатель", data.engine || "не определён"],
  ];
  $("facts").innerHTML = rows
    .map(([k, v]) => {
      // Значения длиннее строки таблицы верстаются во всю ширину.
      const wide = String(v).length > 34 ? ' class="wide"' : "";
      return `<div${wide}><span>${esc(k)}</span><span>${esc(v)}</span></div>`;
    })
    .join("");
}

function renderDtc(codes) {
  if (!codes.length) {
    $("dtc").innerHTML = '<li class="empty">Ошибок не найдено.</li>';
    return;
  }
  $("dtc").innerHTML = codes.map((c) => `
    <li>
      <span class="code">${esc(c.code)}</span>
      <span class="badge">${esc(c.status)}</span>
      <span class="badge">${esc(c.ecu)}</span>
      <div class="desc">${esc(c.description)}</div>
    </li>`).join("");
}

function renderLive(items) {
  $("live").innerHTML = items.map((item) => `
    <div class="tile">
      <div class="name">${esc(item.name)}${item.ecu ? " · " + esc(item.ecu) : ""}</div>
      <div class="value">${esc(item.value)}<span class="unit">${esc(item.unit || "")}</span></div>
    </div>`).join("");
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"]/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
}

async function loadStatus() {
  try {
    renderStatus(await api("/api/status"));
  } catch (err) {
    $("lamp").textContent = "нет связи с машиной: " + err.message;
    $("lamp").className = "error";
  }
}

async function loadDtc() {
  $("dtcStatus").textContent = "читаю…";
  try {
    const data = await api("/api/dtc");
    renderDtc(data.codes);
    $("dtcStatus").textContent = "обновлено " + new Date().toLocaleTimeString();
  } catch (err) {
    $("dtcStatus").textContent = "ошибка: " + err.message;
  }
}

let busy = false;
async function loadLive() {
  if (busy) return;
  busy = true;
  try {
    const data = await api("/api/live");
    renderLive(data.values);
    $("liveStatus").textContent = "обновлено " + new Date().toLocaleTimeString();
  } catch (err) {
    $("liveStatus").textContent = "ошибка: " + err.message;
  } finally {
    busy = false;
  }
}

$("reread").onclick = loadDtc;
$("clear").onclick = async () => {
  if (!confirm(
      "Стереть коды неисправностей?\\n\\n" +
      "Вместе с кодами обнулятся мониторы готовности, и до окончания ездового " +
      "цикла машина не пройдёт инструментальный контроль. Саму неисправность " +
      "сброс не устраняет — код вернётся.")) return;
  $("clear").disabled = true;
  $("dtcStatus").textContent = "стираю…";
  try {
    const data = await api("/api/clear", { method: "POST" });
    $("dtcStatus").textContent = data.message;
    await loadDtc();
    await loadStatus();
  } catch (err) {
    $("dtcStatus").textContent = "ошибка: " + err.message;
  } finally {
    $("clear").disabled = false;
  }
};

(async () => {
  const info = await api("/api/status");
  if (info.write_allowed) $("clear").hidden = false;
  renderStatus(info);
  await loadDtc();
  await loadLive();
  setInterval(() => { if ($("auto").checked) loadLive(); }, 1500);
  setInterval(loadStatus, 15000);
})();
</script>
</body>
</html>
"""


class DiagnosticService:
    """Диагностическая сессия, разделяемая между запросами страницы.

    Шина последовательная и одна: два запроса одновременно её перепутают.
    Поэтому любое обращение к адаптеру проходит через замок.
    """

    def __init__(self, elm: Elm327, engine_code: Optional[str] = None, allow_write: bool = False):
        self.elm = elm
        self.obd = ObdSession(elm)
        self.allow_write = allow_write
        self._lock = threading.Lock()
        self._engine_code = engine_code
        self._vin: Optional[str] = None
        self._engine_text: Optional[str] = None
        self._live_pids: Optional[List[int]] = None

    def _prepare(self) -> None:
        """Один раз узнать то, что за поездку не меняется."""
        if self._live_pids is not None:
            return
        self._vin = self.obd.read_vin()
        try:
            detection = self.obd.detect_engine(self._engine_code)
            self._engine_text = detection.describe()
            self._live_pids = detection.live_pids
        except ValueError:
            self._live_pids = list(pid_module.LIVE_DEFAULT)

    def status(self) -> Dict[str, object]:
        with self._lock:
            self._prepare()
            statuses = self.obd.read_monitor_status()
            try:
                voltage = self.elm.read_voltage()
            except AdapterError:
                voltage = None
            return {
                "vin": self._vin,
                "engine": self._engine_text,
                "protocol": self.elm.protocol_name,
                "adapter": self.elm.adapter_id,
                "voltage": voltage,
                "mil_on": any(s.mil_on for s in statuses.values()),
                "ecus": sorted(statuses),
                "write_allowed": self.allow_write,
            }

    def dtcs(self) -> Dict[str, object]:
        with self._lock:
            codes = self.obd.read_all_dtcs()
        return {
            "codes": [
                {"code": c.code, "description": c.description, "status": c.status, "ecu": c.ecu}
                for c in codes
            ]
        }

    def live(self) -> Dict[str, object]:
        with self._lock:
            self._prepare()
            snapshot = self.obd.read_live_snapshot(self._live_pids)

        values = []
        for pid, answers in snapshot.items():
            entry = pid_module.get_pid(pid)
            for ecu, value in answers.items():
                if isinstance(value, dict):
                    # Составные параметры показываем одной строкой.
                    shown = ", ".join(f"{k}: {v}" for k, v in value.items() if v is not None)
                    unit = ""
                else:
                    shown, unit = value, (entry.unit if entry else "")
                values.append({
                    "name": entry.name if entry else f"Параметр {pid:02X}",
                    "ecu": ecu,
                    "value": shown,
                    "unit": unit,
                })
        return {"values": values}

    def clear(self) -> Dict[str, object]:
        if not self.allow_write:
            raise PermissionError(
                "Запись выключена. Перезапусти сервер с флагом --allow-write, "
                "если сброс ошибок действительно нужен."
            )
        with self._lock:
            result = self.obd.clear_dtcs()
            remaining = self.obd.read_all_dtcs()
        cleared = sum(1 for ok in result.values() if ok)
        message = f"Сброшено блоков: {cleared} из {len(result)}"
        if remaining:
            message += f"; осталось кодов: {len(remaining)} (постоянные или активные)"
        return {"message": message, "remaining": len(remaining)}


def make_handler(service: DiagnosticService) -> type:
    """Собрать обработчик запросов, привязанный к конкретной сессии."""

    routes: Dict[str, Callable[[], Dict[str, object]]] = {
        "/api/status": service.status,
        "/api/dtc": service.dtcs,
        "/api/live": service.live,
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "avensis"

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Страница целиком локальная, встраивать её куда-либо незачем.
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: Dict[str, object], code: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _run(self, action: Callable[[], Dict[str, object]]) -> None:
            try:
                self._send_json(action())
            except PermissionError as exc:
                self._send_json({"error": str(exc)}, code=403)
            except Exception as exc:  # страница должна показать причину, а не белый экран
                log.warning("Запрос %s не выполнен: %s", self.path, exc)
                self._send_json({"error": str(exc)}, code=502)

        def do_GET(self) -> None:  # noqa: N802 -- имя задано базовым классом
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path in routes:
                self._run(routes[self.path])
            else:
                self._send_json({"error": "нет такого адреса"}, code=404)

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/clear":
                self._run(service.clear)
            else:
                self._send_json({"error": "нет такого адреса"}, code=404)

    return Handler


def serve(
    elm: Elm327,
    host: str = "127.0.0.1",
    port: int = 8327,
    engine_code: Optional[str] = None,
    allow_write: bool = False,
) -> None:
    """Поднять веб-интерфейс и работать, пока не прервут."""
    service = DiagnosticService(elm, engine_code=engine_code, allow_write=allow_write)
    server = ThreadingHTTPServer((host, port), make_handler(service))

    print(f"Интерфейс открыт: http://{host}:{port}/")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("  ВНИМАНИЕ: сервер доступен по сети и не защищён паролем.")
        print("  Любой, кто до него дотянется, получит доступ к твоей машине.")
    print(f"  Сброс ошибок: {'разрешён' if allow_write else 'выключен (флаг --allow-write)'}")
    print("  Остановить -- Ctrl+C\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()
