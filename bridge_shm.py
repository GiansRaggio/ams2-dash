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
import faulthandler
import http.server
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time

import websockets

import ams2_shm
import ams2_dampers
import ams2_strategy
import ams2_telemetry
import ams2_tyres

# El visor de telemetria es ACCESORIO: si su modulo no carga, el dash de manejar
# tiene que seguir andando igual. Por eso el import es blando y no un import
# normal arriba -- un error aca no puede dejarte sin instrumentos en pista.
try:
    import ams2_analysis
except Exception as _e:                              # noqa: BLE001
    ams2_analysis = None
    _ANALISIS_ERR = _e

# La bandeja de entrega (subir sesiones a la escuela) es igual de accesoria: si el
# modulo falta o revienta al importar, el dash sigue y el estado dice "off".
try:
    import entrega_cola
except Exception as _e:                              # noqa: BLE001
    entrega_cola = None
    _ENTREGA_ERR = _e

WS_PORT = 8765
HTTP_PORT = 8080

# ---------------- log propio y watchdog del event loop ----------------
# El bridge escribe SU log el mismo, sin depender de como lo hayan lanzado. Antes salia
# por stdout y quedaba en bridge.log solo si el lanzador redirigia; lanzado con
# Start-Process (ventana propia) se perdia entero, justo cuando mas se necesitaba.
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.log")
# Segundos sin latido del pump para declararlo colgado. El pump late a POLL_HZ y sigue
# latiendo aunque AMS2 este cerrado (ahi reintenta abrir la memoria), asi que un silencio
# de 30 s no es "el juego no esta": es que el loop dejo de correr.
STALL_S = 30.0
WATCHDOG_EVERY_S = 5.0
MAX_REINICIOS = 3          # tope para no entrar en bucle de reinicios
_hb = time.monotonic()     # ultimo latido del pump (lo lee el hilo del watchdog)
# [ya_estuvo_conectado, monotonic de la ultima conexion]: para no escribir una linea de
# "conectada" por cada reintento cuando AMS2 esta en menus (ver el pump).
_conectado_antes = [False, 0.0]


def log(msg):
    """A consola Y a archivo. Sin dependencias: esto tiene que funcionar incluso cuando
    todo lo demas esta roto."""
    linea = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(linea, flush=True)
    except Exception:                       # noqa: BLE001 - consola cerrada
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass


def _watchdog():
    """Hilo del SO --no asyncio-- que vigila que el event loop siga vivo.

    El modo de falla que motiva esto: el proceso queda VIVO y reteniendo los puertos
    8765/8080, pero el loop deja de correr. Desde afuera se ve identico a "funcionando"
    (netstat muestra LISTENING) y en el telefono el dash se ve conectado y CONGELADO;
    ademas cualquier relanzamiento choca con WinError 10048 contra el zombi. Medido en
    vivo: puertos escuchando y el handshake del WebSocket agotando el tiempo.

    Al detectarlo vuelca el stack de TODOS los hilos --que es la unica forma de saber
    donde se colgo, porque no hay excepcion ni traceback-- y reinicia el proceso en el
    lugar. El contador de reinicios va por variable de entorno para sobrevivir al execv
    y no quedar en bucle."""
    global _hb
    while True:
        time.sleep(WATCHDOG_EVERY_S)
        atraso = time.monotonic() - _hb
        if atraso < STALL_S:
            continue
        n = int(os.environ.get("AMS2_BRIDGE_REINICIOS", "0")) + 1
        log(f"[watchdog] EVENT LOOP COLGADO: {atraso:.0f}s sin latido del pump. "
            f"Volcando stacks y reiniciando (intento {n}/{MAX_REINICIOS}).")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write("--- stacks de todos los hilos al momento del cuelgue ---\n")
                f.flush()
                faulthandler.dump_traceback(file=f, all_threads=True)
                f.write("--- fin del volcado ---\n")
        except (OSError, RuntimeError):
            pass
        if n > MAX_REINICIOS:
            log("[watchdog] demasiados reinicios seguidos: salgo para no entrar en bucle.")
            os._exit(1)
        os.environ["AMS2_BRIDGE_REINICIOS"] = str(n)
        try:
            # execv reemplaza el proceso: mismos puertos, estado limpio, sin dejar zombi
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except OSError as e:
            log(f"[watchdog] no pude reiniciar ({e}); salgo.")
            os._exit(1)


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
    "tyres": {"live": False, "corners": []},   # gomas: temps por zona, presion, desgaste
    "entrega": {"estado": "off"},   # bandeja de entrega a la escuela; "off" = modulo ausente
}

# Director de estrategia (combustible/neumaticos/paradas). Se alimenta del mismo
# snapshot que update_state y mantiene su estado por vuelta.
strategy = ams2_strategy.StrategyEngine()

# Gomas: temperatura por zona (interior/medio/exterior), presion en caliente y
# diferencial contra el objetivo. El desgaste se lo pide a strategy (unica verdad).
tyres = ams2_tyres.TyreAnalyzer()

# Logger de telemetria por vuelta (corre su propio hilo+reader; se crea en main()).
telemetry = None

# Bandeja de entrega (hilo propio; se crea en main() si el modulo cargo y hay logger).
cola = None
# El jugador esta manejando ahora mismo. Lo lee la bandeja para NO empaquetar ni subir
# con el auto en pista: el zip+sha256 es CPU en Python y se lleva el GIL, y esta medido
# que la presion del dash tira el juego de 160 a 60 fps (ver ams2_telemetry).
_EN_PISTA = False

# Anclaje al auto del JUGADOR (mismo helper que recorder/estrategia): en MP la camara
# (mViewedParticipantIndex) sigue a otros autos, no a ti. Se lee una vez al arrancar.
_player_name = ams2_shm.read_player_name()


class SpeechServer:
    """Habla las alertas de estrategia por la voz de Windows (SAPI), sin depender de que haya un
    navegador/iPad abierto. Edge-trigger como el TTS del dash: cada alerta suena UNA vez al
    aparecer y vuelve a poder sonar si desaparece y reaparece; la mas urgente primero. No bloquea
    el loop (subprocess fire-and-forget)."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._announced = set()

    def handle(self, strat):
        alerts = (strat or {}).get("alerts") or []
        active = {a.get("key") for a in alerts}
        self._announced &= active                 # re-arme: las que ya no estan podran re-sonar
        if not self.enabled:
            return
        fresh = [a for a in alerts if a.get("key") not in self._announced]
        if not fresh:
            return
        a = max(fresh, key=lambda x: x.get("level", 0))   # la mas urgente primero
        self._announced.add(a.get("key"))
        self._speak(a.get("say", ""), a.get("level", 0))

    def _speak(self, text, level=0):
        if not text:
            return
        # SAPI via PowerShell: sin dependencias pip. Texto por stdin -> sin escaping/inyeccion.
        # Para cortar el audio del juego: beep de atencion ('click de radio') + voz a volumen 100,
        # mas lenta = mas clara; las CRITICAS (level>=3) se repiten para no perderlas. Fire-and-forget.
        beep = "[console]::beep(1100,120);[console]::beep(1500,180);"
        speak = "$s.Speak($t);" * (2 if level >= 3 else 1)
        ps = ("$t=[Console]::In.ReadToEnd();"
              "Add-Type -AssemblyName System.Speech;"
              "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
              "$s.Volume=100; $s.Rate=-2;"
              "$v=$s.GetInstalledVoices()|?{$_.VoiceInfo.Culture.Name -like 'es-*'}|select -First 1;"
              "if($v){$s.SelectVoice($v.VoiceInfo.Name)};"
              + beep + speak)
        try:
            p = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            p.stdin.write(text.encode("utf-8"))
            p.stdin.close()
        except Exception:
            pass


# Voz del ingeniero en el PC (sin navegador). AMS2_VOICE=0 para apagarla.
# APAGADO POR DEFECTO desde que el dash se distribuye a los alumnos: la voz
# nunca funciono del todo bien y un TTS hablando solo en el PC de un alumno es
# un susto + un ticket de soporte. Quien la quiera (nosotros) la prende con la
# variable de entorno AMS2_VOICE=1; ya no hay forma de activarla por accidente.
speech = SpeechServer(enabled=os.environ.get("AMS2_VOICE", "0") == "1")

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


def _frame_ok(d):
    """Valida que el frame no sea basura antes de volcarlo (rubrica F1/F2).

    AMS2 comparte el nombre del archivo mapeado con Project CARS 2 y a veces la
    memoria queda corrupta: mVersion=0, nombre de auto vacio, arrays stale. Es la
    causa #1 documentada de 'el dashboard miente'. Tambien rechazamos valores
    fuera de rango fisico. Si el frame no pasa, se conserva el ultimo bueno.
    """
    if d.mVersion != ams2_shm.SHARED_MEMORY_VERSION:
        return False
    if not d.mCarName.split(b"\x00")[0]:        # nombre de auto propio vacio
        return False
    if not (0.0 <= d.mFuelLevel <= 1.05):        # fraccion de combustible fuera de rango
        return False
    return True


def update_state(d):
    """Vuelca un snapshot de shared memory (d) al dict global `state`."""
    global _last_seq, _last_seq_change, _EN_PISTA
    now = time.monotonic()
    if not _frame_ok(d):                         # frame corrupto -> conservar ultimo bueno
        state["connected"] = False
        return
    seq = d.mSequenceNumber
    if seq != _last_seq:
        _last_seq = seq
        _last_seq_change = now
    fresh = (now - _last_seq_change) < 1.5
    state["connected"] = bool(fresh and d.mGameState in _LIVE_STATES)
    _EN_PISTA = bool(fresh and d.mGameState == ams2_shm.GAME_INGAME_PLAYING)

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
    v = ams2_shm.player_index(d, _player_name)   # TU auto (pos/vuelta/economia/leaderboard), no el visto
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
        # Sectores POR PILOTO: el juego ya los publica (mFastestSectorNTimes[i]). Con esto la
        # comparacion "donde le pierdo al mas rapido" sale EN VIVO, sin servidor ni subir nada.
        s = [d.mFastestSector1Times[i], d.mFastestSector2Times[i], d.mFastestSector3Times[i]]
        lb.append({
            "pos": pi.mRacePosition,
            "name": pi.mName.decode("utf-8", "replace"),
            "best": round(b, 3) if b > 0 else None,
            "last": round(la, 3) if la > 0 else None,
            "lap": pi.mCurrentLap,
            "me": i == v,
            "sec": [round(x, 3) if x > 0 else None for x in s],
            "inv": bool(d.mLapsInvalidated[i]),
        })
    lb.sort(key=lambda e: e["pos"])
    state["leaderboard"] = lb[:LEADERBOARD_MAX]

    # Director de estrategia: se alimenta del snapshot crudo (tiene su propio
    # estado por vuelta para fuel/gomas/paradas) y publica su payload.
    # surf_alive va en la direccion contraria a wear_vec/eol_vec (abajo): el detector
    # de crossover lee mTyreTemp, que AMS2 deja muerto en la mayoria de las sesiones de
    # lluvia, y quien mide eso es TyreAnalyzer. Se pasa el valor del frame ANTERIOR
    # (tyres.update corre despues); da igual, es un detector de 60-120 s.
    try:
        strategy.update(d, surf_alive=tyres.surf_alive())
        state["strategy"] = strategy.payload()
    except Exception:
        pass   # nunca tumbar el broadcast por un error del analizador de estrategia
    try:
        speech.handle(state.get("strategy"))   # voz del ingeniero en el PC (sin navegador)
    except Exception:
        pass

    # Gomas: mismo snapshot. El desgaste, las vueltas que le quedan a cada goma (EOL) y
    # las del stint llegan ya resueltos por strategy (que detecta la direccion de
    # mTyreWear y lleva el EMA de ritmo) para no tener dos lecturas del mismo dato.
    try:
        tyres.update(d, wear=strategy.wear_vec(d))
        state["tyres"] = tyres.payload(eol=strategy.eol_vec(d),
                                       stint=strategy.stint_laps())
    except Exception:
        pass   # nunca tumbar el broadcast por un error del analizador de gomas
    if telemetry is not None:
        try:
            state["telemetry"] = telemetry.status()
        except Exception:
            pass
    if cola is not None:
        try:
            state["entrega"] = cola.status()     # copia precomputada: cero I/O a 30 Hz
        except Exception:
            pass   # nunca tumbar el broadcast por la bandeja


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
                log("[bridge-shm] detenido por el usuario (boton del dash)")
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
            elif cmd == "set_tyre_target":        # presion objetivo en caliente (bar), por auto
                try:
                    tyres.set_target(msg.get("bar"))
                except (TypeError, ValueError):
                    pass
            elif cmd == "set_telemetry":          # grabar telemetria: off / summary / full
                if telemetry is not None:
                    m = msg.get("mode")
                    if m in ("off", "summary", "full"):
                        telemetry.set_mode(m)
                    else:
                        telemetry.set_enabled(bool(msg.get("on", True)))
            # Bandeja de entrega. Los tres comandos solo ENCOLAN o cambian config: el
            # trabajo pesado lo hace el hilo de la bandeja, nunca este handler (que vive
            # en el event loop y no puede bloquearse sin que el watchdog reinicie todo).
            elif cmd == "entregar":
                if cola is not None:
                    try:
                        cola.entregar_ahora(msg.get("carpeta") or None)
                    except Exception as e:
                        log(f"[entrega] entregar: {e}")
            elif cmd == "set_entrega_auto":
                if cola is not None:
                    try:
                        cola.set_auto(bool(msg.get("on", False)))
                    except Exception as e:
                        log(f"[entrega] set_auto: {e}")
            elif cmd == "entrega_recargar":
                if cola is not None:
                    try:
                        cola.recargar_config()
                    except Exception as e:
                        log(f"[entrega] recargar: {e}")
    finally:
        CLIENTS.discard(ws)


async def _send_all(msg):
    for ws in list(CLIENTS):
        try:
            await ws.send(msg)
        except websockets.ConnectionClosed:
            CLIENTS.discard(ws)


def _sanear(x):
    """Reemplaza los floats no finitos por None, recursivamente."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _sanear(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_sanear(v) for v in x]
    return x


async def _broadcast():
    if CLIENTS:
        # allow_nan=False NO es opcional: json.dumps serializa un NaN como
        # `NaN`, que es JSON invalido -- el JSON.parse del navegador tira y el
        # dash ENTERO se congela, no solo el widget culpable (ya paso; ver
        # ESTADO.md). Los modulos validan lo suyo, pero en la maquina de un
        # alumno cualquier campo suelto de la SHM (otro auto, otro mod) puede
        # traer basura. El camino normal no paga nada; solo si aparece un no
        # finito se sanea y se emite igual, en vez de congelar el telefono.
        try:
            msg = json.dumps(state, allow_nan=False)
        except ValueError:
            msg = json.dumps(_sanear(state))
        await _send_all(msg)


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
    global _hb
    while not _shutdown:
        await asyncio.sleep(period)
        now = time.monotonic()
        _hb = now                    # latido para el watchdog (ver _watchdog arriba)
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
                # Solo al RECONECTAR de verdad, no en cada reintento. Con AMS2 en menus o
                # cerrado la memoria se congela, el pump la reabre cada ~4 s y esto
                # escribia una linea POR SEGUNDO: 12.807 lineas en un dia, que ahogan el
                # unico rastro que sirve cuando hay que diagnosticar algo. Se avisa la
                # reconexion cuando venia de estar caida un rato, no el churn normal.
                if not _conectado_antes[0] or (now - _conectado_antes[1]) > 30.0:
                    log("[bridge-shm] shared memory conectada ($pcars2$)")
                _conectado_antes[0] = True
                _conectado_antes[1] = now
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
    """Sirve el dash y la API de analisis (/api/*).

    Dos cosas: evita que el navegador del celular sirva una version cacheada, y
    resuelve las rutas /api/* del visor de telemetria leyendo lo que ya esta en
    telemetry/. Todo lo demas cae al servidor de archivos de siempre.
    """

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, *a):
        pass  # silencioso

    # ---------------- API del visor ----------------
    def do_GET(self):
        if self.path.startswith("/api/"):
            return self._api()
        return super().do_GET()

    def _json(self, obj, code=200):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _api(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ruta = u.path[5:]
        if ams2_analysis is None:
            return self._json({"error": "el modulo de analisis no cargo; ver bridge.log"}, 503)
        try:
            if ruta == "sesiones":
                return self._json({"sesiones": ams2_analysis.sesiones(int(q.get("n", 40)))})
            carpeta = ams2_analysis._carpeta(q.get("s", ""))
            if ruta == "sesion":
                return self._json({
                    "meta": ams2_analysis._meta(carpeta),
                    "carpeta": os.path.basename(carpeta),
                    "tandas": ams2_analysis.stints(carpeta),
                })
            if ruta == "mapa":
                return self._json(ams2_analysis.mapa(carpeta, q.get("t")))
            if ruta == "traza":
                return self._json(ams2_analysis.traza(
                    carpeta, q.get("t", ""), paso=float(q.get("paso", ams2_analysis.PASO_M))))
            if ruta == "export":
                return self._export(carpeta, int(q.get("tanda", 1)),
                                    q.get("validas") == "1")
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:                       # nunca tumbar el hilo del server
            log(f"[api] {ruta}: {type(e).__name__}: {e}")
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"error": "ruta desconocida"}, 404)

    def _export(self, carpeta, tanda, solo_validas):
        """CSV de una tanda completa. Se manda en streaming: una tanda larga son
        decenas de MB y armarla entera en memoria antes del primer byte deja al
        celular mirando una pantalla en blanco."""
        lineas = ams2_analysis.stint_csv(carpeta, tanda, solo_validas)
        primera = next(lineas)                       # dispara los errores ANTES del 200
        nombre = f"{os.path.basename(carpeta)}__tanda{tanda}.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{nombre}"')
        self.end_headers()
        self.wfile.write(primera.encode("utf-8"))
        for ln in lineas:
            self.wfile.write(ln.encode("utf-8"))


def serve_http():
    here = os.path.dirname(os.path.abspath(__file__))
    handler = lambda *a, **kw: _NoCacheHandler(*a, directory=here, **kw)
    httpd = http.server.ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), handler)
    httpd.serve_forever()


async def main():
    global analyzer, telemetry, cola
    analyzer = ams2_dampers.DamperAnalyzer().start()
    telemetry = ams2_telemetry.TelemetryLogger().start()
    # Bandeja de entrega: el grabador le avisa cada carpeta que cierra (set_on_close) y
    # ella le pregunta cual es la sesion en curso (sess_dir) para no tocarla jamas. Si
    # algo de esto falla, `cola` queda en None y el dash publica entrega "off".
    if entrega_cola is not None:
        try:
            cola = entrega_cola.ColaEntrega(
                base_dir=ams2_telemetry.TELEM_DIR,
                sess_dir_actual=telemetry.sess_dir,
                en_pista=lambda: _EN_PISTA,
                log=log)
            telemetry.set_on_close(cola.encolar)
            cola.start()
            log("[entrega] bandeja activa")
        except Exception as e:
            cola = None
            log(f"[entrega] bandeja desactivada: {e}")
    else:
        log(f"[entrega] modulo ausente: {_ENTREGA_ERR}")
    threading.Thread(target=serve_http, daemon=True).start()
    # Vigilante del event loop: hilo del SO aparte, para que siga corriendo aunque el
    # loop se cuelgue (que es justo el modo de falla que hay que cazar).
    threading.Thread(target=_watchdog, daemon=True).start()
    reint = os.environ.get("AMS2_BRIDGE_REINICIOS")
    if reint:
        log(f"[bridge-shm] arrancado por el watchdog (reinicio {reint}) "
            f"-- revisa el volcado de stacks mas arriba en {LOG_FILE}")
    ip = lan_ip()
    log("[bridge-shm] Fuente: AMS2 Shared Memory ($pcars2$, Project CARS 2)")
    log(f"[bridge-shm] WS   : ws://{ip}:{WS_PORT}")
    log(f"[bridge-shm] Dash : http://{ip}:{HTTP_PORT}  <- abrir en el celular")
    log("[bridge-shm] (En AMS2: Options -> System -> Shared Memory = Project CARS 2)")
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
        await pump()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("[bridge-shm] detenido (Ctrl-C)")
    except Exception as e:                    # noqa: BLE001
        # Cualquier muerte del loop queda ESCRITA. Antes, lanzado con Start-Process el
        # traceback se iba a una ventana minimizada y se perdia con el proceso.
        import traceback
        log(f"[bridge-shm] MUERTO por excepcion: {type(e).__name__}: {e}")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except OSError:
            pass
        raise
    finally:
        # Cierra la tanda de gomas en curso. Sin esto la referencia de camber solo se
        # persistia si el bridge alcanzaba a ver una rotacion de sesion, asi que cerrar
        # el dash o AMS2 perdia la tanda entera -- justo la que el piloto acaba de girar.
        tyres.close()
