#!/usr/bin/env python3
"""Analisis offline de la telemetria grabada por ams2_telemetry.py.

Lee una carpeta de sesion (telemetry/<...>/) con su summary.jsonl y, opcional,
las trazas L*.csv.gz. Reporta consistencia, consumo, degradacion, mejores
sectores y vuelta teorica; con --lap N inspecciona una traza canal por canal.

Solo stdlib (json, gzip, csv, statistics). Ejemplos:
    python tools/analyze_telemetry.py                 # ultima sesion
    python tools/analyze_telemetry.py --list          # lista sesiones
    python tools/analyze_telemetry.py <carpeta>       # sesion puntual
    python tools/analyze_telemetry.py --lap 7         # inspecciona la vuelta 7
"""
import argparse
import csv
import glob
import gzip
import json
import os
import statistics as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEM = os.path.join(HERE, "telemetry")
CORNERS = ("FL", "FR", "RL", "RR")


def _fmt_t(s):
    if s is None:
        return "  --:--.---"
    m, sec = divmod(s, 60)
    return f"{int(m):>3d}:{sec:06.3f}"


def _sessions():
    if not os.path.isdir(TELEM):
        return []
    subs = [d for d in glob.glob(os.path.join(TELEM, "*")) if os.path.isdir(d)]
    return sorted(subs, key=os.path.getmtime)


def _load(folder):
    sumf = os.path.join(folder, "summary.jsonl")
    laps = []
    if os.path.exists(sumf):
        for line in open(sumf, encoding="utf-8"):
            line = line.strip()
            if line:
                laps.append(json.loads(line))
    meta = {}
    mf = os.path.join(folder, "session.json")
    if os.path.exists(mf):
        meta = json.load(open(mf, encoding="utf-8"))
    return meta, laps


def _slope(ys):
    """Pendiente de regresion lineal simple (unidad por vuelta)."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx = (n - 1) / 2.0
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def report_session(folder):
    meta, laps = _load(folder)
    print(f"\n=== {os.path.basename(folder)} ===")
    if meta:
        print(f"  {meta.get('car','?')} @ {meta.get('track','?')} ({meta.get('track_variation','')}) "
              f"· {meta.get('session','?')} · {meta.get('rate_hz','?')}Hz")
    if not laps:
        print("  (sin vueltas validas registradas todavia)")
        return

    print(f"\n  {'LAP':>3} {'TIME':>11} {'FUEL':>6} {'DEG%max':>8} {'TYRE°avg':>9} {'COMP':>6}")
    for l in laps:
        deg = max(l["wear_delta"]) * 100 if l.get("wear_delta") else 0.0
        tavg = max(l["tyre_temp_avg"]) if l.get("tyre_temp_avg") else 0.0
        print(f"  {l['lap']:>3} {_fmt_t(l.get('lap_time'))} {l.get('fuel_used',0):>5.2f}L "
              f"{deg:>7.2f}% {tavg:>7.0f}° {l.get('compound','')[:6]:>6}")

    times = [l["lap_time"] for l in laps if l.get("lap_time")]
    fuels = [l["fuel_used"] for l in laps if l.get("fuel_used") is not None]
    print("\n  --- resumen ---")
    if times:
        best = min(times)
        print(f"  vueltas validas : {len(laps)}")
        print(f"  mejor / mediana : {_fmt_t(best).strip()} / {_fmt_t(st.median(times)).strip()}")
        if len(times) >= 2:
            sd = st.pstdev(times)
            tag = "excelente" if sd < 0.5 else "buena" if sd < 1.0 else "regular" if sd < 2.0 else "dispersa"
            print(f"  consistencia    : sigma {sd:.2f}s ({tag})  ·  tendencia {_slope(times):+.2f}s/vuelta")
    if fuels:
        print(f"  consumo medio   : {st.mean(fuels):.2f} L/vuelta  ·  tendencia {_slope(fuels):+.3f} L/vuelta")
    degs = [max(l["wear_delta"]) * 100 for l in laps if l.get("wear_delta")]
    if degs and max(degs) > 0:
        print(f"  desgaste medio  : {st.mean(degs):.2f}%/vuelta (peor rueda)  ·  "
              f"a 80% en ~{int(80/max(st.mean(degs),1e-6))} vueltas")
    elif degs:
        print("  desgaste        : plano (mTyreWear no avanza en este auto/sesion)")

    # mejores sectores + vuelta teorica
    secs = [l["sectors"] for l in laps if l.get("sectors") and len(l["sectors"]) == 3
            and all(s > 0 for s in l["sectors"])]
    if secs:
        best_s = [min(s[i] for s in secs) for i in range(3)]
        print(f"  mejores sectores: {best_s[0]:.3f} / {best_s[1]:.3f} / {best_s[2]:.3f}")
        print(f"  vuelta teorica  : {_fmt_t(sum(best_s)).strip()}  (suma de mejores sectores)")


def report_lap(folder, lap_no):
    meta, laps = _load(folder)
    match = [l for l in laps if l["lap"] == lap_no]
    if not match:
        print(f"  no hay vuelta {lap_no} en {os.path.basename(folder)}. Vueltas: "
              f"{[l['lap'] for l in laps]}")
        return
    tname = match[0]["trace"]
    path = os.path.join(folder, tname)
    if not os.path.exists(path):
        print(f"  falta la traza {tname}")
        return
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        cols = rdr.fieldnames
        data = {c: [] for c in cols}
        for row in rdr:
            for c in cols:
                try:
                    data[c].append(float(row[c]))
                except (ValueError, TypeError):
                    pass
    n = len(data.get("t", []))
    print(f"\n=== vuelta {lap_no} · {tname} · {n} muestras ===")
    key = ["speed_kmh", "rpm", "throttle", "brake", "steer", "accel_x", "accel_z",
           "fuel_l", "tyre_temp_FL", "tyre_temp_RR", "brake_temp_FL", "tyre_press_FL"]
    print(f"  {'canal':>14} {'min':>9} {'avg':>9} {'max':>9}")
    for c in key:
        v = data.get(c)
        if v:
            print(f"  {c:>14} {min(v):>9.2f} {sum(v)/len(v):>9.2f} {max(v):>9.2f}")
    if data.get("speed_kmh"):
        print(f"\n  vel. maxima: {max(data['speed_kmh']):.1f} km/h  ·  "
              f"% a fondo: {100*sum(1 for x in data['throttle'] if x>0.98)/max(1,len(data['throttle'])):.0f}%  ·  "
              f"% frenando: {100*sum(1 for x in data['brake'] if x>0.05)/max(1,len(data['brake'])):.0f}%")
    print(f"\n  (cargar en pandas: import pandas as pd; pd.read_csv(r'{path}'))")


def main():
    ap = argparse.ArgumentParser(description="Analisis de telemetria AMS2")
    ap.add_argument("folder", nargs="?", help="carpeta de sesion (default: la ultima)")
    ap.add_argument("--list", action="store_true", help="lista las sesiones")
    ap.add_argument("--lap", type=int, help="inspecciona la traza de esa vuelta")
    a = ap.parse_args()

    if a.list:
        s = _sessions()
        print(f"sesiones en {TELEM}:" if s else f"sin sesiones en {TELEM}")
        for d in s:
            _, laps = _load(d)
            print(f"  {os.path.basename(d)}  ({len(laps)} vueltas)")
        return

    folder = a.folder or (_sessions()[-1] if _sessions() else None)
    if not folder or not os.path.isdir(folder):
        print(f"No hay sesiones en {TELEM}. Maneja con el dash grabando y volve.")
        return
    if a.lap is not None:
        report_lap(folder, a.lap)
    else:
        report_session(folder)


if __name__ == "__main__":
    main()
