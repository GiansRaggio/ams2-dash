#!/usr/bin/env python3
"""Logger de telemetria por vuelta de AMS2 (shared memory) para analisis offline.

Corre su propio hilo con su propio Reader (~50 Hz). Detecta cruces de meta y, por
cada vuelta VALIDA (no invalidada por limites de pista, ni out/in-lap de boxes),
vuelca:

  telemetry/<pista>__<auto>__<sesion>__<fecha>/
      session.json                 metadatos de la sesion
      summary.jsonl                1 linea por vuelta valida (resumen)
      L003_92.451s.csv.gz          traza completa de la vuelta (todos los canales)

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
        self._player_name = self._read_player_name()   # ancla al auto del jugador (no al de la camara)
        self._reset_session()

    def _read_player_name(self):
        """Nombre del jugador para anclar el recorder a SU participante (no al que mira
        la camara). Orden: env AMS2_PLAYER_NAME, luego player.txt junto al script.
        Vacio => cae al comportamiento viejo (mViewedParticipantIndex)."""
        name = os.environ.get("AMS2_PLAYER_NAME", "").strip()
        if not name:
            try:
                pf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player.txt")
                if os.path.exists(pf):
                    name = open(pf, encoding="utf-8").read().strip()
            except Exception:
                name = ""
        return name.lower()

    def _player_idx(self, d):
        """Indice del participante del JUGADOR. En multiplayer mViewedParticipantIndex
        sigue a la CAMARA (transmision / otros pilotos), NO a tu auto -> corrompe la
        deteccion de vuelta y la distancia de traza (mezcla tu fisica con las vueltas de
        otro). Anclamos por NOMBRE (inmune a la camara). Fallback: el visto, si no hay
        nombre configurado o no matchea (p.ej. single-player, donde visto == jugador)."""
        n = max(0, min(d.mNumParticipants, ams2_shm.STORED_PARTICIPANTS_MAX))
        if self._player_name:
            for i in range(n):
                p = d.mParticipantInfo[i]
                if p.mIsActive and self._player_name in \
                        bytes(p.mName).split(b"\x00")[0].decode("utf-8", "replace").lower():
                    return i
        v = d.mViewedParticipantIndex
        return v if 0 <= v < n else 0

    def _reset_session(self):
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
        v = self._player_idx(d)                   # TU auto, no el que mira la camara (bug MP)
        if not (0 <= v < ams2_shm.STORED_PARTICIPANTS_MAX):
            return
        p = d.mParticipantInfo[v]

        with self._lock:
            if d.mFuelCapacity > 1.0:
                self._cap = d.mFuelCapacity
            cap = self._cap

            sig = (bytes(d.mTrackLocation), bytes(d.mCarName), int(d.mSessionState))
            if sig != self._sig:
                self._rotate_session(d, sig)

            in_pit = d.mPitMode in _PIT_INSIDE
            if in_pit:
                self._pit_this = True
            self._in_pit_prev = in_pit

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
                return
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
        self._sig = sig
        self._last_lap = -1
        self._buf = []
        self._invalid = False
        self._pit_this = True
        self._pit_prev = True
        self._agg = None
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

    def _commit_lap(self, d, p):
        """Cierra la vuelta. SIEMPRE (si es vuelta de pista, no out/in/pit) guarda un
        registro de sectores con validez por sector -> permite rescatar sectores limpios
        de vueltas invalidadas. La traza+resumen completos solo si la vuelta es 100% limpia."""
        n_samples = self._agg["n"] if self._agg else 0
        flying = (self._lap_ok and self._mode != "off"
                  and not self._pit_this and not self._pit_prev
                  and self._sess_dir is not None
                  and n_samples >= MIN_LAP_SAMPLES)
        if not flying:
            return
        lap_time = d.mLastLapTime if d.mLastLapTime > 0 else None
        lap_no = int(p.mLapsCompleted)
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
