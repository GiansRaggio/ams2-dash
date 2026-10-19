#!/usr/bin/env python3
"""Logger de telemetria por vuelta de AMS2 (shared memory) para analisis offline.

Corre su propio hilo con su propio Reader (~50 Hz). Detecta cruces de meta y, por
cada vuelta VALIDA (no invalidada por limites de pista, ni out/in-lap de boxes),
vuelca:

  telemetry/<pista>__<auto>__<sesion>__<fecha>/
      session.json                 metadatos de la sesion
      summary.jsonl                1 linea por vuelta valida (resumen)
      L003_92.451s.csv.gz          traza completa de la vuelta LIMPIA
      X005_93.118s.csv.gz          traza de una vuelta INVALIDADA por limites de pista
      S001_151.351s.csv.gz         traza de la vuelta 1 de una CARRERA (desde la largada)
      timeline.jsonl              linea de tiempo COMPLETA (aditiva): 1 registro por CADA
                                   vuelta cruzada (out/in/pit/invalida/corta incluidas) +
                                   eventos (largada, pit_in/out, cambio de goma)

El prefijo X es deliberado. Una vuelta invalidada por pisar un piano conserva
frenadas y trazada utiles, asi que su TRAZA se guarda (el visor compara la tanda
vuelta por vuelta y sin ellas se pierde media sesion), pero NO entra a
summary.jsonl. Todo el analisis del repo llega a las trazas por el resumen, de
modo que su corpus sigue siendo solo vueltas limpias y no cambia por esto.

El prefijo S, por lo mismo: la vuelta 1 de una carrera (largada, trafico) no es
representativa aunque sea valida, y tyre_replay/crossover_replay toman TODO L*
como vuelta limpia. Va al visor por la linea de tiempo (kind "start"), no al resumen.

La traza es CSV gzippeado: 1 fila por muestra (~50 Hz), columnas = canales
(velocidad, pedales, direccion, g-forces, posicion, y por esquina: temps de goma
inner/center/outer, temp de freno, desgaste, recorrido/velocidad de suspension,
slip, ride height, presion, rps). Pensado para abrir con pandas / MoTeC / Excel.

append-only y por-archivo -> sobrevive un taskkill sin corromper datos previos.
La VALIDEZ por limites de pista NO afecta nada mas que el guardado (solo se
guardan vueltas limpias, que son las representativas para evaluar).
Solo Windows (Reader usa la shared memory de AMS2).
"""
import gzip
import json
import os
import threading
import time
from datetime import datetime

import ams2_shm

CORNERS = ("FL", "FR", "RL", "RR")
RATE_HZ = 50                  # muestras por segundo de la traza
MIN_LAP_SAMPLES = 200         # menos que esto = vuelta demasiado corta (out/parcial)
# Techo duro del buffer de una vuelta. 20 min a 50 Hz: mas del doble de la vuelta
# mas larga del corpus (Nordschleife, 6:31 = 19.500 muestras). Es una red de
# seguridad contra el crecimiento sin limite, no un limite de diseno.
MAX_LAP_SAMPLES = 60000
TELEM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry")

_LIVE = (ams2_shm.GAME_INGAME_PLAYING, ams2_shm.GAME_INGAME_INMENU_TIME_TICKING)
# Fuera de toda sesion: el menu principal del juego (y el juego cerrado). Es el fin
# de la clase -- "a boxes, cierra la sesion y entrega" -- y la carpeta en curso se
# cierra igual que al rotar. NO van aca la pausa (INGAME_PAUSED), el menu dentro
# de la sesion (INMENU_TIME_TICKING), el reinicio (INGAME_RESTARTING) ni la
# repeticion DENTRO de la sesion (INGAME_REPLAY): todos siguen dentro de la
# sesion. Enum verificado el 2026-10-11
# contra SharedMemory.h de CREST2-AMS2 (SHARED_MEMORY_VERSION 14): EXITED 0,
# FRONT_END 1, PLAYING 2, PAUSED 3, INMENU_TIME_TICKING 4, RESTARTING 5,
# INGAME_REPLAY 6, FRONT_END_REPLAY 7.
# FRONT_END_REPLAY (7) tambien: es una repeticion vista DESDE el menu principal, con
# la sesion ya terminada (visto en pista el 2026-10-11: carrera -> menu de sesion (4)
# -> menu principal (1, mSessionState 0) -> repeticion desde el menu (7)).
_FUERA_DE_SESION = (ams2_shm.GAME_FRONT_END, ams2_shm.GAME_EXITED,
                    ams2_shm.GAME_FRONT_END_REPLAY)
# Segundos seguidos en el menu principal antes de cerrar. Contra un parpadeo: la
# shared memory se escribe sin lock y un frame a medio escribir puede traer
# cualquier mGameState; cinco segundos son 250 frames seguidos a 50 Hz.
MENU_CIERRE_S = 5.0
_PIT_INSIDE = (1, 2, 4)       # mPitMode entrando/parado/garage -> en boxes
# Mitad de la pista: pasar por aca en pista despues de boxes prueba que el auto dio la
# vuelta y no esta en el pit lane, que tambien queda cerca de la linea de meta.
_MITAD = (0.25, 0.75)
_PIT_NONE = 0
# mSessionState de carrera y mRaceState (enum de SharedMemory.h de CREST2-AMS2, v14)
_SESION_CARRERA = 5
_SIN_LARGAR, _CORRIENDO = 1, 2   # RACESTATE_NOT_STARTED, RACESTATE_RACING
_SESS = {0: "invalid", 1: "practice", 2: "test", 3: "qualify",
         4: "formation", 5: "race", 6: "hotlap"}

# --- esquema de canales: header y fila se construyen del MISMO spec (no se desfasan) ---
_SCALAR = [
    ("t",         lambda d, p, cap: round(d.mCurrentTime, 3)),       # s en la vuelta
    ("lap_dist",  lambda d, p, cap: round(p.mCurrentLapDistance, 2)),  # m
    ("speed_kmh", lambda d, p, cap: round(d.mSpeed * 3.6, 2)),
    ("rpm",       lambda d, p, cap: round(d.mRpm)),
    ("gear",      lambda d, p, cap: d.mGear),
    ("throttle",  lambda d, p, cap: round(d.mUnfilteredThrottle, 4)),
    ("brake",     lambda d, p, cap: round(d.mUnfilteredBrake, 4)),
    ("clutch",    lambda d, p, cap: round(d.mUnfilteredClutch, 4)),
    ("steer",     lambda d, p, cap: round(d.mUnfilteredSteering, 4)),
    ("steer_f",   lambda d, p, cap: round(d.mSteering, 4)),
    ("brake_bias", lambda d, p, cap: round(d.mBrakeBias, 4)),
    ("accel_x",   lambda d, p, cap: round(d.mLocalAcceleration[0], 3)),  # m/s2
    ("accel_y",   lambda d, p, cap: round(d.mLocalAcceleration[1], 3)),
    ("accel_z",   lambda d, p, cap: round(d.mLocalAcceleration[2], 3)),
    ("yaw",       lambda d, p, cap: round(d.mOrientation[0], 4)),
    ("pitch",     lambda d, p, cap: round(d.mOrientation[1], 4)),
    ("roll",      lambda d, p, cap: round(d.mOrientation[2], 4)),
    ("pos_x",     lambda d, p, cap: round(p.mWorldPosition[0], 2)),
    ("pos_y",     lambda d, p, cap: round(p.mWorldPosition[1], 2)),
    ("pos_z",     lambda d, p, cap: round(p.mWorldPosition[2], 2)),
    ("water_t",   lambda d, p, cap: round(d.mWaterTempCelsius, 1)),
    ("oil_t",     lambda d, p, cap: round(d.mOilTempCelsius, 1)),
    ("fuel_l",    lambda d, p, cap: round(d.mFuelLevel * cap, 2)),
    # --- handling / powertrain (balance del auto + analisis de cambios) ---
    ("max_rpm",       lambda d, p, cap: round(d.mMaxRPM)),
    ("engine_torque", lambda d, p, cap: round(d.mEngineTorque, 1)),
    ("local_vx",      lambda d, p, cap: round(d.mLocalVelocity[0], 3)),   # lateral m/s -> slip angle del auto
    ("local_vz",      lambda d, p, cap: round(d.mLocalVelocity[2], 3)),   # longitudinal m/s
    ("ang_vel_x",     lambda d, p, cap: round(d.mAngularVelocity[0], 4)),  # pitch rate
    ("ang_vel_y",     lambda d, p, cap: round(d.mAngularVelocity[1], 4)),  # yaw rate (rotacion eje vertical)
    ("ang_vel_z",     lambda d, p, cap: round(d.mAngularVelocity[2], 4)),  # roll rate
    ("abs_active",    lambda d, p, cap: int(d.mAntiLockActive)),           # bool dedicado (mas limpio que el flag)
    # --- CONTEXTO DE CARRERA (desde 2026-08-20) ---
    # El grabador era de vuelta propia: media bien la tecnica y no veia NADA de lo que
    # pasa con los otros autos. Eso dejaba la dimension de racecraft y limpieza -- un
    # quinto de la evaluacion de la escuela -- entera al ojo del instructor. Estos
    # canales ya estaban en la shared memory; solo habia que guardarlos.
    #
    # OJO al leerlos: `coll_mag` NO es un pulso. AMS2 deja el ULTIMO choque publicado
    # y ahi se queda, asi que contar frames con coll_mag>0 cuenta cualquier cosa menos
    # choques. Para contarlos hay que detectar el CAMBIO de valor. Misma trampa que
    # `mLapInvalidated` (ver docs/ESTADO.md): en esta shared memory conviene asumir
    # que un campo es estado sostenido y no evento, salvo prueba en contrario.
    ("race_pos",     lambda d, p, cap: int(p.mRacePosition)),
    ("split_ahead",  lambda d, p, cap: round(d.mSplitTimeAhead, 3)),   # s al de adelante
    ("split_behind", lambda d, p, cap: round(d.mSplitTimeBehind, 3)),  # s al de atras
    ("coll_idx",     lambda d, p, cap: int(d.mLastOpponentCollisionIndex)),   # -1 si nadie
    ("coll_mag",     lambda d, p, cap: round(d.mLastOpponentCollisionMagnitude, 3)),
    ("crash_state",  lambda d, p, cap: int(d.mCrashState)),
    ("aero_dmg",     lambda d, p, cap: round(d.mAeroDamage, 4)),
    ("engine_dmg",   lambda d, p, cap: round(d.mEngineDamage, 4)),
    ("flag",         lambda d, p, cap: int(d.mHighestFlagColour)),
    ("yellow",       lambda d, p, cap: int(d.mYellowFlagState)),
    ("launch_stage", lambda d, p, cap: int(d.mLaunchStage)),           # calidad de largada
    ("clutch_slip",  lambda d, p, cap: int(d.mClutchSlipping)),
]
_CORNER = [
    ("tyre_temp",   lambda d, i: round(d.mTyreTemp[i], 1)),
    ("tyre_t_in",   lambda d, i: round(d.mTyreTempLeft[i], 1)),
    ("tyre_t_mid",  lambda d, i: round(d.mTyreTempCenter[i], 1)),
    ("tyre_t_out",  lambda d, i: round(d.mTyreTempRight[i], 1)),
    ("brake_temp",  lambda d, i: round(d.mBrakeTempCelsius[i], 1)),
    ("tyre_wear",   lambda d, i: round(d.mTyreWear[i], 5)),
    ("susp_travel", lambda d, i: round(d.mSuspensionTravel[i], 5)),
    ("susp_vel",    lambda d, i: round(d.mSuspensionVelocity[i], 5)),
    ("tyre_slip",   lambda d, i: round(d.mTyreSlipSpeed[i], 3)),
    ("ride_h",      lambda d, i: round(d.mRideHeight[i], 5)),
    ("tyre_press",  lambda d, i: round(d.mAirPressure[i], 2)),
    ("tyre_rps",    lambda d, i: round(d.mTyreRPS[i], 2)),
    ("terrain",     lambda d, i: d.mTerrain[i]),                            # superficie bajo la rueda (codigo)
    ("carcass_t",   lambda d, i: round(d.mTyreCarcassTemp[i] - 273.15, 1)),  # Kelvin -> C (temp estructura, estable)
    # MARGEN de agarre SIN USAR (0..1), no agarre disponible. Medido: corr -0.725 con
    # deslizamiento y -0.769 con G lateral; 0.457 en recta contra 0.104 en curva -> BAJA
    # cuando exiges la goma. Sirve para saber que rueda llega antes al limite en cada
    # curva. OJO: el docstring de ams2_strategy dice que este canal "no se puebla" --
    # eso es FALSO, se midio variando 0.0000-0.9967 con senal coherente.
    # PENDIENTE de resolver: si el 0.0 exacto es saturacion real o centinela de
    # "sin dato / rueda descargada". Hasta saberlo, no tratar 0 como limite alcanzado.
    ("tyre_grip",   lambda d, i: round(d.mTyreGrip[i], 4)),
    # Piel de la goma (Kelvin -> C). Canal PROPIO, no copia del bulk: se midio 2.8-5.2 C
    # de diferencia. Con carcass_t y tyre_temp completa el corte de profundidad
    # superficie -> masa -> carcasa, que es lo que AMS2 modela de verdad.
    ("layer_t",     lambda d, i: round(d.mTyreLayerTemp[i] - 273.15, 1)),
]
HEADER = [n for n, _ in _SCALAR] + [f"{n}_{c}" for n, _ in _CORNER for c in CORNERS]


def _txt(b):
    """Texto de la shared memory tal cual, sin sanear. `_safe` existe para armar
    nombres de CARPETA (cambia espacios y parentesis); estos campos viajan al .srt y
    tienen que salir identicos a los que produce el juego."""
    try:
        return b.split(b"\x00")[0].decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _row(d, p, cap):
    r = [fn(d, p, cap) for _, fn in _SCALAR]
    for _, fn in _CORNER:
        for i in range(4):
            r.append(fn(d, i))
    return ",".join(map(str, r))


def _safe(b):
    s = b.split(b"\x00")[0].decode("utf-8", "replace")
    return "".join(c if c.isalnum() else "_" for c in s).strip("_") or "x"


def _label(b):
    """Nombre de compuesto para MOSTRAR (timeline): decode utf-8 + strip de nulls,
    conservando espacios/puntuacion. A diferencia de _safe (pensado para nombres de
    archivo) no colapsa la puntuacion, asi que 'P Zero' y 'P.Zero' no se confunden."""
    return b.split(b"\x00")[0].decode("utf-8", "replace").strip() or "x"


class TelemetryLogger:
    """Graba la traza completa de cada vuelta valida en disco (CSV gz + resumen)."""

    def __init__(self, base_dir=TELEM_DIR, rate_hz=RATE_HZ, on_close=None):
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._mode = "full"       # "off" | "summary" (liviano) | "full" (traza completa)
        self._base = base_dir
        self._period = 1.0 / rate_hz
        # Aviso de "esta carpeta ya se cerro": lo consume la bandeja de entrega. Se
        # anota dentro del lock al rotar y se DRENA en _run fuera del lock, porque
        # _ingest tiene un return temprano dentro del lock y un callback al final del
        # metodo se saltaria. Ademas asi el callback nunca corre con el lock tomado.
        self._on_close = on_close
        self._cerrada = None
        # desde cuando (reloj monotono) el juego esta en el menu principal; None =
        # dentro de una sesion. El reloj es inyectable para probar el debounce.
        self._menu_desde = None
        self._reloj = time.monotonic
        # Vigilancia del JUEGO (no del menu): ultimo mSequenceNumber visto, desde
        # cuando no cambia (o no se puede leer la memoria), y si ya se dio el juego
        # por perdido. `_juego_vivo` es inyectable para los tests.
        self._seq_prev = None
        self._quieta_desde = None
        self._chequeo_proc = 0.0
        self._juego_perdido = False
        self._juego_vivo = ams2_shm.juego_abierto
        # stats expuestas al frontend
        self._laps_logged = 0
        self._last_file = None
        self._recording = False
        self._player_name = ams2_shm.read_player_name()   # ancla a TU auto (no al de la camara, bug MP)
        self._reset_session()

    def _reset_session(self):
        self._rivals = {}         # {nombre: {best, sec[3], pos, laps, inv}} de la sesion en curso
        self._rivals_dirty = False
        self._sig = None
        self._sess_dir = None
        self._sess_label = None
        self._creada = False
        self._meta_sesion = None
        self._eventos_pend = []
        self._cap = 100.0
        self._last_lap = -1
        self._buf = []            # filas CSV (str) de la vuelta en curso
        self._invalid = False     # la vuelta se invalido (limites de pista)
        self._lap_ok = False      # estuvo habilitado toda la vuelta
        self._pit_this = True     # hubo pit en esta vuelta (1ra de sesion: si)
        self._pit_prev = True     # la vuelta arranca saliendo de boxes (-> out-lap)
        self._in_pit_prev = False
        self._en_pista = False    # desde la ultima vez en boxes ya paso por la mitad de la pista
        self._dist_prev = None    # mCurrentLapDistance del frame anterior fuera de boxes
        self._sin_largar = False  # carrera esperando la largada (parrilla / formacion)
        self._largada = False     # la vuelta en curso es la 1 de la carrera, desde la largada
        self._anclar_en_meta = False  # largo detras de la linea: la vuelta empieza al cruzarla
        self._compound_prev = None  # compuesto de las 4 gomas visto (para detectar cambio de goma)
        self._sectimes = [0.0, 0.0]
        self._sec_invalid = [False, False, False]
        self._lap_uid = 0         # id monotonico por vuelta loggeada (no resetea con el garage)
        self._agg = None          # agregados de resumen
        self._start = None        # {fuel, wear[4]} al inicio de la vuelta

    # ---------------- API publica ----------------
    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop = True

    def set_mode(self, mode):
        """off = no graba; summary = solo resumen por vuelta (liviano, ~10Hz);
        full = resumen + traza completa de 71 canales (~50Hz).

        Pasar a "off" CIERRA la carpeta en curso (igual que al rotar): apagado, el
        hilo ni siquiera lee la memoria, asi que nada mas le va a entrar, y si se
        quedaba abierta la bandeja la seguia viendo como "la actual" y no la
        ofrecia nunca. Si se vuelve a encender en la misma sesion del juego, sigue
        en una carpeta nueva."""
        if mode in ("off", "summary", "full"):
            with self._lock:
                self._mode = mode
                if mode == "off":
                    self._cerrar_carpeta()

    def set_enabled(self, on):
        self.set_mode("full" if on else "off")   # compat con el toggle viejo

    def set_on_close(self, cb):
        """Registra quien recibe la carpeta cerrada. Setter y no solo argumento del
        constructor porque la bandeja necesita a su vez `sess_dir()` de este objeto:
        el cableado es circular y alguien tiene que ir segundo."""
        with self._lock:
            self._on_close = cb

    def sess_dir(self):
        """Carpeta de la sesion EN CURSO (o None). La bandeja la usa para no empaquetar
        jamas la carpeta que todavia se esta escribiendo."""
        with self._lock:
            return self._sess_dir

    def _drenar_cerrada(self):
        with self._lock:
            carpeta, self._cerrada = self._cerrada, None
            cb = self._on_close
        if carpeta and cb:
            try:
                cb(carpeta)               # solo encola: el trabajo pesado es del otro hilo
            except Exception:
                pass                      # un fallo de la bandeja no puede tocar al grabador

    def status(self):
        with self._lock:
            return {
                "mode": self._mode,
                "enabled": self._mode != "off",
                "recording": self._recording,
                "laps_logged": self._laps_logged,
                "session": self._sess_label,
                "last_file": self._last_file,
            }

    # ---------------- hilo de muestreo ----------------
    def _run(self):
        reader = None
        while not self._stop:
            mode = self._mode
            if mode == "off":             # apagado: hilo dormido, no lee la memoria
                self._recording = False
                self._drenar_cerrada()    # la carpeta que cerro set_mode("off")
                time.sleep(0.5)
                continue
            if reader is None:
                try:
                    reader = ams2_shm.Reader().open()
                except ams2_shm.SharedMemoryUnavailable:
                    self._vigilar_juego(None)
                    self._drenar_cerrada()
                    time.sleep(1.0)
                    continue
            try:
                d = reader.snapshot()
            except OSError:
                reader.close()
                reader = None
                self._vigilar_juego(None)
                self._drenar_cerrada()
                time.sleep(1.0)
                continue
            if self._vigilar_juego(d.mSequenceNumber):
                try:
                    self._ingest(d)
                except Exception:
                    pass                  # nunca tumbar el hilo por un error de I/O
            self._drenar_cerrada()        # fuera del lock, y aunque _ingest haya retornado antes
            time.sleep(self._period if mode == "full" else 0.1)   # full 50Hz, summary ~10Hz

    def _ingest(self, d):
        if d.mVersion != ams2_shm.SHARED_MEMORY_VERSION:
            return
        # Antes del filtro de participantes, a proposito: en el menu principal el
        # juego puede publicar cero participantes, y ese return dejaba la carpeta
        # abierta hasta la proxima sesion o hasta reiniciar el dash.
        self._vigilar_menu_principal(d)
        if d.mNumParticipants <= 0:
            return
        if d.mGameState not in _LIVE:
            with self._lock:
                self._recording = False
            return
        v = ams2_shm.player_index(d, self._player_name)   # TU auto, no el que mira la camara (bug MP)
        if not (0 <= v < ams2_shm.STORED_PARTICIPANTS_MAX):
            return
        p = d.mParticipantInfo[v]

        with self._lock:
            if d.mFuelCapacity > 1.0:
                self._cap = d.mFuelCapacity
            cap = self._cap

            sig = (bytes(d.mTrackLocation), bytes(d.mCarName), int(d.mSessionState))
            if sig != self._sig:
                self._write_rivals()      # cerrar el registro de la sesion que se va
                # Este es uno de los DOS puntos donde una carpeta esta demostrablemente
                # cerrada: nada mas le va a entrar. El otro es la vuelta al menu
                # principal (_vigilar_menu_principal). Por eso la bandeja se engancha
                # aca y no en un timer -- una sesion que se sube mientras crece se
                # duplica en el servidor. Y solo si tiene vueltas: una sin vueltas la
                # bandeja la rechazaria igual (ver _entregable).
                if self._entregable():
                    self._cerrada = self._sess_dir
                self._rotate_session(d, sig)
            self._track_rivals(d, v)      # acumula en memoria; se escribe al cerrar vuelta

            in_pit = d.mPitMode in _PIT_INSIDE
            if in_pit:
                self._pit_this = True

            lap = p.mLapsCompleted
            carrera = int(d.mSessionState) == _SESION_CARRERA
            estado = self._estado_carrera(d, v) if carrera else 0
            if self._last_lap < 0:
                self._last_lap = lap
                self._begin_lap(d, p, cap)
            elif lap > self._last_lap:
                self._commit_lap(d, p)
                self._last_lap = lap
                self._begin_lap(d, p, cap)
            elif lap < self._last_lap:        # volvio a boxes / reinicio
                self._last_lap = lap
                self._begin_lap(d, p, cap)
                self._pit_prev = True
                self._largada = False
            elif self._cruce_sin_contar(d, p, in_pit):
                # Cruzo meta y AMS2 no conto la vuelta: era la out-lap. Se cierra ACA
                # para que la lanzada que empieza sea una vuelta propia, con su traza.
                self._commit_lap(d, p, contada=False)
                self._begin_lap(d, p, cap)
            elif self._sin_largar and estado == _CORRIENDO:
                # LARGADA: la vuelta 1 empieza aca, sin la parrilla ni la formacion. La
                # primera vuelta de la sesion se daba por out-lap; en carrera solo lo es
                # si se larga desde el pit lane.
                self._begin_lap(d, p, cap)
                self._largada = True
                self._pit_prev = d.mPitMode != _PIT_NONE
                largo = d.mTrackLength
                self._anclar_en_meta = (not self._pit_prev and largo > 0
                                        and p.mCurrentLapDistance > 0.5 * largo)
            elif self._anclar_en_meta and self._cruce_de_meta(d, p, in_pit):
                # Largo detras de la linea: la distancia arrancaba cerca del final de
                # la vuelta y el visor (que exige distancia creciente) se quedaba solo
                # con esos metros. La traza parte al cruzar la linea.
                self._begin_lap(d, p, cap)
                self._anclar_en_meta = False
            self._sin_largar = carrera and estado == _SIN_LARGAR
            self._seguir_pista(d, p, in_pit)

            # El sector EN CURSO se deduce de lo que el juego publica en ESTE frame, y
            # se calcula ANTES de copiar los splits. Con `_sectimes` habia dos fallas:
            # se actualiza unas lineas mas abajo con el split de este mismo frame (un
            # corte justo en la linea del S1 se le atribuia al S2), y es sticky -- solo
            # se escribe cuando el valor supera 0.1 y nunca se baja, asi que un frame
            # torn con basura dejaba el sector en curso mintiendo hasta meta.
            cs_ahora = (0 if d.mCurrentSector1Time <= 0.1
                        else 1 if d.mCurrentSector2Time <= 0.1 else 2)
            # captura de sectores en vivo: S1/S2 se finalizan a mitad de vuelta y quedan
            # estables hasta meta (el bug viejo leia mCurrentSector1Time ya reseteado al cruzar).
            if d.mCurrentSector1Time > 0.1:
                self._sectimes[0] = round(d.mCurrentSector1Time, 3)
            if d.mCurrentSector2Time > 0.1:
                self._sectimes[1] = round(d.mCurrentSector2Time, 3)
            if bool(d.mLapInvalidated):
                # Marcar SOLO en el flanco de subida, y con el sector del frame.
                #
                # OJO CON EL RAZONAMIENTO, que ya me lo comi una vez: `_sec_invalid[cs]`
                # ACUMULA, no pisa. Si el flag estuviera alto desde el S1 hasta meta, el
                # codigo viejo habria dejado los TRES sectores sucios -- y en el corpus
                # eso pasa 1 vez de 134, mientras 128 marcan SOLO el S3. Conclusion: el
                # flag NO esta alto durante S1/S2. AMS2 lo levanta TARDE, ya cerca de
                # meta, cuando confirma la infraccion de limites.
                #
                # Por eso el flanco NO basta para atribuir bien el sector, y el rescate
                # de sectores "limpios" de vueltas sucias queda DESACTIVADO en el
                # analizador hasta verificarlo en pista: salirse a proposito en el S1 y
                # mirar que marca sectors.jsonl. Un intento por sector cierra el tema.
                if not self._invalid:
                    self._sec_invalid[cs_ahora] = True
                self._invalid = True
            if self._mode == "off":
                self._recording = False
                self._in_pit_prev = in_pit    # sembrar el estado de boxes: al reactivar (off->full)
                return                         # parado en el pit no dispara un pit_in fantasma
            # --- linea de tiempo: eventos discretos (solo si SI se graba; en off ya salimos) ---
            if self._sess_dir is not None:
                if in_pit and not self._in_pit_prev:
                    self._timeline_event("pit_in", d, p)
                elif not in_pit and self._in_pit_prev:
                    self._timeline_event("pit_out", d, p)
                comp = tuple(bytes(d.mTyreCompound[i]).split(b"\x00")[0] for i in range(4))
                # AMS2 puebla los strings de goma con lag (primeros frames tras cargar la
                # sesion) y la SHM se escribe sin lock (torn frames): una lectura vacia da
                # basura. Solo comparar/emitir con las 4 gomas pobladas -> 'start' espera el
                # primer compuesto REAL y los torn frames no generan cambios fantasma.
                if all(comp):
                    if self._compound_prev is None:
                        self._timeline_event("start", d, p, extra={"compound": [_label(c) for c in comp]})
                    elif comp != self._compound_prev:
                        self._timeline_event("tyre_change", d, p,
                                             extra={"from": [_label(c) for c in self._compound_prev],
                                                    "to": [_label(c) for c in comp]})
                    self._compound_prev = comp
            self._in_pit_prev = in_pit
            self._update_agg(d)                       # agregados de resumen (ambos modos)
            # La traza completa solo en full, y SOLO manejando.
            #
            # `_LIVE` incluye INMENU_TIME_TICKING (menu con el reloj corriendo:
            # lobby, garage, espera entre carreras). En ese estado el contador de
            # vueltas no avanza, asi que `_begin_lap` -- el unico que vacia el
            # buffer -- nunca se llama y la lista crece a 50 filas/s PARA SIEMPRE.
            # Son ~580 bytes por fila: 1.7 MB por minuto, 104 MB por hora. Y el
            # costo real no es la memoria sino el GC de Python, que recorre
            # contenedores: con millones de strings cada pasada se vuelve carisima
            # y se lleva el GIL, hundiendo los FPS del juego. Medido por el piloto:
            # tras varias carreras seguidas el juego caia de 160 a 60 fps, y
            # cerrando el dash volvia a 160.
            # Tampoco antes de la largada: la parrilla y la formacion no son vuelta 1.
            if (self._mode == "full" and d.mGameState == ams2_shm.GAME_INGAME_PLAYING
                    and not self._sin_largar):
                if len(self._buf) < MAX_LAP_SAMPLES:
                    self._buf.append(_row(d, p, cap))
                elif self._buf:
                    # Segunda red: una "vuelta" mas larga que esto no es una vuelta
                    # (quedaste detenido en pista, o el contador no avanzo). Se suelta
                    # el buffer en vez de arrastrarlo; la vuelta se pierde, que es
                    # justo lo que ya iba a pasar con ella.
                    self._buf = []
                    self._lap_ok = False
            self._recording = True

    # ---------------- ciclo de vuelta / sesion ----------------
    def _begin_lap(self, d, p, cap):
        self._buf = []
        self._invalid = False
        self._lap_ok = self._mode != "off"
        # Out-lap = la que ARRANCA saliendo de boxes. Si despues de boxes el auto ya
        # dio la vuelta por la pista (la out-lap ya se cerro), la que empieza es lanzada.
        self._pit_prev = self._pit_this and not self._en_pista
        self._pit_this = bool(d.mPitMode in _PIT_INSIDE)
        self._sectimes = [0.0, 0.0]                  # S1, S2 capturados en vivo (S3 = total - S1 - S2)
        self._sec_invalid = [False, False, False]    # validez por sector (limites de pista)
        self._agg = {"tmin": [9e9] * 4, "tmax": [-9e9] * 4, "tsum": [0.0] * 4,
                     "bmax": [-9e9] * 4, "n": 0}
        self._start = {"fuel": d.mFuelLevel * cap,
                       "wear": [d.mTyreWear[i] for i in range(4)]}

    def _cruce_sin_contar(self, d, p, in_pit):
        """Cruce de meta que AMS2 NO conto: el de la out-lap que sale del garage.

        Cuando el pit lane no cruza la linea (Spa, Road Atlanta, Watkins Glen... el
        90% de las salidas del corpus), la out-lap no tuvo un cruce que la empezara:
        al cruzar meta arranca el cronometro pero mLapsCompleted no sube, y recien
        sube al cerrar la LANZADA. Se reconoce por la distancia, que vuelve a cero
        en la linea aunque el contador no se mueva. Solo se mira en una vuelta con
        boxes y despues de pasar por la mitad de la pista, para que no lo dispare el
        pit lane (tambien junto a la linea) ni una vuelta normal."""
        return self._pit_this and self._en_pista and self._cruce_de_meta(d, p, in_pit)

    def _cruce_de_meta(self, d, p, in_pit):
        """La distancia volvio a cero en pista: el auto cruzo la linea de meta."""
        largo = d.mTrackLength
        return (not in_pit and largo > 0
                and self._dist_prev is not None and self._dist_prev > 0.5 * largo
                and p.mCurrentLapDistance < _MITAD[0] * largo)

    @staticmethod
    def _estado_carrera(d, v):
        """mRaceState de TU auto. El arreglo por participante es opcional a proposito
        (como en _track_rivals): sin el, el campo global; sin ninguno, 0 = no se sabe,
        y la vuelta 1 se trata como antes."""
        try:
            return int(d.mRaceStates[v])
        except (AttributeError, IndexError, TypeError):
            return int(getattr(d, "mRaceState", 0))

    def _seguir_pista(self, d, p, in_pit):
        """Estado de pista para `_cruce_sin_contar` y la regla de out-lap de _begin_lap."""
        largo = d.mTrackLength
        dist = p.mCurrentLapDistance
        if in_pit:
            self._en_pista = False
            self._dist_prev = None
            return
        if largo > 0 and _MITAD[0] * largo < dist < _MITAD[1] * largo:
            self._en_pista = True
        self._dist_prev = dist

    def _update_agg(self, d):
        a = self._agg
        if a is None:
            return
        for i in range(4):
            t = d.mTyreTemp[i]
            if t < a["tmin"][i]:
                a["tmin"][i] = t
            if t > a["tmax"][i]:
                a["tmax"][i] = t
            a["tsum"][i] += t
            b = d.mBrakeTempCelsius[i]
            if b > a["bmax"][i]:
                a["bmax"][i] = b
        a["n"] += 1

    def _vigilar_menu_principal(self, d):
        """Cierra la carpeta en curso cuando el juego VUELVE AL MENU PRINCIPAL y se
        queda ahi `MENU_CIERRE_S` segundos seguidos.

        Antes la carpeta solo se cerraba al cambiar la firma (pista, auto, tipo de
        sesion), o sea al ENTRAR a la siguiente sesion: el alumno terminaba la clase,
        salia al menu, y la bandeja seguia viendo esa carpeta como "la actual" -- no
        la podia entregar hasta girar otra cosa o reiniciar el dash, cuando los
        documentos del curso le dicen "a boxes, cierra la sesion y entrega".

        Sigue siendo un cierre por HECHO del juego, no por reloj de inactividad: en
        el menu principal no hay sesion, y si el alumno vuelve a entrar, el juego
        carga una sesion nueva aunque sea la misma pista con el mismo auto (antes esa
        se pegaba a la carpeta vieja). El debounce no decide QUE se cierra, solo
        descarta un mGameState leido de un frame a medio escribir.

        Se cierra igual que al rotar: rivales volcados, carpeta anotada en
        `_cerrada` (la drena _run hacia la bandeja) y firma en blanco, para que la
        proxima entrada a pista abra carpeta nueva. Se mira en cada _ingest, con el
        estado del frame; no hay hilo ni timer aparte.
        """
        if d.mGameState not in _FUERA_DE_SESION:
            self._menu_desde = None
            return
        ahora = self._reloj()
        if self._menu_desde is None:
            self._menu_desde = ahora
            return
        if ahora - self._menu_desde < MENU_CIERRE_S:
            return
        with self._lock:
            self._cerrar_carpeta()

    def _cerrar_carpeta(self):
        """Cierra la carpeta en curso igual que al rotar: rivales volcados, carpeta
        anotada en `_cerrada` (la drena _run hacia la bandeja), firma en blanco para
        que la proxima entrada a pista abra carpeta nueva. LLAMAR CON EL LOCK.
        Lo usan los tres cierres que no son rotacion: menu principal, juego cerrado
        y grabador apagado. False si no habia nada abierto."""
        if self._sess_dir is None and self._sig is None:
            return False                  # ya cerrada, o nunca se abrio: nada que hacer
        self._write_rivals()
        if self._entregable():            # sin vueltas no se le ofrece a la bandeja
            self._cerrada = self._sess_dir
        self._soltar_sesion()
        self._recording = False
        return True

    def _vigilar_juego(self, seq):
        """Cierra la carpeta si el JUEGO se fue (cerrado de golpe, sin pasar por el
        menu principal), con el mismo debounce que el menu. Devuelve si el frame se
        debe ingerir.

        `seq` es el mSequenceNumber del frame, o None si la memoria no se pudo leer.
        La memoria NO desaparece al cerrar el juego: el bridge tiene handles abiertos
        y el mapeo sobrevive, asi que `snapshot()` sigue entregando el ULTIMO frame
        congelado, sin error (ver ams2_shm.juego_abierto). La senal es entonces que
        el frame no cambie: `MENU_CIERRE_S` segundos con el mismo mSequenceNumber (o
        sin memoria legible). Como un juego vivo tambien deja de escribir (medido: en
        el menu principal la memoria queda quieta 70 s seguidos) o puede no dejarse
        leer un rato (cargando), antes de cerrar se mira la lista de procesos -- a lo
        mas cada 2 s -- y solo se cierra si AMS2 explicitamente ya no esta. Si el
        frame vuelve a cambiar antes, se cancela todo.

        Una vez perdido el juego, los frames congelados NO se ingieren: el ultimo
        estado publicado suele ser "en pista", y re-ingerirlo abriria una carpeta
        nueva y vacia apenas cerrada la anterior.
        """
        ahora = self._reloj()
        if seq is not None and seq != self._seq_prev:
            self._seq_prev = seq
            self._quieta_desde = None
            self._juego_perdido = False
            return True
        if self._juego_perdido:
            return False
        if self._quieta_desde is None:
            self._quieta_desde = ahora
            return True
        if ahora - self._quieta_desde < MENU_CIERRE_S:
            return True
        # Quieta o ilegible: en los DOS casos decide el proceso. Que la memoria no se
        # pueda leer no prueba que el juego se fue (puede estar cargando una sesion),
        # asi que solo se cierra si AMS2 explicitamente NO esta; si no se sabe, no.
        if ahora - self._chequeo_proc < 2.0:
            return True
        self._chequeo_proc = ahora
        if self._juego_vivo() is not False:
            return True                   # sigue abierto (o no se sabe): no se cierra
        self._juego_perdido = True
        with self._lock:
            self._cerrar_carpeta()
        return False

    def _soltar_sesion(self):
        """Estado por-sesion en blanco: lo comun a rotar y a cerrar en el menu.
        La vuelta en curso se descarta."""
        # Rivales: se vacian aca o el rivals.json de la carrera arrastraria a los que solo
        # corrieron la qualy, con tiempos de la sesion (o la PISTA) anterior.
        self._rivals = {}
        self._rivals_dirty = False
        self._sig = None
        self._sess_dir = None
        self._sess_label = None
        self._creada = False          # la carpeta ya existe en disco (ver _crear_carpeta)
        self._meta_sesion = None
        self._eventos_pend = []       # eventos de linea de tiempo antes de crear la carpeta
        self._last_lap = -1
        self._buf = []
        self._invalid = False
        self._pit_this = True
        self._pit_prev = True
        self._agg = None
        self._compound_prev = None    # sesion nueva -> no emitir tyre_change espurio
        self._in_pit_prev = False      # ni pit_in/out espurio del estado anterior

    def _crear_carpeta(self):
        """Crea en disco la carpeta de la sesion, la PRIMERA vez que hay algo que
        guardar: el primer cruce de meta (la vuelta de salida ya cuenta).

        Antes se creaba al entrar a la sesion, con el auto todavia en el garage. Con
        los cierres nuevos (menu principal, juego cerrado, grabador apagado) cada
        vuelta a la misma sesion abre carpeta nueva, y entrar y salir sin girar
        dejaba carpetas con un session.json y nada mas, que la bandeja encolaba
        para rechazarlas despues. Los eventos que pasan en el garage (goma de
        largada, entrada y salida de boxes) se guardan en memoria y se vuelcan aca,
        en orden."""
        if self._sess_dir is None or self._creada:
            return
        try:
            os.makedirs(self._sess_dir, exist_ok=True)
            with open(os.path.join(self._sess_dir, "session.json"), "w", encoding="utf-8") as f:
                json.dump(self._meta_sesion, f, ensure_ascii=False, indent=2)
        except OSError:
            self._sess_dir = None
            return
        self._creada = True
        pend, self._eventos_pend = self._eventos_pend, []
        for rec in pend:
            self._write_timeline(rec)

    def _entregable(self):
        """La carpeta en curso vale para la bandeja: existe y tiene al menos una
        vuelta en el resumen. Sin vueltas la bandeja la rechazaria igual ("no hay
        ninguna vuelta cronometrada limpia"), despues de dejarla dos minutos como
        pendiente y de listarla como invalida."""
        return bool(self._creada and self._sess_dir
                    and os.path.exists(os.path.join(self._sess_dir, "summary.jsonl")))

    def _rotate_session(self, d, sig):
        # cambio de pista/auto/tipo-de-sesion -> nueva carpeta; la vuelta en curso se descarta
        self._soltar_sesion()
        self._sig = sig
        label = _SESS.get(int(d.mSessionState), "session")
        self._sess_label = label
        name = (f"{_safe(bytes(d.mTrackLocation))}__{_safe(bytes(d.mCarName))}"
                f"__{label}__{datetime.now():%Y%m%d_%H%M%S}")
        # Nunca reabrir una carpeta que ya se cerro: el nombre va al segundo, y apagar
        # y prender el grabador dentro del mismo segundo (o una rotacion igual de
        # rapida) volvia a escribir en la carpeta que la bandeja ya tenia en cola.
        ruta, k = os.path.join(self._base, name), 2
        while os.path.exists(ruta):
            ruta, k = os.path.join(self._base, f"{name}_{k}"), k + 1
        self._sess_dir = ruta
        # La carpeta NO se crea aca: recien con el primer cruce de meta
        # (_crear_carpeta). Los metadatos si se toman ahora, con la hora de ENTRADA.
        self._meta_sesion = {
            "track": _safe(bytes(d.mTrackLocation)),
            "track_variation": _safe(bytes(d.mTrackVariation)),
            "car": _safe(bytes(d.mCarName)),
            "car_class": _safe(bytes(d.mCarClassName)),
            # Nombres TRADUCIDOS: son los que el juego muestra en pantalla y los que
            # usan las apps de telemetria de terceros para decidir que sesiones son
            # comparables entre si. No son iguales a los crudos -- la variante cruda
            # de Spielberg es "Spielberg_Modern" y la traducida es "Spielberg" --
            # asi que exportar con la cruda deja nuestras sesiones sin pareja.
            # Se guardan SIN sanear: tienen que salir tal cual para que matcheen.
            "track_tr": _txt(bytes(d.mTranslatedTrackLocation)),
            "track_variation_tr": _txt(bytes(d.mTranslatedTrackVariation)),
            "session": label,
            "track_length_m": round(d.mTrackLength, 1),
            "started": datetime.now().isoformat(timespec="seconds"),
            "rate_hz": round(1.0 / self._period),
            "channels": HEADER,
        }

    # ---------------- linea de tiempo (aditiva, no toca vueltas limpias) ----------------
    def _track_rivals(self, d, v):
        """Mejores tiempos de CADA piloto de la sesion (incluido tu).

        Es lo unico que la shared memory publica de los demas autos: nombre, posicion,
        vueltas, mejor vuelta y mejores sectores. NO hay acelerador/freno/volante ajenos
        -- podras comparar TIEMPOS y sectores contra un piloto de referencia, no sus inputs.

        Se guarda junto al resto de la sesion (telemetry/, gitignoreado): es tu registro
        personal de con quien giraste y como te fue. No sale del PC.
        """
        # Los arrays por participante son opcionales a proposito: si una version del juego
        # (o un mock) no los trae, el registro de rivales se salta -- pero la GRABACION
        # sigue. Sin esto, un AttributeError aca lo traga el except del hilo y se deja de
        # grabar en silencio, que es el peor modo de falla posible.
        try:
            best_all = d.mFastestLapTimes
            s1, s2, s3 = d.mFastestSector1Times, d.mFastestSector2Times, d.mFastestSector3Times
            inv_all = d.mLapsInvalidated
        except AttributeError:
            return
        n = max(0, min(d.mNumParticipants, ams2_shm.STORED_PARTICIPANTS_MAX))
        for i in range(n):
            pi = d.mParticipantInfo[i]
            if not pi.mIsActive:
                continue
            name = bytes(pi.mName).split(b"\x00")[0].decode("utf-8", "replace").strip()
            if not name:
                continue
            best = best_all[i]
            rec = {
                "best": round(best, 3) if best > 0 else None,
                "sec": [round(x, 3) if x > 0 else None for x in (s1[i], s2[i], s3[i])],
                "pos": int(pi.mRacePosition),
                "laps": int(pi.mLapsCompleted),
                "inv": bool(inv_all[i]),
                "me": i == v,
            }
            if self._rivals.get(name) != rec:
                self._rivals[name] = rec
                self._rivals_dirty = True

    def _write_rivals(self):
        """Vuelca el registro de rivales. Se llama al cerrar vuelta y al rotar sesion,
        NO por frame: escribir a 50Hz stutearia el broadcast."""
        if self._sess_dir is None or not self._creada or not self._rivals_dirty or not self._rivals:
            return                        # sin carpeta todavia: queda pendiente (dirty)
        try:
            path = os.path.join(self._sess_dir, "rivals.json")
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._rivals, f, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(tmp, path)
            self._rivals_dirty = False
        except OSError:
            pass

    def _write_timeline(self, rec):
        """Append de un registro a timeline.jsonl. Guarda ante OSError y sin carpeta de sesion.
        Antes de crear la carpeta, los eventos se guardan en memoria y una vuelta la crea."""
        if self._sess_dir is None:
            return
        if not self._creada:
            if rec.get("type") != "lap":
                self._eventos_pend.append(rec)
                return
            self._crear_carpeta()
            if not self._creada:
                return
        try:
            with open(os.path.join(self._sess_dir, "timeline.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _timeline_event(self, event, d, p, extra=None):
        """Emite un evento discreto (start / pit_in / pit_out / tyre_change) a la linea de tiempo."""
        rec = {"type": "event", "event": event, "lap": int(p.mLapsCompleted),
               "rain": round(d.mRainDensity, 3), "track_t": round(d.mTrackTemperature, 1),
               "fuel_l": round(d.mFuelLevel * self._cap, 2),
               "ts": datetime.now().isoformat(timespec="seconds")}
        if extra:
            rec.update(extra)
        self._write_timeline(rec)

    def _volcar_traza(self, tname):
        """Escribe el buffer como traza: a .tmp y os.replace, para que bajo su nombre
        final solo exista la traza COMPLETA. False si fallo el disco."""
        ruta = os.path.join(self._sess_dir, tname)
        try:
            with gzip.open(ruta + ".tmp", "wt", newline="", encoding="utf-8") as f:
                f.write(",".join(HEADER) + "\n")
                f.write("\n".join(self._buf))
                f.write("\n")
            os.replace(ruta + ".tmp", ruta)
            return True
        except OSError:
            return False

    def _commit_lap(self, d, p, contada=True):
        """Cierra la vuelta. SIEMPRE (si es vuelta de pista, no out/in/pit) guarda un
        registro de sectores con validez por sector -> permite rescatar sectores limpios
        de vueltas invalidadas. La traza+resumen completos solo si la vuelta es 100% limpia.

        `contada=False`: la cierra un cruce que AMS2 no conto (ver _cruce_sin_contar).
        Es una out-lap: no tiene tiempo propio, y mLastLapTime es el de otra vuelta."""
        # El primer cruce de meta de la sesion es lo que crea la carpeta en disco (ver
        # _crear_carpeta): antes de eso no habia nada que guardar.
        if self._sess_dir is not None and self._mode != "off":
            self._crear_carpeta()
        # Los mejores de cada piloto solo pueden haber mejorado al cruzar meta -> es el
        # momento natural de volcarlos, y saca la escritura del camino de los 50Hz.
        if self._mode != "off":
            self._write_rivals()
        largada, self._largada = self._largada, False
        self._anclar_en_meta = False
        n_samples = self._agg["n"] if self._agg else 0
        de_pista = (self._lap_ok and self._mode != "off"
                    and not self._pit_this and not self._pit_prev
                    and self._sess_dir is not None
                    and n_samples >= MIN_LAP_SAMPLES)
        # La vuelta 1 de carrera guarda traza (S###) y linea de tiempo, pero NO
        # sectores ni resumen: largada y trafico no son una vuelta representativa.
        flying = de_pista and not largada
        # --- linea de tiempo: UNA linea por CADA vuelta cruzada (out/in/pit/invalida/corta) ---
        # gateado en modo != off (en off _commit_lap SI se llama, pero no debe escribir nada).
        lap_no = int(p.mLapsCompleted)
        lap_time = d.mLastLapTime if contada and d.mLastLapTime > 0 else None
        # Nombre de la traza. Prefijo L = vuelta limpia, X = vuelta INVALIDADA por
        # limites de pista. La distincion es deliberada y no cosmetica: todo el
        # analisis del repo llega a las trazas por summary.jsonl (que solo lista
        # las limpias), asi que mezclar las invalidadas bajo el mismo prefijo
        # cambiaria en silencio el corpus de cada herramienta. Con X quedan
        # visibles SOLO para quien las pida por la linea de tiempo -- el visor,
        # que las necesita para comparar la tanda vuelta por vuelta.
        tname = None
        if de_pista and self._mode == "full":
            prefijo = "S" if largada else "X" if self._invalid else "L"
            tname = f"{prefijo}{lap_no:03d}_{(lap_time if lap_time else 0):.3f}s.csv.gz"
        linea_tl = None
        if self._sess_dir is not None and self._mode != "off":
            if self._pit_this:
                kind = "pit"
            elif self._pit_prev:
                kind = "out"
            elif largada:
                kind = "start"
            elif self._invalid:
                kind = "invalid"
            elif n_samples < MIN_LAP_SAMPLES:
                kind = "short"
            else:
                kind = "flying"
            a = self._agg
            n = max(1, a["n"]) if a else 1
            # Se ARMA aca pero se escribe despues de la traza: la linea de tiempo es
            # la que le anuncia la traza a los lectores (el visor sobre la sesion en
            # curso), y anunciarla antes de que exista daba "no existe esa traza"
            # justo mientras se volcaba.
            linea_tl = {
                "type": "lap", "lap": lap_no, "lap_time": round(lap_time, 3) if lap_time else None,
                "kind": kind, "pit": bool(self._pit_this), "out": bool(self._pit_prev),
                "invalid": bool(self._invalid), "samples": n_samples,
                # uid AUN NO asignado (se incrementa mas abajo, solo para vueltas de
                # pista): se anticipa aca para que la linea de tiempo pueda cruzarse
                # con summary.jsonl y sectors.jsonl sin adivinar por orden.
                "uid": (self._lap_uid + 1) if flying else None,
                "trace": tname,
                "compound": [_label(bytes(d.mTyreCompound[i])) for i in range(4)],
                "fuel_start": round(self._start["fuel"], 2) if self._start else None,
                "fuel_end": round(d.mFuelLevel * self._cap, 2),
                "rain": round(d.mRainDensity, 3), "track_t": round(d.mTrackTemperature, 1),
                "tyre_temp_avg": [round(a["tsum"][i] / n, 1) for i in range(4)] if a else None,
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
        if not flying:
            if tname and not self._volcar_traza(tname) and linea_tl:
                linea_tl["trace"] = None          # no anunciar una traza que no existe
            if linea_tl:
                self._write_timeline(linea_tl)
            return
        self._lap_uid += 1                 # identidad unica (el numero de vuelta se repite tras el garage)
        # sectores: S1/S2 capturados en vivo (estables), S3 = total - S1 - S2 (robusto)
        s1, s2 = self._sectimes
        if lap_time and s1 > 0 and s2 > 0 and (lap_time - s1 - s2) > 0:
            sectors = [s1, s2, round(lap_time - s1 - s2, 3)]
        else:
            sectors = [round(d.mCurrentSector1Time, 3), round(d.mCurrentSector2Time, 3),
                       round(d.mCurrentSector3Time, 3)]
        # La TRAZA se guarda tambien si la vuelta se invalido (como X###). Una
        # vuelta invalidada por pisar un piano sigue teniendo la frenada, la
        # trazada y los cambios buenos: para comparar una tanda vuelta por vuelta
        # descartarla es perder la mitad de la sesion. Lo que NO se guarda es su
        # resumen: eso mantiene intacto el corpus de las herramientas de analisis,
        # que se apoyan en que summary.jsonl solo tiene vueltas representativas.
        #
        # Orden de escritura: traza (a .tmp y os.replace) -> linea de tiempo ->
        # sectores -> resumen. Asi, bajo su nombre final solo existe la traza
        # COMPLETA, y para cuando la linea de tiempo la anuncia ya esta; y el
        # resumen sigue escribiendose despues de la linea de tiempo, que es lo que
        # evita vueltas "huerfanas" (ver analyze_telemetry._partir_corridas).
        if tname and not self._volcar_traza(tname):
            tname = None
            if linea_tl:
                linea_tl["trace"] = None      # no anunciar una traza que no existe
        if linea_tl:
            self._write_timeline(linea_tl)
        # registro de sectores para TODA vuelta de pista (limpia o invalidada) -> rescate
        try:
            with open(os.path.join(self._sess_dir, "sectors.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "uid": self._lap_uid,
                    "lap": lap_no, "lap_time": round(lap_time, 3) if lap_time else None,
                    "sectors": sectors, "sec_valid": [not x for x in self._sec_invalid],
                    "invalid": bool(self._invalid),
                    # Version del esquema. Sin esto no hay forma de saber si un
                    # sectors.jsonl viejo trae la atribucion contaminada: los registros
                    # sin `sv` son anteriores al 2026-08-20 y su sec_valid no sirve.
                    "sv": 2,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if self._invalid:     # vuelta sucia: sectores y traza guardados, sin resumen
            return
        a, st = self._agg, self._start
        end_fuel = d.mFuelLevel * self._cap
        end_wear = [d.mTyreWear[i] for i in range(4)]
        try:
            n = max(1, a["n"]) if a else 1
            summary = {
                "uid": self._lap_uid,
                "lap": lap_no,
                "lap_time": round(lap_time, 3) if lap_time else None,
                "valid": True,
                "samples": n_samples,
                "fuel_start": round(st["fuel"], 2) if st else None,
                "fuel_end": round(end_fuel, 2),
                "fuel_used": round(st["fuel"] - end_fuel, 2) if st else None,
                "wear_start": [round(x, 5) for x in st["wear"]] if st else None,
                "wear_end": [round(x, 5) for x in end_wear],
                "wear_delta": [round(end_wear[i] - st["wear"][i], 5) for i in range(4)] if st else None,
                "tyre_temp_min": [round(x, 1) for x in a["tmin"]] if a else None,
                "tyre_temp_max": [round(x, 1) for x in a["tmax"]] if a else None,
                "tyre_temp_avg": [round(a["tsum"][i] / n, 1) for i in range(4)] if a else None,
                "brake_temp_max": [round(x, 1) for x in a["bmax"]] if a else None,
                "ambient_t": round(d.mAmbientTemperature, 1),
                "track_t": round(d.mTrackTemperature, 1),
                "rain": round(d.mRainDensity, 3),
                "sectors": sectors,
                "compound": _safe(bytes(d.mTyreCompound[0])),
                "tc_setting": int(d.mTractionControlSetting),    # nivel TC configurado (verificar si AMS2 lo puebla)
                "abs_setting": int(d.mAntiLockSetting),          # nivel ABS configurado
                "brake_bias": round(d.mBrakeBias, 4),            # fraccion delantera (0.48 = 48% adelante / 52% atras)
                "drs": int(d.mDrsState),
                "trace": tname,
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
            with open(os.path.join(self._sess_dir, "summary.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(summary, ensure_ascii=False) + "\n")
            self._laps_logged += 1
            self._last_file = tname or f"L{lap_no:03d} (resumen)"
        except OSError:
            pass
