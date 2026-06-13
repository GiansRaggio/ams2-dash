#!/usr/bin/env python3
"""
Bridge AMS2 -> Dashboard web.

Escucha el broadcast UDP de Automobilista 2 (puerto 5606, protocolo
"Project CARS 2"), parsea los paquetes de telemetria, timings y time-stats,
y reenvia el estado como JSON via WebSocket. Tambien sirve dashboard.html
por HTTP para abrirlo desde el navegador del celular.

Uso:
    pip install websockets   (o: sudo pacman -S python-websockets)
    python bridge.py

Luego en el celular (misma red): http://IP_DEL_PC:8080
Requisitos en AMS2 (Options -> System):
    UDP = On | UDP Protocol Version = Project CARS 2 | UDP Frequency = 1-4
"""
import asyncio
import http.server
import json
import os
import socket
import struct
import threading
import time

import websockets

UDP_PORT = 5606
WS_PORT = 8765
HTTP_PORT = 8080

# Estado compartido (un solo hilo asyncio lo escribe; HTTP no lo toca)
state = {
    "connected": False,       # llegan paquetes de AMS2
    "speed_kmh": 0,
    "rpm": 0,
    "max_rpm": 1,
    "gear": 0,                # -1 = R, 0 = N
    "throttle": 0,            # 0-100
    "brake": 0,               # 0-100
    "fuel_liters": 0.0,
    "fuel_capacity": 0,
    "split_ahead": None,      # segundos (None = sin dato)
    "split_behind": None,
    "event_remaining": None,  # segundos restantes de sesion
    "position": 0,
    "num_participants": 0,
    "current_lap": 0,
    "current_time": None,     # vuelta actual, segundos
    "last_lap": None,
    "best_lap": None,
    "water_temp": None,       # grados C
    "oil_temp": None,         # grados C
    "pit_limiter": False,     # speed limiter activo (sCarFlags bit 3)
    "abs_active": False,      # ABS interviniendo (bit 4)
    "tc_active": False,       # TC interviniendo (bit 6, SIN VERIFICAR)
    "engine_warning": False,  # aviso de motor (bit 2)
    "fuel_per_lap": None,     # litros/vuelta (promedio de las ultimas vueltas)
    "fuel_laps_left": None,   # vueltas estimadas con el combustible actual
}
_viewed = -1  # indice del participante que estamos manejando/viendo
_last_packet = 0.0  # epoch del ultimo paquete recibido

# Economia de combustible: se calcula al cruzar meta (delta de nivel por vuelta)
_fuel_lap_start = None  # litros al empezar la vuelta actual
_last_lap_seen = 0      # numero de vuelta visto por ultima vez
_fuel_per_lap = []      # consumo de las ultimas vueltas (litros)


def _f32(d, off):
    return struct.unpack_from("<f", d, off)[0]


def _u16(d, off):
    return struct.unpack_from("<H", d, off)[0]


def _s16(d, off):
    return struct.unpack_from("<h", d, off)[0]


def parse_telemetry(d):
    """sTelemetryData (packet type 0)."""
    global _viewed
    _viewed = struct.unpack_from("<b", d, 12)[0]
    state["brake"] = round(d[29] / 255 * 100)
    state["throttle"] = round(d[30] / 255 * 100)
    cap = d[28]
    state["fuel_capacity"] = cap
    state["fuel_liters"] = round(_f32(d, 32) * cap, 1)
    state["speed_kmh"] = round(_f32(d, 36) * 3.6)
    state["rpm"] = _u16(d, 40)
    state["max_rpm"] = max(_u16(d, 42), 1)
    g = d[45] & 0x0F
    state["gear"] = -1 if g == 15 else g

    flags = d[17]            # sCarFlags
    state["engine_warning"] = bool(flags & 0x04)
    state["pit_limiter"] = bool(flags & 0x08)
    # El bit 0x10 tambien queda encendido acelerando (ruidoso), por eso se
    # condiciona al pedal: ABS solo cuenta si se esta frenando, TC (0x40) solo
    # si se esta acelerando. Verificado en pista correlacionando flags y pedales.
    state["abs_active"] = bool(flags & 0x10) and state["brake"] > 15
    state["tc_active"] = bool(flags & 0x40) and state["throttle"] > 15
    state["oil_temp"] = _s16(d, 18)
    state["water_temp"] = _s16(d, 22)


def parse_timings(d):
    """sTimingsData (packet type 3)."""
    num = struct.unpack_from("<b", d, 12)[0]
    state["num_participants"] = max(num, 0)
    ev = _f32(d, 17)
    state["event_remaining"] = ev if ev >= 0 else None
    sa = _f32(d, 21)
    sb = _f32(d, 25)
    state["split_ahead"] = sa if sa >= 0 else None
    state["split_behind"] = sb if sb >= 0 else None

    if 0 <= _viewed < 32:
        base = 33 + _viewed * 32
        state["position"] = d[base + 14] & 0x7F
        state["current_lap"] = d[base + 21]
        ct = _f32(d, base + 22)
        state["current_time"] = ct if ct >= 0 else None
        _update_fuel_economy(state["current_lap"])


def _update_fuel_economy(lap):
    """Estima consumo y autonomia midiendo el nivel de combustible al cruzar meta.

    Se llama con el numero de vuelta actual; cuando incrementa, la diferencia de
    litros respecto al inicio de la vuelta anterior es lo consumido en esa vuelta.
    Se promedian las ultimas vueltas para suavizar.
    """
    global _fuel_lap_start, _last_lap_seen, _fuel_per_lap
    fuel = state["fuel_liters"]
    if lap <= 0:
        return
    if lap < _last_lap_seen:          # nueva sesion / reset de vueltas
        _fuel_per_lap = []
        _fuel_lap_start = fuel
        _last_lap_seen = lap
        state["fuel_per_lap"] = None
        state["fuel_laps_left"] = None
        return
    if lap > _last_lap_seen:          # se completo una vuelta
        if _fuel_lap_start is not None:
            used = _fuel_lap_start - fuel
            if used > 0.05:           # ignora reabastecimiento u out-lap raro
                _fuel_per_lap.append(used)
                del _fuel_per_lap[:-5]   # conserva solo las ultimas 5
        _fuel_lap_start = fuel
        _last_lap_seen = lap
    if _fuel_per_lap:
        avg = sum(_fuel_per_lap) / len(_fuel_per_lap)
        state["fuel_per_lap"] = round(avg, 2)
        state["fuel_laps_left"] = int(fuel / avg) if avg > 0 else None


def parse_timestats(d):
    """sTimeStatsData (packet type 7)."""
    if 0 <= _viewed < 32:
        base = 16 + _viewed * 32
        fast = _f32(d, base)
        last = _f32(d, base + 4)
        state["best_lap"] = fast if fast > 0 else None
        state["last_lap"] = last if last > 0 else None


class AMS2Protocol(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        global _last_packet
        if len(data) < 13:
            return
        ptype = data[10]
        try:
            if ptype == 0 and len(data) >= 60:
                parse_telemetry(data)
            elif ptype == 3 and len(data) >= 1063:
                parse_timings(data)
            elif ptype == 7 and len(data) >= 1040:
                parse_timestats(data)
            _last_packet = time.time()
        except (struct.error, IndexError):
            pass


CLIENTS = set()


async def ws_handler(ws):
    CLIENTS.add(ws)
    try:
        await ws.wait_closed()
    finally:
        CLIENTS.discard(ws)


async def broadcaster():
    """Envia el estado a ~15 Hz a todos los clientes conectados."""
    while True:
        await asyncio.sleep(1 / 15)
        state["connected"] = (time.time() - _last_packet) < 3
        msg = json.dumps(state)
        for ws in list(CLIENTS):
            try:
                await ws.send(msg)
            except websockets.ConnectionClosed:
                CLIENTS.discard(ws)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class _NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """Evita que el navegador del celular sirva una version cacheada del dash."""

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()


def serve_http():
    here = os.path.dirname(os.path.abspath(__file__))
    handler = lambda *a, **kw: _NoCacheHandler(*a, directory=here, **kw)
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler)
    httpd.serve_forever()


async def main():
    loop = asyncio.get_running_loop()
    # Mismo bind que la app SimDashboard: recibe el broadcast de AMS2
    await loop.create_datagram_endpoint(
        AMS2Protocol, local_addr=("0.0.0.0", UDP_PORT), reuse_port=True
    )
    threading.Thread(target=serve_http, daemon=True).start()
    ip = lan_ip()
    print(f"[bridge] UDP  : escuchando AMS2 en :{UDP_PORT}")
    print(f"[bridge] WS   : ws://{ip}:{WS_PORT}")
    print(f"[bridge] Dash : http://{ip}:{HTTP_PORT}  <- abrir en el celular")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await broadcaster()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bridge] detenido")
