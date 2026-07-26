#!/usr/bin/env python3
"""Logger de telemetria por vuelta de AMS2 (shared memory) para analisis offline.

Corre su propio hilo con su propio Reader (~50 Hz). Detecta cruces de meta y, por
cada vuelta VALIDA (no invalidada por limites de pista, ni out/in-lap de boxes),
vuelca:

  telemetry/<pista>__<auto>__<sesion>__<fecha>/
      session.json                 metadatos de la sesion
      summary.jsonl                1 linea por vuelta valida (resumen)
      L003_92.451s.csv.gz          traza completa de la vuelta (todos los canales)
      timeline.jsonl               linea de tiempo COMPLETA (aditiva): 1 registro por CADA
                                   vuelta cruzada (out/in/pit/invalida/corta incluidas) +
                                   eventos (largada, pit_in/out, cambio de goma)

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
TELEM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry")

_LIVE = (ams2_shm.GAME_INGAME_PLAYING, ams2_shm.GAME_INGAME_INMENU_TIME_TICKING)
_PIT_INSIDE = (1, 2, 4)       # mPitMode entrando/parado/garage -> en boxes
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

    def __init__(self, base_dir=TELEM_DIR, rate_hz=RATE_HZ):
        self._lock = threading.Lock()
        self._stop = False
        self._thread = None
        self._mode = "full"       # "off" | "summary" (liviano) | "full" (traza completa)
        self._base = base_dir
        self._period = 1.0 / rate_hz
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
        self._cap = 100.0
        self._last_lap = -1
        self._buf = []            # filas CSV (str) de la vuelta en curso
        self._invalid = False     # la vuelta se invalido (limites de pista)
        self._lap_ok = False      # estuvo habilitado toda la vuelta
        self._pit_this = True     # hubo pit en esta vuelta (1ra de sesion: si)
        self._pit_prev = True     # hubo pit en la vuelta anterior (-> out-lap)
        self._in_pit_prev = False
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
        full = resumen + traza completa de 71 canales (~50Hz)."""
        if mode in ("off", "summary", "full"):
            with self._lock:
                self._mode = mode

    def set_enabled(self, on):
        self.set_mode("full" if on else "off")   # compat con el toggle viejo

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
                time.sleep(0.5)
                continue
            if reader is None:
                try:
                    reader = ams2_shm.Reader().open()
                except ams2_shm.SharedMemoryUnavailable:
                    time.sleep(1.0)
                    continue
            try:
                d = reader.snapshot()
            except OSError:
                reader.close()
                reader = None
                time.sleep(1.0)
                continue
            try:
                self._ingest(d)
            except Exception:
                pass                      # nunca tumbar el hilo por un error de I/O
            time.sleep(self._period if mode == "full" else 0.1)   # full 50Hz, summary ~10Hz

    def _ingest(self, d):
        if d.mVersion != ams2_shm.SHARED_MEMORY_VERSION or d.mNumParticipants <= 0:
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
                self._rotate_session(d, sig)
            self._track_rivals(d, v)      # acumula en memoria; se escribe al cerrar vuelta

            in_pit = d.mPitMode in _PIT_INSIDE
            if in_pit:
                self._pit_this = True

            lap = p.mLapsCompleted
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

            # captura de sectores en vivo: S1/S2 se finalizan a mitad de vuelta y quedan
            # estables hasta meta (el bug viejo leia mCurrentSector1Time ya reseteado al cruzar).
            if d.mCurrentSector1Time > 0.1:
                self._sectimes[0] = round(d.mCurrentSector1Time, 3)
            if d.mCurrentSector2Time > 0.1:
                self._sectimes[1] = round(d.mCurrentSector2Time, 3)
            if bool(d.mLapInvalidated):
                self._invalid = True
                # atribuir la invalidacion al sector EN CURSO; los previos quedan limpios
                cs = 0 if self._sectimes[0] <= 0.1 else (1 if self._sectimes[1] <= 0.1 else 2)
                self._sec_invalid[cs] = True
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
            if self._mode == "full":                  # la traza completa solo en full
                self._buf.append(_row(d, p, cap))
            self._recording = True

    # ---------------- ciclo de vuelta / sesion ----------------
    def _begin_lap(self, d, p, cap):
        self._buf = []
        self._invalid = False
        self._lap_ok = self._mode != "off"
        self._pit_prev = self._pit_this
        self._pit_this = bool(d.mPitMode in _PIT_INSIDE)
        self._sectimes = [0.0, 0.0]                  # S1, S2 capturados en vivo (S3 = total - S1 - S2)
        self._sec_invalid = [False, False, False]    # validez por sector (limites de pista)
        self._agg = {"tmin": [9e9] * 4, "tmax": [-9e9] * 4, "tsum": [0.0] * 4,
                     "bmax": [-9e9] * 4, "n": 0}
        self._start = {"fuel": d.mFuelLevel * cap,
                       "wear": [d.mTyreWear[i] for i in range(4)]}

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

    def _rotate_session(self, d, sig):
        # cambio de pista/auto/tipo-de-sesion -> nueva carpeta; la vuelta en curso se descarta
        # Rivales: se vacian aca o el rivals.json de la carrera arrastraria a los que solo
        # corrieron la qualy, con tiempos de la sesion (o la PISTA) anterior.
        self._rivals = {}
        self._rivals_dirty = False
        self._sig = sig
        self._last_lap = -1
        self._buf = []
        self._invalid = False
        self._pit_this = True
        self._pit_prev = True
        self._agg = None
        self._compound_prev = None    # sesion nueva -> no emitir tyre_change espurio
        self._in_pit_prev = False      # ni pit_in/out espurio del estado anterior
        label = _SESS.get(int(d.mSessionState), "session")
        self._sess_label = label
        name = (f"{_safe(bytes(d.mTrackLocation))}__{_safe(bytes(d.mCarName))}"
                f"__{label}__{datetime.now():%Y%m%d_%H%M%S}")
        self._sess_dir = os.path.join(self._base, name)
        try:
            os.makedirs(self._sess_dir, exist_ok=True)
            meta = {
                "track": _safe(bytes(d.mTrackLocation)),
                "track_variation": _safe(bytes(d.mTrackVariation)),
                "car": _safe(bytes(d.mCarName)),
                "car_class": _safe(bytes(d.mCarClassName)),
                "session": label,
                "track_length_m": round(d.mTrackLength, 1),
                "started": datetime.now().isoformat(timespec="seconds"),
                "rate_hz": round(1.0 / self._period),
                "channels": HEADER,
            }
            with open(os.path.join(self._sess_dir, "session.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except OSError:
            self._sess_dir = None

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
        if self._sess_dir is None or not self._rivals_dirty or not self._rivals:
            return
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
        """Append de un registro a timeline.jsonl. Guarda ante OSError y sin carpeta de sesion."""
        if self._sess_dir is None:
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

    def _commit_lap(self, d, p):
        """Cierra la vuelta. SIEMPRE (si es vuelta de pista, no out/in/pit) guarda un
        registro de sectores con validez por sector -> permite rescatar sectores limpios
        de vueltas invalidadas. La traza+resumen completos solo si la vuelta es 100% limpia."""
        # Los mejores de cada piloto solo pueden haber mejorado al cruzar meta -> es el
        # momento natural de volcarlos, y saca la escritura del camino de los 50Hz.
        if self._mode != "off":
            self._write_rivals()
        n_samples = self._agg["n"] if self._agg else 0
        flying = (self._lap_ok and self._mode != "off"
                  and not self._pit_this and not self._pit_prev
                  and self._sess_dir is not None
                  and n_samples >= MIN_LAP_SAMPLES)
        # --- linea de tiempo: UNA linea por CADA vuelta cruzada (out/in/pit/invalida/corta) ---
        # gateado en modo != off (en off _commit_lap SI se llama, pero no debe escribir nada).
        lap_no = int(p.mLapsCompleted)
        lap_time = d.mLastLapTime if d.mLastLapTime > 0 else None
        if self._sess_dir is not None and self._mode != "off":
            if self._pit_this:
                kind = "pit"
            elif self._pit_prev:
                kind = "out"
            elif self._invalid:
                kind = "invalid"
            elif n_samples < MIN_LAP_SAMPLES:
                kind = "short"
            else:
                kind = "flying"
            a = self._agg
            n = max(1, a["n"]) if a else 1
            self._write_timeline({
                "type": "lap", "lap": lap_no, "lap_time": round(lap_time, 3) if lap_time else None,
                "kind": kind, "pit": bool(self._pit_this), "out": bool(self._pit_prev),
                "invalid": bool(self._invalid), "samples": n_samples,
                "compound": [_label(bytes(d.mTyreCompound[i])) for i in range(4)],
                "fuel_start": round(self._start["fuel"], 2) if self._start else None,
                "fuel_end": round(d.mFuelLevel * self._cap, 2),
                "rain": round(d.mRainDensity, 3), "track_t": round(d.mTrackTemperature, 1),
                "tyre_temp_avg": [round(a["tsum"][i] / n, 1) for i in range(4)] if a else None,
                "ts": datetime.now().isoformat(timespec="seconds"),
            })
        if not flying:
            return
        self._lap_uid += 1                 # identidad unica (el numero de vuelta se repite tras el garage)
        # sectores: S1/S2 capturados en vivo (estables), S3 = total - S1 - S2 (robusto)
        s1, s2 = self._sectimes
        if lap_time and s1 > 0 and s2 > 0 and (lap_time - s1 - s2) > 0:
            sectors = [s1, s2, round(lap_time - s1 - s2, 3)]
        else:
            sectors = [round(d.mCurrentSector1Time, 3), round(d.mCurrentSector2Time, 3),
                       round(d.mCurrentSector3Time, 3)]
        # registro de sectores para TODA vuelta de pista (limpia o invalidada) -> rescate
        try:
            with open(os.path.join(self._sess_dir, "sectors.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "uid": self._lap_uid,
                    "lap": lap_no, "lap_time": round(lap_time, 3) if lap_time else None,
                    "sectors": sectors, "sec_valid": [not x for x in self._sec_invalid],
                    "invalid": bool(self._invalid),
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if self._invalid:     # vuelta sucia: sectores rescatables guardados, pero sin traza/resumen
            return
        a, st = self._agg, self._start
        end_fuel = d.mFuelLevel * self._cap
        end_wear = [d.mTyreWear[i] for i in range(4)]
        try:
            tname = None
            if self._mode == "full":               # la traza completa solo en modo full
                tname = f"L{lap_no:03d}_{(lap_time if lap_time else 0):.3f}s.csv.gz"
                with gzip.open(os.path.join(self._sess_dir, tname), "wt",
                               newline="", encoding="utf-8") as f:
                    f.write(",".join(HEADER) + "\n")
                    f.write("\n".join(self._buf))
                    f.write("\n")
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
