#!/usr/bin/env python3
"""Bridge AMS2 (Shared Memory) -> Dashboard web.

Variante de bridge.py que lee la SHARED MEMORY de AMS2 ('$pcars2$', formato
Project CARS 2) en vez de escuchar el UDP. Misma salida exacta: estado por
WebSocket (:8765) + dashboard por HTTP (:8080), mismo JSON, mismo index.html.

Por que: activar el UDP en AMS2 causa stuttering (el juego serializa y emite un
paquete por frame). La shared memory el juego ya la escribe siempre; nosotros
solo la leemos -> sin costo para el juego, sin stutter.

Uso (Windows):
    .venv\\Scripts\\python.exe bridge_shm.py
Requisitos en AMS2 (Options -> System):
    Shared Memory = On  |  Shared Memory Type = Project CARS 2
(Es independiente del UDP: no hace falta activar el UDP.)
"""
import asyncio
import http.server
import json
import os
import socket
import threading
import time

import websockets

import ams2_shm
import ams2_dampers
import ams2_strategy

WS_PORT = 8765
HTTP_PORT = 8080
POLL_HZ = 30   # frecuencia de lectura de shared memory y de broadcast
LEADERBOARD_MAX = 16   # cuantos pilotos enviar a la pagina de tiempos

# Estados de juego en los que hay datos vivos del auto (no menu principal/replay-de-menu)
_LIVE_STATES = {
    ams2_shm.GAME_INGAME_PLAYING,
    ams2_shm.GAME_INGAME_PAUSED,
    ams2_shm.GAME_INGAME_INMENU_TIME_TICKING,
    ams2_shm.GAME_INGAME_RESTARTING,
    ams2_shm.GAME_INGAME_REPLAY,
}

# Estado compartido (mismo esquema que bridge.py -> index.html no cambia)
state = {
    "connected": False,
    "speed_kmh": 0,
    "rpm": 0,
    "max_rpm": 1,
    "gear": 0,
    "throttle": 0,
    "brake": 0,
    "fuel_liters": 0.0,
    "fuel_capacity": 0,
    "split_ahead": None,
    "split_behind": None,
    "event_remaining": None,
    "position": 0,
    "num_participants": 0,
    "current_lap": 0,
    "current_time": None,
    "last_lap": None,
    "best_lap": None,
    "water_temp": None,
    "oil_temp": None,
    "pit_limiter": False,
    "abs_active": False,
    "tc_active": False,
    "engine_warning": False,
    "fuel_per_lap": None,
    "fuel_laps_left": None,
    "leaderboard": [],   # [{pos, name, best, last, lap, me}] top-N por posicion
    "strategy": {"calibrating": True, "live": False, "mode": "none"},   # director de estrategia
}

# Director de estrategia (combustible/neumaticos/paradas). Se alimenta del mismo
# snapshot que update_state y mantiene su estado por vuelta.
strategy = ams2_strategy.StrategyEngine()

# Economia de combustible (identica a bridge.py: delta de nivel al cruzar meta)
_fuel_lap_start = None
_last_lap_seen = 0
_fuel_per_lap = []

# Deteccion de "señal viva" por avance de mSequenceNumber
_last_seq = -1
_last_seq_change = 0.0


def _update_fuel_economy(lap):
    """Estima consumo y autonomia midiendo el nivel de combustible al cruzar meta."""
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
                del _fuel_per_lap[:-5]
        _fuel_lap_start = fuel
        _last_lap_seen = lap
    if _fuel_per_lap:
        avg = sum(_fuel_per_lap) / len(_fuel_per_lap)
        state["fuel_per_lap"] = round(avg, 2)
        state["fuel_laps_left"] = int(fuel / avg) if avg > 0 else None


def update_state(d):
    """Vuelca un snapshot de shared memory (d) al dict global `state`."""
    global _last_seq, _last_seq_change
    now = time.monotonic()
    seq = d.mSequenceNumber
    if seq != _last_seq:
        _last_seq = seq
        _last_seq_change = now
    fresh = (now - _last_seq_change) < 1.5
    state["connected"] = bool(fresh and d.mGameState in _LIVE_STATES)

    cap = d.mFuelCapacity
    state["speed_kmh"] = round(d.mSpeed * 3.6)
    state["rpm"] = round(d.mRpm)
    state["max_rpm"] = max(round(d.mMaxRPM), 1)
    state["gear"] = d.mGear                      # -1 = R, 0 = N
    state["throttle"] = round(d.mUnfilteredThrottle * 100)
    state["brake"] = round(d.mUnfilteredBrake * 100)
    state["fuel_capacity"] = round(cap)
    state["fuel_liters"] = round(d.mFuelLevel * cap, 1)
    state["water_temp"] = round(d.mWaterTempCelsius)
    state["oil_temp"] = round(d.mOilTempCelsius)

    sa, sb = d.mSplitTimeAhead, d.mSplitTimeBehind
    state["split_ahead"] = sa if sa >= 0 else None
    state["split_behind"] = sb if sb >= 0 else None
    ev = d.mEventTimeRemaining
    state["event_remaining"] = ev if ev >= 0 else None

    state["num_participants"] = max(d.mNumParticipants, 0)
    v = d.mViewedParticipantIndex
    if 0 <= v < ams2_shm.STORED_PARTICIPANTS_MAX:
        p = d.mParticipantInfo[v]
        state["position"] = p.mRacePosition
        state["current_lap"] = p.mCurrentLap
        ct = d.mCurrentTime
        state["current_time"] = ct if ct >= 0 else None
        _update_fuel_economy(p.mCurrentLap)
    else:
        state["position"] = 0
        state["current_lap"] = 0
        state["current_time"] = None

    last, best = d.mLastLapTime, d.mBestLapTime
    state["last_lap"] = last if last > 0 else None
    state["best_lap"] = best if best > 0 else None

    # Flags del auto. mCarFlags usa el MISMO layout de bits que el sCarFlags del
    # UDP, asi que reproducimos exactamente la logica calibrada del dash original:
    # ABS/TC parpadean al intervenir -> se condicionan al pedal para limpiar ruido.
    # (Alternativa mas directa para ABS: d.mAntiLockActive, un bool dedicado.)
    flags = d.mCarFlags
    state["engine_warning"] = bool(flags & ams2_shm.CAR_ENGINE_WARNING)
    state["pit_limiter"] = bool(flags & ams2_shm.CAR_SPEED_LIMITER)
    state["abs_active"] = bool(flags & ams2_shm.CAR_ABS) and state["brake"] > 15
    state["tc_active"] = bool(flags & ams2_shm.CAR_TC) and state["throttle"] > 15

    # Tabla de tiempos por piloto (pagina "Tiempos"): vuelta rapida + ultima de cada uno.
    # mFastestLapTimes[i] / mLastLapTimes[i] estan indexados por indice de participante.
    lb = []
    for i in range(ams2_shm.STORED_PARTICIPANTS_MAX):
        pi = d.mParticipantInfo[i]
        if not pi.mIsActive or pi.mRacePosition <= 0:
            continue
        b = d.mFastestLapTimes[i]
        la = d.mLastLapTimes[i]
        lb.append({
            "pos": pi.mRacePosition,
            "name": pi.mName.decode("utf-8", "replace"),
            "best": round(b, 3) if b > 0 else None,
            "last": round(la, 3) if la > 0 else None,
            "lap": pi.mCurrentLap,
            "me": i == v,
        })
    lb.sort(key=lambda e: e["pos"])
    state["leaderboard"] = lb[:LEADERBOARD_MAX]

    # Director de estrategia: se alimenta del snapshot crudo (tiene su propio
    # estado por vuelta para fuel/gomas/paradas) y publica su payload.
    try:
        strategy.update(d)
        state["strategy"] = strategy.payload()
    except Exception:
        pass   # nunca tumbar el broadcast por un error del analizador de estrategia


# ---------------- WebSocket + HTTP ----------------
CLIENTS = set()
analyzer = None   # DamperAnalyzer (se crea en main); su hilo muestrea aparte
_shutdown = False  # lo activa el comando "stop_server" del dash -> pump termina y el proceso sale


async def ws_handler(ws):
    global _shutdown
    CLIENTS.add(ws)
    try:
        async for raw in ws:                     # comandos del cliente
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            cmd = msg.get("cmd")
            if cmd == "reset_dampers" and analyzer:
                analyzer.reset()
            elif cmd == "stop_server":
                print("[bridge-shm] detenido por el usuario (boton del dash)")
                _shutdown = True
            elif cmd == "set_race":               # formato de carrera manual (plan en practica)
                try:
                    strategy.set_race_plan(msg.get("mode"), float(msg.get("value")),
                                           int(msg.get("additional", 0) or 0))
                except (TypeError, ValueError):
                    pass
            elif cmd == "clear_race":
                strategy.clear_race_plan()
            elif cmd == "set_alllaps":            # contar vueltas anomalas/invalidas
                strategy.set_use_all_laps(bool(msg.get("on", True)))
    finally:
        CLIENTS.discard(ws)


async def _send_all(msg):
    for ws in list(CLIENTS):
        try:
            await ws.send(msg)
        except websockets.ConnectionClosed:
            CLIENTS.discard(ws)


async def _broadcast():
    if CLIENTS:
        await _send_all(json.dumps(state))


async def pump():
    """Lee shared memory a POLL_HZ y emite el estado a los clientes.

    Resiliente: si AMS2 no esta abierto al arrancar, reintenta abrir el mapeo
    cada 1 s (sirviendo "SIN SEÑAL" mientras tanto). Si la señal queda congelada
    >3 s (AMS2 cerrado/reiniciado/menu), suelta y reabre el mapeo para recuperar.
    """
    period = 1.0 / POLL_HZ
    reader = None
    next_retry = 0.0
    next_damper = 0.0
    while not _shutdown:
        await asyncio.sleep(period)
        now = time.monotonic()
        # Histograma de dampers (del analizador, hilo aparte): siempre a ~2 Hz,
        # independiente del reader del bridge -> tambien se emite en el lobby.
        if analyzer is not None and now >= next_damper:
            next_damper = now + 0.5
            await _send_all(json.dumps({"dampers": analyzer.payload()}))
        if reader is None:
            if now < next_retry:
                await _broadcast()
                continue
            try:
                reader = ams2_shm.Reader().open()
                print("[bridge-shm] shared memory conectada ($pcars2$)")
            except ams2_shm.SharedMemoryUnavailable:
                state["connected"] = False
                next_retry = now + 1.0
                await _broadcast()
                continue
        try:
            update_state(reader.snapshot())
        except OSError:
            reader.close()
            reader = None
            state["connected"] = False
            next_retry = now + 1.0
            await _broadcast()
            continue
        # Recuperacion ante AMS2 cerrado/reiniciado: si lleva >3 s congelada, reabrir.
        if (now - _last_seq_change) > 3.0:
            reader.close()
            reader = None
            next_retry = now + 1.0
        await _broadcast()


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

    def log_message(self, *a):
        pass  # silencioso


def serve_http():
    here = os.path.dirname(os.path.abspath(__file__))
    handler = lambda *a, **kw: _NoCacheHandler(*a, directory=here, **kw)
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler)
    httpd.serve_forever()


async def main():
    global analyzer
    analyzer = ams2_dampers.DamperAnalyzer().start()
    threading.Thread(target=serve_http, daemon=True).start()
    ip = lan_ip()
    print(f"[bridge-shm] Fuente: AMS2 Shared Memory ($pcars2$, Project CARS 2)")
    print(f"[bridge-shm] WS   : ws://{ip}:{WS_PORT}")
    print(f"[bridge-shm] Dash : http://{ip}:{HTTP_PORT}  <- abrir en el celular")
    print(f"[bridge-shm] (En AMS2: Options -> System -> Shared Memory = Project CARS 2)")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await pump()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[bridge-shm] detenido")
