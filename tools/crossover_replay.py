#!/usr/bin/env python3
"""Replay del detector de crossover lluvia->lisos contra sesiones GRABADAS.

Hermano de tools/tyre_replay.py, y por la misma razon: el crossover se habia
validado SOLO contra escenarios sinteticos (tools/test_strategy.py, feed_wet),
donde mTyreTemp siempre esta vivo porque lo escribe el propio test. En pista de
verdad ese canal se muere en la MAYORIA de las sesiones de lluvia (ver abajo), y
el mock no lo podia ver nunca.

Este script recorre telemetry/ alimentando frame a frame un StrategyEngine y un
TyreAnalyzer REALES -- los mismos que corre el bridge, en el mismo orden -- y
muestra que habria dicho el semaforo en cada cruce de meta.

QUE MIDE (una linea por vuelta cruzada):
  rain / trkT  : condiciones de esa vuelta (del summary.jsonl)
  bulk         : max(mTyreTemp) EN LA LINEA -- el canal que usaba la senal C
  carc         : max(mTyreCarcassTemp) en la linea, para comparar
  surf         : DEAD si el TyreAnalyzer declaro muerto el modelo de superficie
  estado       : el semaforo (green/amber/red/calibrando) + senales activas

DATOS: las trazas .csv.gz NO guardan lluvia ni temperatura de pista (son del
entorno, no del auto), asi que esas dos se leen de summary.jsonl y se mantienen
constantes durante la vuelta -- que es como las entrega el juego, y ademas el
detector solo las muestrea UNA vez por vuelta, al cruzar meta.

mSessionState se fuerza a RACE: el crossover solo corre en carrera y el archivo
tiene 2 carreras de lluvia; forzarlo deja medir tambien las practicas/quali con
goma de agua, que es donde vive casi toda la evidencia del canal muerto.

Uso:
    .venv\\Scripts\\python.exe tools\\crossover_replay.py            # sesiones con goma de agua
    .venv\\Scripts\\python.exe tools\\crossover_replay.py --all      # todas (incl. secas)
    .venv\\Scripts\\python.exe tools\\crossover_replay.py --match Kansai

Sale con codigo 1 si falla alguna VERIFICA (abajo).
"""
import argparse
import csv
import gzip
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ams2_strategy
import ams2_tyres

TDIR = os.path.join(HERE, "telemetry")
KELVIN = 273.15
C = ("FL", "FR", "RL", "RR")

# Sesiones de LLUVIA del archivo (goma de agua montada en al menos una vuelta).
# Las 6 marcadas DEAD son las mismas que tools/tyre_replay.py exige detectar: el
# 100% de las sesiones con modelo de superficie muerto son sesiones de lluvia.
DEAD_SURF = {
    "Kansai__Audi_R8_LMS_GT4__race__20260712_214749",
    "Jerez__Audi_R8_LMS_GT4__practice__20260628_204629",
    "Jerez__Audi_R8_LMS_GT4__practice__20260705_202843",
    "Jerez__Audi_R8_LMS_GT4__practice__20260705_210152",
    "Jerez__Audi_R8_LMS_GT4__practice__20260705_211334",
    "Jerez__Audi_R8_LMS_GT4__qualify__20260705_214529",
}
# La UNICA carrera del archivo que seco de verdad: Buenos Aires 21/06. El piloto
# aguanto en lluvia hasta la 13 y monto lisos en la 16 -> el lap-time cayo de
# ~132 s a ~124 s. O sea el cruce existio y llego TARDE: el semaforo tiene que
# haber estado en amber/red antes del cambio.
DRYING_RACE = "Buenos_Aires__Audi_R8_LMS_GT4__race__20260621_214635"
# Kansai fue al reves (lluvia 0.200 -> 0.306, cada vez mas mojado) con el bulk
# muerto: el semaforo NO puede mandar a boxes ahi. Es el falso positivo a batir.
WETTER_RACE = "Kansai__Audi_R8_LMS_GT4__race__20260712_214749"


def f(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return float("nan")


class Part:
    def __init__(self):
        self.mLapsCompleted = 0
        self.mCurrentLap = 1
        self.mRacePosition = 1
        self.mIsActive = True
        self.mName = b"YO"


class Snap:
    """Snapshot SHM de mentira: solo los campos que leen StrategyEngine y
    TyreAnalyzer, en las MISMAS unidades del SHM real (presion bar*100,
    layer/carcass en Kelvin, mTyreTemp en Celsius)."""

    def __init__(self, track, car):
        self.mVersion = ams2_strategy.ams2_shm.SHARED_MEMORY_VERSION
        self.mNumParticipants = 1
        self.mViewedParticipantIndex = 0
        self.mTrackLocation = track
        self.mCarName = car
        self.mGameState = 2                 # INGAME_PLAYING
        self.mSessionState = 5              # RACE (ver docstring)
        self.mFuelLevel = 0.5
        self.mFuelCapacity = 100.0
        self.mEventTimeRemaining = -1.0
        self.mSessionDuration = 0.0
        self.mSessionAdditionalLaps = 0
        self.mLapsInEvent = 30
        self.mEnforcedPitStopLap = -1
        self.mPitMode = 0
        self.mCurrentTime = 5.0
        self.mLastLapTime = 0.0
        self.mBestLapTime = 0.0
        self.mSpeed = 0.0
        self.mRainDensity = 0.0
        self.mTrackTemperature = 25.0
        self.mTyreCompound = [b"Liso"] * 4
        self.mTyreWear = [0.0] * 4
        self.mTyreTemp = [0.0] * 4
        self.mTyreCarcassTemp = [KELVIN] * 4
        self.mTyreLayerTemp = [KELVIN] * 4
        self.mAirPressure = [175.0] * 4
        self.mBrakeTempCelsius = [0.0] * 4
        self.mTyreTempLeft = [0.0] * 4
        self.mTyreTempRight = [0.0] * 4
        self._p = Part()

    @property
    def mParticipantInfo(self):
        return [self._p] * 64

    def load(self, row):
        """Carga los canales por rueda desde una fila del CSV."""
        self.mSpeed = f(row, "speed_kmh") / 3.6
        self.mTyreTemp = [f(row, f"tyre_temp_{c}") for c in C]
        self.mTyreCarcassTemp = [f(row, f"carcass_t_{c}") + KELVIN for c in C]
        self.mTyreLayerTemp = [f(row, f"layer_t_{c}") + KELVIN for c in C]
        self.mAirPressure = [f(row, f"tyre_press_{c}") for c in C]
        self.mBrakeTempCelsius = [f(row, f"brake_temp_{c}") for c in C]
        self.mTyreTempLeft = [f(row, f"tyre_t_in_{c}") for c in C]
        self.mTyreTempRight = [f(row, f"tyre_t_out_{c}") for c in C]
        self.mTyreWear = [f(row, f"tyre_wear_{c}") for c in C]
        fl = f(row, "fuel_l")
        if math.isfinite(fl):
            self.mFuelLevel = fl / self.mFuelCapacity


def lap_meta(sess_dir):
    """rain / track_t / compuesto / lap_time por numero de vuelta, del summary."""
    meta = {}
    p = os.path.join(sess_dir, "summary.jsonl")
    if not os.path.exists(p):
        return meta
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("lap") is None:
                continue
            meta[int(r["lap"])] = {
                "rain": r.get("rain", 0.0), "track_t": r.get("track_t", 25.0),
                "comp": (r.get("compound") or "Liso"), "lap_time": r.get("lap_time") or 0.0,
            }
    return meta


def has_wet(meta):
    """Alguna vuelta con goma de agua? Se decide por metadatos ANTES de descomprimir
    las trazas: sin esto el barrido descomprime las 141 sesiones para tirar 132."""
    for m in meta.values():
        c = str(m.get("comp", "")).lower()
        if any(k in c for k in ("wet", "rain", "lluv", "inter", "mojad",
                                "weather", "pluie", "regen")):
            return True
    return False


def replay(sess_dir, name):
    laps = sorted(x for x in os.listdir(sess_dir)
                  if x.startswith("L") and x.endswith(".csv.gz"))
    if not laps:
        return None
    meta = lap_meta(sess_dir)
    track = name.split("__")[0].encode()
    car = name.split("__")[1].encode() if "__" in name else b"car"

    eng = ams2_strategy.StrategyEngine()
    eng._player_name = ""                    # single-player: cae a mViewedParticipantIndex
    an = ams2_tyres.TyreAnalyzer(base_dir=os.path.join(sess_dir, "_no_state"))
    d = Snap(track, car)
    clock = 0.0
    rows_out = []
    any_wet = False

    for lp in laps:
        n = int(lp[1:4])
        m = meta.get(n, {})
        d.mRainDensity = float(m.get("rain", 0.0))
        d.mTrackTemperature = float(m.get("track_t", 25.0))
        comp = str(m.get("comp", "Liso")).encode("utf-8", "replace")
        d.mTyreCompound = [comp] * 4
        d._p.mLapsCompleted = n - 1
        d._p.mCurrentLap = n
        d.mLastLapTime = 0.0

        last = None
        with gzip.open(os.path.join(sess_dir, lp), "rt", newline="") as fh:
            prev_t = None
            for row in csv.DictReader(fh):
                t = f(row, "t")
                dt = 0.02 if (prev_t is None or not math.isfinite(t)) \
                    else min(max(t - prev_t, 0.0), 0.5)
                prev_t = t
                clock += dt
                d.load(row)
                # MISMO orden y MISMO cableado que bridge_shm: strategy primero (con la
                # liveness que midio tyres en el frame anterior), tyres despues.
                eng.update(d, surf_alive=an.surf_alive())
                an.update(d, wear=eng.wear_vec(d), now=clock)
                last = row
        if last is None:
            continue
        # --- cruce de meta: el tick donde mLapsCompleted incrementa ---
        d.load(last)
        d.mLastLapTime = float(m.get("lap_time") or 0.0)
        d._p.mLapsCompleted = n
        d._p.mCurrentLap = n + 1
        eng.update(d, surf_alive=an.surf_alive())
        an.update(d, wear=eng.wear_vec(d), now=clock)

        o = eng.payload()
        cx = o.get("crossover")
        tp = an.payload()
        wet = eng._is_wet_compound()
        any_wet = any_wet or wet
        rows_out.append({
            "lap": n, "rain": d.mRainDensity, "track_t": d.mTrackTemperature,
            "bulk": max(d.mTyreTemp), "carc": max(d.mTyreCarcassTemp) - KELVIN,
            "lap_time": d.mLastLapTime, "comp": comp.decode("utf-8", "replace"),
            "wet": wet, "surf_dead": tp["surf_dead"], "cx": cx,
        })
    return {"name": name, "rows": rows_out, "any_wet": any_wet}


def show(r):
    print(f"\n=== {r['name']} ===")
    print(f"{'lp':>3s} {'comp':>7s} {'rain':>6s} {'trkT':>6s} {'lap_t':>7s} {'bulk':>6s} "
          f"{'carc':>6s} {'surf':>5s} {'estado':>10s}  señales")
    for x in r["rows"]:
        cx = x["cx"]
        st = cx.get("state", "-") if cx else "(sin panel)"
        sig = " · ".join(cx.get("signals") or []) if cx else ""
        wt = cx.get("wet_temp") if cx else None
        surf = "DEAD" if x["surf_dead"] else "ok"
        extra = f"   [wet_temp={wt}]" if cx else ""
        print(f"{x['lap']:3d} {x['comp'][:7]:>7s} {x['rain']:6.3f} {x['track_t']:6.1f} "
              f"{x['lap_time']:7.2f} {x['bulk']:6.1f} {x['carc']:6.1f} {surf:>5s} "
              f"{st:>10s}  {sig}{extra}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="", help="substring del nombre de sesion")
    ap.add_argument("--all", action="store_true", help="incluir sesiones secas")
    args = ap.parse_args()

    results = []
    for name in sorted(os.listdir(TDIR)):
        d = os.path.join(TDIR, name)
        if not os.path.isdir(d) or (args.match and args.match not in name):
            continue
        if not args.all and not has_wet(lap_meta(d)):
            continue
        r = replay(d, name)
        if r is None or not r["rows"]:
            continue
        if not args.all and not r["any_wet"]:
            continue
        results.append(r)
        show(r)

    fails = []
    for r in results:
        wet_rows = [x for x in r["rows"] if x["wet"] and x["cx"]]
        if not wet_rows:
            continue
        dead_expected = r["name"] in DEAD_SURF

        # 1) donde el bulk esta muerto, la senal C no puede aparecer: es el canal
        #    del bug leido como si fuera una goma fria de verdad.
        for x in wet_rows:
            if x["surf_dead"] and "gomas de lluvia recalentando" in (x["cx"].get("signals") or []):
                fails.append(f"{r['name']} v{x['lap']}: senal C con modelo de superficie MUERTO")
            if x["surf_dead"] and x["cx"].get("wet_temp") is not None:
                fails.append(f"{r['name']} v{x['lap']}: wet_temp={x['cx']['wet_temp']} "
                             f"emitido con el canal muerto (bulk={x['bulk']:.0f})")

        # 2) el detector no puede mandar a boxes en la carrera que se MOJABA.
        if r["name"] == WETTER_RACE:
            bad = [x["lap"] for x in wet_rows if x["cx"].get("state") in ("amber", "red")]
            if bad:
                fails.append(f"{WETTER_RACE}: cruce en amber/red con lluvia EN AUMENTO "
                             f"(vueltas {bad})")

        # 3) el detector tiene que haber avisado en la carrera que SECO, antes del
        #    cambio real (vuelta 16). Aviso = amber o red en alguna vuelta con wets.
        if r["name"] == DRYING_RACE:
            warned = [x["lap"] for x in wet_rows if x["cx"].get("state") in ("amber", "red")]
            if not warned:
                fails.append(f"{DRYING_RACE}: pista secandose y el semaforo nunca aviso")
            else:
                print(f"\n{DRYING_RACE}: aviso en vueltas {warned} "
                      f"(cambio real a lisos en la 16)")

        if dead_expected and not any(x["surf_dead"] for x in r["rows"]):
            fails.append(f"{r['name']}: modelo de superficie muerto NO detectado")

    print(f"\n{len(results)} sesiones reproducidas "
          f"({sum(1 for r in results if r['any_wet'])} con goma de agua).")
    if fails:
        print("FALLAS:")
        for x in fails:
            print("  - " + x)
        sys.exit(1)
    print("VERIFICA: OK")


if __name__ == "__main__":
    main()
