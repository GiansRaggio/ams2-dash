#!/usr/bin/env python3
"""Servidor de mentira para revisar la pagina de GOMAS sin estar en pista.

Sirve el MISMO index.html por HTTP (:8081) y emite el MISMO JSON que bridge_shm por
WebSocket (:8766), pero alimentado con escenarios armados a mano. Corre en puertos
propios para no pelearse con el bridge real (:8080 / :8765), que puede estar vivo.

DOS DECISIONES QUE IMPORTAN:

1. El objeto `tyres` NO se inventa aca: se construye una instancia real de
   ams2_tyres.TyreAnalyzer, se le escriben las lecturas suavizadas y se llama a su
   payload(). Asi el mock emite por definicion lo mismo que emite el bridge (mismas
   claves, mismos redondeos, mismos veredictos pstat/tstat/camber/axle). Si alguien
   cambia payload(), el mock cambia solo -- no queda validando una mentira.

2. index.html tiene el puerto del WS hardcodeado (ws://<host>:8765). Como el mock vive
   en 8766, el HTML se reescribe AL VUELO al servirlo. El archivo del repo no se toca.

OJO: el onmessage del dash trata CUALQUIER mensaje sin `.dampers` como un frame de
estado completo. Por eso el mock nunca manda acks ni respuestas sueltas: solo frames.

Uso:
    .venv\\Scripts\\python.exe tools\\tyre_mock.py            # cicla escenarios cada 8 s
    .venv\\Scripts\\python.exe tools\\tyre_mock.py --scenario HOT   # arranca fijo en uno
Despues abrir http://localhost:8081 y entrar a la pagina de gomas.

Comandos por WS (para el QA, sin esperar el ciclo):
    {"cmd":"scenario","name":"DESVIADO"}   fija un escenario (congela el ciclo)
    {"cmd":"scenario","name":"auto"}       vuelve a ciclar
    {"cmd":"set_tyre_target","bar":1.90}   objetivo de presion (prueba el modal)
"""
import argparse
import asyncio
import http.server
import json
import math
import os
import socket
import sys
import threading
import time

import websockets

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ams2_tyres

sys.stdout.reconfigure(line_buffering=True)   # el log sirve aunque la salida vaya a un pipe

WS_PORT = 8766
HTTP_PORT = 8081
EMIT_HZ = 15          # el watchdog del dash reconecta si pasan >3 s sin frame
SCENARIO_SECS = 8.0   # cuanto dura cada escenario en modo ciclo
ANIM_SECS = 8.0       # periodo de la rampa de temperatura del out-lap

# Objetivo de presion en caliente (bar). Lo pisa el comando set_tyre_target.
TARGET_BAR = ams2_tyres.DEFAULT_TARGET_BAR

# Escenario activo: None = ciclar automatico.
PINNED = None
_pin_t0 = 0.0


# ---------------- escenarios ----------------
# Cada uno devuelve las lecturas CRUDAS por esquina (orden FL, FR, RL, RR); el payload
# real (delta, psi, spread, veredictos) lo deriva TyreAnalyzer.payload().
# press en bar, temps en C, wear en fraccion 0..1, brake en C.

def _sin_sesion(t):
    return dict(car="", compound="", live=False,
                press=[None] * 4, t_in=[None] * 4, t_mid=[None] * 4, t_out=[None] * 4,
                carcass=[None] * 4, brake=[None] * 4, wear=[None] * 4)


def _frio_outlap(t):
    """Carcasa subiendo 45 -> 75 C en ANIM_SECS: se ve marchar el marcador de zona y,
    al cruzar WARM_MIN (60), `warm` pasa a true y aparece el delta de presion."""
    k = (t % ANIM_SECS) / ANIM_SECS
    c = 45.0 + 30.0 * k
    mid = c - 10.0
    p = 1.58 + 0.14 * k                     # la presion sube con la temperatura
    return dict(car="Mercedes-AMG GT4", compound="Slick", live=True,
                press=[round(p + i * 0.01, 3) for i in range(4)],
                t_in=[mid + 4.0] * 4, t_mid=[mid] * 4, t_out=[mid - 2.0] * 4,
                carcass=[c, c - 1.5, c + 1.0, c - 0.5],
                brake=[110 + 90 * k, 108 + 90 * k, 90 + 70 * k, 88 + 70 * k],
                wear=[0.015, 0.014, 0.012, 0.013])


def _caliente_ok(t):
    """Todo en objetivo (delta 0.00, pstat ok) y en ventana termica (tstat ok)."""
    return dict(car="Porsche 911 GT3 R", compound="Slick Soft", live=True,
                press=[TARGET_BAR] * 4,
                t_in=[92, 91, 89, 90], t_mid=[87, 86, 84, 85], t_out=[86, 85, 83, 84],
                carcass=[88, 87, 90, 89],
                brake=[420, 415, 330, 325],
                wear=[0.21, 0.23, 0.18, 0.19])


def _desviado(t):
    """Deltas +0.10 (falta presion) y -0.06 (sobra), spread +14 en FL (mucho camber
    negativo -> warn) y -2 en FR (negativo -> falta camber)."""
    lo = TARGET_BAR - 0.10       # delta = target - press = +0.10  -> pstat "low"
    hi = TARGET_BAR + 0.06       # delta = -0.06                   -> pstat "high"
    return dict(car="BMW M4 GT3", compound="Slick Medium", live=True,
                press=[lo, hi, lo, hi],
                t_in=[100, 84, 92, 91], t_mid=[92, 85, 87, 86], t_out=[86, 86, 86, 85],
                carcass=[94, 88, 91, 90],
                brake=[455, 448, 340, 336],
                wear=[0.52, 0.31, 0.28, 0.27])


def _hot(t):
    return dict(car="Ferrari 488 GT3", compound="Slick Soft", live=True,
                press=[TARGET_BAR + 0.12] * 4,
                t_in=[112, 110, 104, 103], t_mid=[106, 105, 99, 98],
                t_out=[101, 100, 95, 94],
                carcass=[104, 104, 101, 100],
                brake=[610, 605, 470, 465],
                wear=[0.71, 0.69, 0.55, 0.56])


def _detenido(t):
    """live=false: el auto esta parado (box/grilla). Las lecturas siguen siendo validas
    -- reflejan el enfriamiento -- pero el dash avisa 'lectura en frio'."""
    return dict(car="Porsche 911 GT3 R", compound="Slick Soft", live=False,
                press=[TARGET_BAR - 0.04] * 4,
                t_in=[74, 73, 71, 72], t_mid=[70, 69, 67, 68], t_out=[68, 67, 65, 66],
                carcass=[72, 71, 69, 70],
                brake=[180, 176, 140, 138],
                wear=[0.34, 0.35, 0.30, 0.31])


def _parcial(t):
    """Campos null sueltos para probar los em-dashes: FL sin presion, FR sin zona media,
    RL sin carcasa (rompe el promedio del eje trasero), RR sin desgaste ni freno."""
    return dict(car="Chevrolet Camaro GT4.R", compound="Slick", live=True,
                press=[None, TARGET_BAR + 0.02, TARGET_BAR - 0.01, TARGET_BAR],
                t_in=[93, 90, None, 89], t_mid=[88, None, 84, 85],
                t_out=[87, 86, 83, None],
                carcass=[89, 88, None, 87],
                brake=[430, 425, 335, None],
                wear=[0.22, None, 0.19, None])


def _nombre_largo(t):
    """Overflow: nombre de auto y compuesto absurdamente largos en la barra de info."""
    return dict(car="Mercedes-AMG GT4 Evo Endurance Special Edition Nurburgring Test Car 2024",
                compound="Hipercompuesto Blando Experimental de Lluvia Extrema Ultra Slick",
                live=True,
                press=[TARGET_BAR + 0.01] * 4,
                t_in=[91, 90, 88, 89], t_mid=[86, 85, 83, 84], t_out=[85, 84, 82, 83],
                carcass=[87, 86, 89, 88],
                brake=[410, 405, 320, 318],
                wear=[0.25, 0.26, 0.20, 0.21])


def _box_frio(t):
    """Parado Y frio: goma nueva antes de salir. Es el PRIMER estado de cada sesion y
    el que faltaba -- _detenido tiene la carcasa a ~70 (warm=True), asi que solo ejercia
    la rama parado-y-caliente. Aca el banner NO debe decir 'se esta enfriando': esta
    goma nunca estuvo caliente, y lo que el piloto necesita saber es por que no hay delta."""
    return dict(car="Porsche 911 GT3 R", compound="Slick Soft", live=False,
                press=[TARGET_BAR - 0.22] * 4,
                t_in=[26, 26, 25, 25], t_mid=[25, 25, 24, 24], t_out=[24, 24, 23, 23],
                carcass=[28, 28, 27, 27],
                brake=[40, 40, 38, 38],
                wear=[0.0, 0.0, 0.0, 0.0])


SCENARIOS = [
    ("SIN_SESION", _sin_sesion),
    ("BOX_FRIO", _box_frio),
    ("FRIO_OUTLAP", _frio_outlap),
    ("CALIENTE_OK", _caliente_ok),
    ("DESVIADO", _desviado),
    ("HOT", _hot),
    ("DETENIDO", _detenido),
    ("PARCIAL", _parcial),
    ("NOMBRE_LARGO", _nombre_largo),
]
BY_NAME = dict(SCENARIOS)


# ---------------- construccion del payload de gomas ----------------
# base_dir inexistente a proposito: TyreAnalyzer no encuentra tyre_targets.json, arranca
# con el default y NUNCA escribe en el repo (el bridge real usa ese archivo).
_an = ams2_tyres.TyreAnalyzer(base_dir=os.path.join(HERE, "tools", "_mock_no_state"))


def _f(vals):
    """A float: en el bridge real todo pasa por el EMA y sale float. Escribir un int
    aca haria que payload() emitiera 14 donde el bridge emite 14.0 -- misma pantalla,
    pero el mock dejaria de ser byte-comparable con el real."""
    return [None if v is None else float(v) for v in vals]


def tyres_payload(scn, t):
    d = scn(t)
    _an._car = d["car"]
    _an._compound = d["compound"]
    _an._live = d["live"]
    _an._press = _f(d["press"])
    _an._t_in = _f(d["t_in"])
    _an._t_mid = _f(d["t_mid"])
    _an._t_out = _f(d["t_out"])
    _an._carcass = _f(d["carcass"])
    _an._brake = _f(d["brake"])
    _an._wear = _f(d["wear"])
    _an._targets = {d["car"]: TARGET_BAR}      # objetivo vigente, tambien con car=""
    return _an.payload()


def base_frame(t):
    """Frame completo: TODOS los campos que lee applyStatic() en index.html. Si falta
    uno el dash muestra basura y el QA valida cualquier cosa."""
    rpm = int(6100 + 2100 * math.sin(t * 1.8))
    thr = 92 if rpm > 6000 else 35
    brk = 0 if rpm > 6000 else 60
    return {
        "connected": True,
        "speed_kmh": round(150 + 70 * math.sin(t * 1.8)),
        "rpm": rpm,
        "max_rpm": 8300,
        "gear": 4,
        "throttle": thr,
        "brake": brk,
        "fuel_liters": round(max(4.0, 48.0 - (t % 600) * 0.03), 1),
        "fuel_capacity": 65,
        "split_ahead": 0.214,
        "split_behind": 1.103,
        "event_remaining": 1325.0,
        "position": 3,
        "num_participants": 20,
        "current_lap": 5,
        "current_time": round(t % 95, 3),
        "last_lap": 95.610,
        "best_lap": 94.880,
        "water_temp": 91,
        "oil_temp": 104,
        "pit_limiter": False,
        "abs_active": brk > 50,
        "tc_active": thr > 80,
        "engine_warning": False,
        "fuel_per_lap": 2.85,
        "fuel_laps_left": 16,
        "leaderboard": [],
        # Minimo coherente: renderStrategy corta temprano con live=false -> "Sin sesion".
        "strategy": {"calibrating": True, "live": False, "mode": "none", "session": ""},
    }


def build_frame():
    now = time.monotonic()
    if PINNED:
        name, scn = PINNED, BY_NAME[PINNED]
        t_scn = now - _pin_t0
    else:
        i = int(now / SCENARIO_SECS) % len(SCENARIOS)
        name, scn = SCENARIOS[i]
        t_scn = now % SCENARIO_SECS
    f = base_frame(now)
    f["tyres"] = tyres_payload(scn, t_scn)
    return name, json.dumps(f)


# ---------------- WebSocket ----------------
CLIENTS = set()


async def ws_handler(ws):
    global PINNED, _pin_t0, TARGET_BAR
    CLIENTS.add(ws)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            cmd = msg.get("cmd")
            if cmd == "scenario":
                name = str(msg.get("name", "")).upper()
                if name in ("AUTO", ""):
                    PINNED = None
                    print("[mock] escenario: AUTO (ciclando)")
                elif name in BY_NAME:
                    PINNED = name
                    _pin_t0 = time.monotonic()
                    print(f"[mock] escenario fijado: {name}")
                else:
                    print(f"[mock] escenario desconocido: {name}")
            elif cmd == "set_tyre_target":
                try:
                    v = float(msg.get("bar"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(v) and 0.5 <= v <= 4.0:
                    TARGET_BAR = round(v, 2)
                    print(f"[mock] objetivo -> {TARGET_BAR} bar")
            # Cualquier otro comando (set_race, set_telemetry, ...) se ignora en silencio:
            # NO responder nada, el dash tomaria la respuesta como frame de estado.
    finally:
        CLIENTS.discard(ws)


async def pump():
    period = 1.0 / EMIT_HZ
    last_name = None
    while True:
        await asyncio.sleep(period)
        name, msg = build_frame()
        if name != last_name:
            print(f"[mock] escenario: {name}")
            last_name = name
        for ws in list(CLIENTS):
            try:
                await ws.send(msg)
            except websockets.ConnectionClosed:
                CLIENTS.discard(ws)


# ---------------- HTTP ----------------
class _MockHandler(http.server.SimpleHTTPRequestHandler):
    """Sin cache (mismo motivo que el bridge: el celular sirve versiones viejas) y con
    el puerto del WebSocket reescrito 8765 -> 8766 al vuelo, para que el dash servido
    aca hable con el mock y no con el bridge real."""

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
                    html = f.read()
            except OSError:
                self.send_error(500, "no se pudo leer index.html")
                return
            html = html.replace("location.hostname}:8765", f"location.hostname}}:{WS_PORT}")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


def serve_http():
    handler = lambda *a, **kw: _MockHandler(*a, directory=HERE, **kw)
    http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler).serve_forever()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def main():
    threading.Thread(target=serve_http, daemon=True).start()
    ip = lan_ip()
    print(f"[mock] MOCK de gomas (no toca AMS2 ni el bridge real de :8765/:8080)")
    print(f"[mock] WS   : ws://{ip}:{WS_PORT}")
    print(f"[mock] Dash : http://localhost:{HTTP_PORT}   (o http://{ip}:{HTTP_PORT})")
    print(f"[mock] Escenarios: {', '.join(n for n, _ in SCENARIOS)}")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await pump()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", help="arrancar fijo en un escenario (si no, cicla)")
    args = ap.parse_args()
    if args.scenario:
        n = args.scenario.upper()
        if n not in BY_NAME:
            sys.exit(f"escenario desconocido: {n}\nvalidos: {', '.join(BY_NAME)}")
        PINNED = n
        _pin_t0 = time.monotonic()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[mock] detenido")
