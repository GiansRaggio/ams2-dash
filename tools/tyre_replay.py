#!/usr/bin/env python3
"""Replay de sesiones grabadas contra el TyreAnalyzer REAL (el del bridge).

Este es el guardian anti-mock: la v3 se valido contra escenarios sinteticos que
vivian en un regimen termico de box (bulk = carcasa - 9) y su senal principal salio
saturada el 100% del tiempo en pista de verdad. Este script recorre las trazas
.csv.gz de telemetry/ (14+ autos, 25+ pistas) alimentando frame a frame una
instancia real de TyreAnalyzer -- mismas EMAs, mismos umbrales, mismo payload --
y mide lo que el instrumento HABRIA dicho en cada sesion:

  - %hot / %cold por esquina (tdev): si un veredicto pasa del ~10% del tiempo en
    sesiones normales, esta saturado y NO informa. La v3 daba 100%.
  - deteccion del modelo de superficie muerto (surf_dead): debe encender en las
    sesiones con bulk pegado al ambiente y NUNCA en las vivas.
  - tiempo hasta warm y % del tiempo rodado en warm.
  - distribucion del trend (heat/stable/cool).
  - rango de rel (estructura) para confirmar que no satura la rampa +-15.

Uso:
    .venv\\Scripts\\python.exe tools\\tyre_replay.py                # todas las sesiones
    .venv\\Scripts\\python.exe tools\\tyre_replay.py --match Kansai # filtra por nombre
    .venv\\Scripts\\python.exe tools\\tyre_replay.py --events      # lista episodios tdev

Sale con codigo 1 si falla alguna de las verificaciones duras (abajo, VERIFICA).
Correr despues de CUALQUIER cambio de umbrales o de logica en ams2_tyres.py.
"""
import argparse
import csv
import gzip
import math
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import ams2_tyres

TDIR = os.path.join(HERE, "telemetry")
KELVIN = 273.15
C = ("FL", "FR", "RL", "RR")

# Sesiones con el modelo de superficie MUERTO, identificadas a mano en el barrido
# 2026-07-25 (bulk mediana <=40 C con carcasa >=75 C). Si el detector no las pilla,
# o marca muerta una sesion viva, el replay FALLA.
DEAD_SURF = {
    "Kansai__Audi_R8_LMS_GT4__race__20260712_214749",
    "Jerez__Audi_R8_LMS_GT4__practice__20260628_204629",
    "Jerez__Audi_R8_LMS_GT4__practice__20260705_202843",
    "Jerez__Audi_R8_LMS_GT4__practice__20260705_210152",
    "Jerez__Audi_R8_LMS_GT4__practice__20260705_211334",
    "Jerez__Audi_R8_LMS_GT4__qualify__20260705_214529",
}
# %max de tiempo rodado que un veredicto tdev puede estar encendido por esquina en
# una sesion sin que lo consideremos saturado. Un instrumento que opina siempre no
# informa (leccion v3); los episodios reales (trompo, rueda cocinada) son minutos.
MAX_FIRE_PCT = 12.0


class Shim:
    """Snapshot SHM de mentira alimentado desde una fila del CSV. Solo los campos
    que TyreAnalyzer.update() lee. Unidades: las MISMAS del SHM real (presion en
    bar*100, layer/carcass en Kelvin) para pasar por el mismo decode del bridge."""
    __slots__ = ("mCarName", "mTyreCompound", "mSpeed", "mAirPressure", "mTyreTemp",
                 "mTyreLayerTemp", "mTyreCarcassTemp", "mBrakeTempCelsius",
                 "mTyreTempLeft", "mTyreTempRight", "mLocalAcceleration",
                 "mTrackLocation")


def f(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return float("nan")


def build_shim(row, car, track=b"track"):
    d = Shim()
    d.mCarName = car
    d.mTrackLocation = track
    d.mTyreCompound = [b"Slick"]
    d.mSpeed = f(row, "speed_kmh") / 3.6
    d.mAirPressure = [f(row, f"tyre_press_{c}") for c in C]          # ya viene bar*100
    d.mTyreTemp = [f(row, f"tyre_temp_{c}") for c in C]              # C
    d.mTyreLayerTemp = [f(row, f"layer_t_{c}") + KELVIN for c in C]  # NaN si no hay col
    d.mTyreCarcassTemp = [f(row, f"carcass_t_{c}") + KELVIN for c in C]
    d.mBrakeTempCelsius = [f(row, f"brake_temp_{c}") for c in C]
    # el recorder guarda tyre_t_in = mTyreTempLeft y tyre_t_out = mTyreTempRight
    # CRUDOS (marco absoluto del auto); el analizador aplica INNER_IS_RIGHT el solo.
    d.mTyreTempLeft = [f(row, f"tyre_t_in_{c}") for c in C]
    d.mTyreTempRight = [f(row, f"tyre_t_out_{c}") for c in C]
    # G lateral: de aca sale que lado CARGA la pista, y de ahi que ruedas tienen derecho
    # a un veredicto de camber. SIN ESTE CAMPO el replay corre con dir="=" y sin tiempo
    # de curva acumulado, o sea NUNCA ejercita el camino nuevo y queda verde por vacio
    # -- el mismo modo de falla que dejo pasar la v3 (un harness que no reproduce la
    # realidad). Por eso VERIFICA de abajo exige que el camber haya opinado de verdad.
    d.mLocalAcceleration = [f(row, "accel_x"), 0.0, 0.0]
    return d


def replay(sess_dir, name, events_out=None):
    laps = sorted(x for x in os.listdir(sess_dir)
                  if x.startswith("L") and x.endswith(".csv.gz"))
    if not laps:
        return None
    # base_dir REAL y descartable (no _no_state, que no existe y hacia que
    # _save_targets fallara en silencio -> el camino de persistencia de la referencia
    # de camber tenia cobertura CERO mientras el replay se reportaba verde).
    state_dir = tempfile.mkdtemp(prefix="tyre_replay_")
    an = ams2_tyres.TyreAnalyzer(base_dir=state_dir)
    car = name.split("__")[1].encode() if "__" in name else b"car"
    track = name.split("__")[0].encode() if "__" in name else b"track"

    clock = 0.0            # reloj monotonico inyectado (sesion continua)
    live_s = 0.0
    warm_s = 0.0
    warm_at = None
    fire_s = [dict(hot=0.0, cold=0.0) for _ in range(4)]
    trend_s = {"heat": 0.0, "stable": 0.0, "cool": 0.0, None: 0.0}
    rel_min, rel_max = 0.0, 0.0
    dead_any = False
    dead_final = False
    ep = [None] * 4        # episodio tdev abierto por esquina
    cam_s = 0.0            # s con AL MENOS una rueda opinando de camber
    cam_idle = 0.0         # s con ruedas medidas pero ninguna con derecho a opinar
    cam_dir = "="          # ultimo lado cargado detectado

    for lp in laps:
        with gzip.open(os.path.join(sess_dir, lp), "rt", newline="") as fh:
            prev_t = None
            for row in csv.DictReader(fh):
                t = f(row, "t")
                dt = 0.02 if (prev_t is None or not math.isfinite(t)) \
                    else min(max(t - prev_t, 0.0), 0.5)
                prev_t = t
                clock += dt
                an.update(build_shim(row, car, track), now=clock)
                # evaluar el payload ~1 vez por segundo de sesion (como lo veria el dash)
                if int(clock) == int(clock - dt):
                    continue
                p = an.payload()
                if not p["live"]:
                    continue
                live_s += 1.0
                if p["warm"]:
                    warm_s += 1.0
                    if warm_at is None:
                        warm_at = clock
                trend_s[p["trend"]] = trend_s.get(p["trend"], 0.0) + 1.0
                if p["surf_dead"]:
                    dead_any = True
                dead_final = p["surf_dead"]
                # Cobertura del camino de camber. Sin esto el replay puede quedar verde
                # sin haber ejercitado NADA (paso una vez: el shim no traia
                # mLocalAcceleration, asi que no habia lado cargado y el veredicto se
                # apagaba en las 58 sesiones, en silencio).
                cam_dir = p.get("dir", "=")
                st = [c.get("cstat") for c in p["corners"]]
                if any(s in ("ok", "warn") for s in st):
                    cam_s += 1.0
                elif any(s == "idle" for s in st):
                    cam_idle += 1.0
                for i, cor in enumerate(p["corners"]):
                    if cor["rel"] is not None:
                        rel_min = min(rel_min, cor["rel"])
                        rel_max = max(rel_max, cor["rel"])
                    v = cor["tdev"]
                    if v:
                        fire_s[i][v] += 1.0
                        if ep[i] is None:
                            ep[i] = [C[i], v, clock, cor["carcass"]]
                    elif ep[i] is not None:
                        if events_out is not None:
                            e = ep[i]
                            events_out.append((name, e[0], e[1], e[2], clock - e[2], e[3]))
                        ep[i] = None
    # Cierre de tanda REAL (el mismo que corre el bridge al apagarse) y comprobacion de
    # que la referencia llego al disco. Esto es lo que antes no se ejercitaba nunca.
    an.close()
    guardado = os.path.exists(os.path.join(state_dir, "tyre_targets.json"))
    shutil.rmtree(state_dir, ignore_errors=True)
    return {
        "name": name, "live_s": live_s, "warm_s": warm_s, "warm_at": warm_at,
        "fire": fire_s, "trend": trend_s, "rel": (rel_min, rel_max),
        "dead_any": dead_any, "dead_final": dead_final,
        "cam_s": cam_s, "cam_idle": cam_idle, "cam_dir": cam_dir,
        "cam_saved": guardado,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="", help="substring del nombre de sesion")
    ap.add_argument("--events", action="store_true", help="listar episodios tdev")
    args = ap.parse_args()

    events = [] if args.events else None
    rows, fails = [], []
    for name in sorted(os.listdir(TDIR)):
        d = os.path.join(TDIR, name)
        if not os.path.isdir(d) or (args.match and args.match not in name):
            continue
        r = replay(d, name, events)
        if r is None or r["live_s"] < 120:
            continue
        rows.append(r)

    print(f"{'sesion':56s} {'rodado':>7s} {'warm%':>6s} {'hot%/cold% por esquina':>30s}"
          f" {'trend h/s/c%':>14s} {'rel rango':>10s} {'surf':>5s} {'camber':>11s}")
    for r in rows:
        ls = r["live_s"]
        fire = " ".join(f"{100*f_['hot']/ls:.0f}/{100*f_['cold']/ls:.0f}" for f_ in r["fire"])
        tr = "/".join(f"{100*r['trend'].get(k,0)/ls:.0f}" for k in ("heat", "stable", "cool"))
        rel = f"{r['rel'][0]:+.0f}..{r['rel'][1]:+.0f}"
        surf = "DEAD" if r["dead_final"] else ("flip" if r["dead_any"] else "ok")
        cam = f"{r['cam_dir']} {100*r['cam_s']/ls:3.0f}%"
        print(f"{r['name'][:56]:56s} {ls/60:6.1f}m {100*r['warm_s']/ls:5.0f}% {fire:>30s}"
              f" {tr:>14s} {rel:>10s} {surf:>5s} {cam:>11s}")

        # ---- VERIFICA (duras: cualquiera en falso = exit 1) ----
        expect_dead = r["name"] in DEAD_SURF
        if expect_dead and not r["dead_final"]:
            fails.append(f"{r['name']}: modelo de superficie muerto NO detectado")
        if not expect_dead and r["dead_any"]:
            fails.append(f"{r['name']}: sesion viva marcada surf_dead (falso positivo)")
        for i, f_ in enumerate(r["fire"]):
            for k in ("hot", "cold"):
                pct = 100 * f_[k] / ls
                if pct > MAX_FIRE_PCT:
                    fails.append(f"{r['name']}: tdev {k} saturado en {C[i]} ({pct:.0f}%)")

    if events is not None:
        print("\n--- episodios tdev (sesion, rueda, veredicto, t inicio, duracion s, carcasa) ---")
        for e in events:
            print(f"  {e[0][:48]:48s} {e[1]} {e[2]:4s} t={e[3]:7.1f}s dur={e[4]:5.1f}s carc={e[5]}")

    # ---- COBERTURA (no por sesion sino del conjunto) ----
    # El veredicto de camber se apaga a proposito en varios casos (superficie muerta,
    # rueda descargada, tanda corta). Lo que NO puede pasar es que se apague en TODAS:
    # eso significa que el camino esta roto o que el harness dejo de alimentarlo, y es
    # el modo de falla que ya dejo pasar un rediseno entero sin que nadie lo notara.
    vivas = [r for r in rows if not r["dead_final"]]
    con_cam = [r for r in vivas if r["cam_s"] > 0]
    if vivas and len(con_cam) < len(vivas) // 2:
        fails.append(f"camber apagado en {len(vivas) - len(con_cam)}/{len(vivas)} sesiones "
                     "vivas: el canal de G lateral no esta llegando o el gate esta roto")
    direcc = [r for r in vivas if r["cam_dir"] != "="]
    if vivas and not direcc:
        fails.append("ninguna pista salio direccional: el indice de carga no se acumula")
    # El cierre de tanda tiene que llegar al disco. Estuvo roto y en silencio: el
    # base_dir del replay apuntaba a un directorio inexistente y _save_targets se
    # tragaba el OSError, asi que la persistencia de la referencia nunca se probo.
    guardadas = [r for r in vivas if r["cam_saved"]]
    if vivas and len(guardadas) < len(vivas) // 2:
        fails.append(f"la referencia de camber solo se persistio en {len(guardadas)}/"
                     f"{len(vivas)} sesiones vivas: el cierre de tanda no llega al disco")

    print(f"\n{len(rows)} sesiones reproducidas · camber opinando en "
          f"{len(con_cam)}/{len(vivas)} vivas · {len(direcc)} pistas direccionales · "
          f"referencia persistida en {len(guardadas)}/{len(vivas)}.")
    if fails:
        print("FALLAS:")
        for x in fails:
            print("  - " + x)
        sys.exit(1)
    print("VERIFICA: OK (sin saturacion, surf_dead correcto en las 6 sesiones malas)")


if __name__ == "__main__":
    main()
