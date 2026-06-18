#!/usr/bin/env python3
"""Tests del TelemetryLogger con snapshots sinteticos (sin AMS2 en vivo).

Verifica: se guarda una vuelta valida (traza csv.gz + linea de resumen), se
descartan out-lap, vuelta invalida y grabacion deshabilitada. Escribe a un dir
temporal que se limpia al final. Correr: python tools/test_telemetry.py
"""
import gzip
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ams2_telemetry as T


class P:
    def __init__(self, laps_completed=0, dist=0.0):
        self.mLapsCompleted = laps_completed
        self.mCurrentLap = laps_completed + 1
        self.mCurrentLapDistance = dist
        self.mWorldPosition = [10.0, 1.0, 20.0]
        self.mRacePosition = 1
        self.mIsActive = True


class Snap:
    def __init__(self, laps_completed=0, invalid=False, pit=0, dist=0.0):
        self.mVersion = 14
        self.mNumParticipants = 5
        self.mViewedParticipantIndex = 0
        self.mGameState = 2          # PLAYING
        self.mSessionState = 1       # PRACTICE
        self.mTrackLocation = b"Nordschleife"
        self.mTrackVariation = b"24h"
        self.mCarName = b"Porsche 992 GT3 R"
        self.mCarClassName = b"GT3"
        self.mTrackLength = 20832.0
        self.mTyreCompound = [b"Soft", b"Soft", b"Soft", b"Soft"]
        self.mPitMode = pit
        self.mLapInvalidated = invalid
        self.mLastLapTime = 472.5
        self.mCurrentTime = 120.0
        self.mFuelCapacity = 90.0
        self.mFuelLevel = 0.5
        # canales escalares
        self.mSpeed = 55.0
        self.mRpm = 7200.0
        self.mGear = 4
        self.mUnfilteredThrottle = 0.8
        self.mUnfilteredBrake = 0.0
        self.mUnfilteredClutch = 0.0
        self.mUnfilteredSteering = 0.1
        self.mSteering = 0.1
        self.mBrakeBias = 0.56
        self.mLocalAcceleration = [2.0, 9.8, 1.0]
        self.mOrientation = [0.5, 0.01, 0.02]
        self.mWaterTempCelsius = 90.0
        self.mOilTempCelsius = 105.0
        self.mAmbientTemperature = 22.0
        self.mTrackTemperature = 31.0
        self.mRainDensity = 0.0
        self.mCurrentSector1Time = 100.0
        self.mCurrentSector2Time = 180.0
        self.mCurrentSector3Time = 192.5
        # canales por esquina
        q = lambda v: [v, v, v, v]
        self.mTyreTemp = q(88.0)
        self.mTyreTempLeft = q(86.0)
        self.mTyreTempCenter = q(89.0)
        self.mTyreTempRight = q(90.0)
        self.mBrakeTempCelsius = q(350.0)
        self.mTyreWear = q(0.05)
        self.mSuspensionTravel = q(0.03)
        self.mSuspensionVelocity = q(0.1)
        self.mTyreSlipSpeed = q(0.5)
        self.mRideHeight = q(0.06)
        self.mAirPressure = q(165.0)
        self.mTyreRPS = q(120.0)
        self._p = P(laps_completed, dist)

    @property
    def mParticipantInfo(self):
        return [self._p] * 64


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


def feed_lap(log, lap_completed, n=260, invalid=False, pit=0, fuel0=0.5, wear0=0.05):
    """Alimenta n muestras de una vuelta (mLapsCompleted=lap_completed)."""
    for k in range(n):
        s = Snap(laps_completed=lap_completed, invalid=invalid, pit=pit,
                 dist=k / n * 20000.0)
        s.mFuelLevel = fuel0 - k / n * 0.03      # va consumiendo
        s.mTyreWear = [wear0 + k / n * 0.02] * 4
        log._ingest(s)


def cross_to(log, lap_completed, fuel=0.5, wear=0.05):
    """Una muestra con el nuevo contador de vueltas -> dispara commit + begin.

    El combustible/desgaste se leen EN EL CRUCE DE META (begin/commit usan estos
    snapshots), asi que para simular consumo hay que bajar fuel entre cruces.
    """
    s = Snap(laps_completed=lap_completed, dist=0.0)
    s.mFuelLevel = fuel
    s.mTyreWear = [wear] * 4
    log._ingest(s)


def session_dir(base):
    subs = [os.path.join(base, d) for d in os.listdir(base)
            if os.path.isdir(os.path.join(base, d))]
    return subs[0] if subs else None


def main():
    base = tempfile.mkdtemp(prefix="ams2tel_")
    try:
        log = T.TelemetryLogger(base_dir=base)

        print("test_valid_lap (lap1 limpia -> traza + resumen):")
        feed_lap(log, 0)                 # lap0 = out-lap inicial (se descarta)
        cross_to(log, 1, fuel=0.50, wear=0.05)   # begin lap1 (45 L, wear 0.05)
        feed_lap(log, 1)                 # lap1 limpia
        cross_to(log, 2, fuel=0.45, wear=0.10)   # commit lap1: uso 4.5 L, deg 0.05
        sd = session_dir(base)
        _ok("carpeta de sesion creada", sd is not None)
        traces = [f for f in os.listdir(sd) if f.endswith(".csv.gz")] if sd else []
        _ok("1 traza .csv.gz", len(traces) == 1, traces)
        sumf = os.path.join(sd, "summary.jsonl")
        lines = open(sumf, encoding="utf-8").read().strip().splitlines() if os.path.exists(sumf) else []
        _ok("1 linea de resumen", len(lines) == 1, len(lines))
        if lines:
            rec = json.loads(lines[0])
            _ok("resumen valido=True", rec["valid"] is True)
            _ok("fuel_used > 0", rec["fuel_used"] > 0, rec["fuel_used"])
            _ok("wear_delta presente", rec["wear_delta"] is not None, rec["wear_delta"])
            _ok("tyre_temp_avg presente", rec["tyre_temp_avg"] is not None)
        if traces:
            with gzip.open(os.path.join(sd, traces[0]), "rt", encoding="utf-8") as f:
                head = f.readline().strip().split(",")
                rows = f.read().strip().splitlines()
            _ok("header coincide con HEADER", head == T.HEADER, len(head))
            _ok("filas ~260", abs(len(rows) - 260) <= 2, len(rows))
            _ok("cada fila tiene todas las columnas",
                all(len(r.split(",")) == len(T.HEADER) for r in rows[:5]))

        print("test_invalid_lap (lap invalida -> NO se guarda):")
        log2 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel2_"))
        feed_lap(log2, 0); cross_to(log2, 1)
        feed_lap(log2, 1, invalid=True)          # se invalida
        cross_to(log2, 2)
        sd2 = session_dir(log2._base)
        tr2 = [f for f in os.listdir(sd2) if f.endswith(".csv.gz")] if sd2 else []
        _ok("vuelta invalida no guardada", len(tr2) == 0, tr2)
        shutil.rmtree(log2._base, ignore_errors=True)

        print("test_disabled (modo off -> NO se guarda):")
        log3 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel3_"))
        log3.set_mode("off")
        feed_lap(log3, 0); cross_to(log3, 1)
        feed_lap(log3, 1); cross_to(log3, 2)
        sd3 = session_dir(log3._base)
        tr3 = ([f for f in os.listdir(sd3) if f.endswith(".csv.gz")]
               if sd3 and os.path.isdir(sd3) else [])
        _ok("off no guarda", len(tr3) == 0, tr3)
        st = log3.status()
        _ok("status refleja mode=off", st["mode"] == "off" and st["enabled"] is False)
        shutil.rmtree(log3._base, ignore_errors=True)

        print("test_summary (modo resumen -> linea de resumen SIN traza):")
        log4 = T.TelemetryLogger(base_dir=tempfile.mkdtemp(prefix="ams2tel4_"))
        log4.set_mode("summary")
        feed_lap(log4, 0); cross_to(log4, 1, fuel=0.50, wear=0.05)
        feed_lap(log4, 1); cross_to(log4, 2, fuel=0.45, wear=0.10)
        sd4 = session_dir(log4._base)
        tr4 = [f for f in os.listdir(sd4) if f.endswith(".csv.gz")] if sd4 else []
        sumf4 = os.path.join(sd4, "summary.jsonl") if sd4 else ""
        lines4 = (open(sumf4, encoding="utf-8").read().strip().splitlines()
                  if os.path.exists(sumf4) else [])
        _ok("resumen: 1 linea", len(lines4) == 1, len(lines4))
        _ok("resumen: SIN traza .csv.gz", len(tr4) == 0, tr4)
        if lines4:
            rec4 = json.loads(lines4[0])
            _ok("resumen: trace=null", rec4["trace"] is None)
            _ok("resumen: fuel_used > 0", rec4["fuel_used"] > 0, rec4["fuel_used"])
        shutil.rmtree(log4._base, ignore_errors=True)

        print("done.")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
