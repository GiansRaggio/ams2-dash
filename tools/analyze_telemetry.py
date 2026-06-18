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


def load_trace(folder, lap_no, laps=None):
    """Carga la traza de una vuelta como dict canal->lista de floats."""
    if laps is None:
        _, laps = _load(folder)
    m = [l for l in laps if l["lap"] == lap_no]
    if not m:
        return None
    path = os.path.join(folder, m[0]["trace"])
    if not os.path.exists(path):
        return None
    data = {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        rdr = csv.DictReader(f)
        for c in rdr.fieldnames:
            data[c] = []
        for row in rdr:
            for c in rdr.fieldnames:
                try:
                    data[c].append(float(row[c]))
                except (ValueError, TypeError):
                    data[c].append(0.0)
    return data


def _mono(dist, *cols):
    """Recorta a la porcion de distancia estrictamente creciente (evita el wrap de meta)."""
    out_d, out = [], [[] for _ in cols]
    last = -1e9
    for i, d in enumerate(dist):
        if d > last:
            out_d.append(d)
            for k, c in enumerate(cols):
                out[k].append(c[i])
            last = d
    return (out_d, *out)


def _interp(xs, ys, x):
    """Interpolacion lineal de ys(xs) en x (xs creciente)."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo]
    f = (x - xs[lo]) / span if span else 0
    return ys[lo] + f * (ys[hi] - ys[lo])


def _smooth(v, w=15):
    if len(v) < w or w < 2:
        return list(v)
    half = w // 2
    out = []
    for i in range(len(v)):
        a, b = max(0, i - half), min(len(v), i + half + 1)
        out.append(sum(v[a:b]) / (b - a))
    return out


def _corners(dist, spd, min_prom=15.0, min_gap_m=80.0):
    """Detecta apex de curvas como minimos locales prominentes de velocidad."""
    s = _smooth(spd, 21)
    n = len(s)
    cand = []
    for i in range(2, n - 2):
        if s[i] <= s[i - 1] and s[i] <= s[i + 1]:
            # prominencia: cuanto sube a ambos lados antes del proximo minimo
            lmax = s[i]
            j = i
            while j > 0 and s[j - 1] >= s[j]:
                lmax = max(lmax, s[j - 1]); j -= 1
            rmax = s[i]
            j = i
            while j < n - 1 and s[j + 1] >= s[j]:
                rmax = max(rmax, s[j + 1]); j += 1
            prom = min(lmax, rmax) - s[i]
            if prom >= min_prom:
                cand.append((dist[i], s[i], prom))
    # fusionar minimos muy cercanos (quedarse con el de menor velocidad)
    cand.sort()
    merged = []
    for d, v, p in cand:
        if merged and d - merged[-1][0] < min_gap_m:
            if v < merged[-1][1]:
                merged[-1] = (d, v, p)
        else:
            merged.append((d, v, p))
    return [{"n": i + 1, "apex": round(d), "vmin": round(v, 1)}
            for i, (d, v, p) in enumerate(merged)]


def _coasting(dist, thr, brk, spd, min_m=8.0):
    """Tramos de coasting (gas~0 y freno~0 en movimiento) -> lista de (dist_ini, largo_m)."""
    zones = []
    i, n = 0, len(dist)
    while i < n:
        if thr[i] < 0.05 and brk[i] < 0.05 and spd[i] > 30:
            j = i
            while j < n and thr[j] < 0.05 and brk[j] < 0.05 and spd[j] > 30:
                j += 1
            length = dist[j - 1] - dist[i]
            if length >= min_m:
                zones.append((dist[i], round(length)))
            i = j
        else:
            i += 1
    return zones


def report_vs(folder, lap_a, lap_b):
    _, laps = _load(folder)
    times = {l["lap"]: l.get("lap_time") for l in laps}
    if lap_b is None:                       # default: contra la mejor vuelta
        valid = [(l["lap"], l["lap_time"]) for l in laps if l.get("lap_time")]
        lap_b = min(valid, key=lambda x: x[1])[0] if valid else None
    ta, tb = load_trace(folder, lap_a, laps), load_trace(folder, lap_b, laps)
    if not ta or not tb:
        print(f"  faltan trazas (vueltas {[l['lap'] for l in laps]})")
        return
    da, sa, tha, bra = _mono(ta["lap_dist"], ta["speed_kmh"], ta["throttle"], ta["brake"])
    db, sb = _mono(tb["lap_dist"], tb["speed_kmh"])[:2]
    da2, t_a = _mono(ta["lap_dist"], ta["t"])
    db2, t_b = _mono(tb["lap_dist"], tb["t"])

    print(f"\n=== vuelta {lap_a} vs {lap_b} (referencia) · {os.path.basename(folder)} ===")
    print(f"  lap-time: {_fmt_t(times.get(lap_a)).strip()} vs {_fmt_t(times.get(lap_b)).strip()}"
          f"  (delta {(times.get(lap_a,0)-times.get(lap_b,0)):+.3f}s)")

    # delta acumulado por distancia + donde se pierde mas
    end = min(da2[-1], db2[-1])
    grid = list(range(0, int(end), 50))
    deltas = [_interp(da2, t_a, x) - _interp(db2, t_b, x) for x in grid]
    segs = [(grid[i], deltas[i] - deltas[i - 1]) for i in range(1, len(deltas))]
    worst = sorted(segs, key=lambda s: s[1], reverse=True)[:4]
    best = sorted(segs, key=lambda s: s[1])[:3]
    corners = _corners(db, sb)

    def near(d):
        c = min(corners, key=lambda c: abs(c["apex"] - d)) if corners else None
        return f"~T{c['n']}" if c and abs(c["apex"] - d) < 150 else f"{d}m"
    print("  donde PERDES tiempo:")
    for d, dv in worst:
        if dv > 0.02:
            print(f"   {near(d):>6} (dist {d}m): +{dv:.2f}s")
    print("  donde GANAS:")
    for d, dv in best:
        if dv < -0.02:
            print(f"   {near(d):>6} (dist {d}m): {dv:.2f}s")

    # vmin por curva: A vs B
    print("  velocidad de apex por curva (A vs ref):")
    ca = _corners(da, sa)
    for c in corners:
        ma = min((x for x in ca if abs(x["apex"] - c["apex"]) < 120),
                 key=lambda x: abs(x["apex"] - c["apex"]), default=None)
        if ma:
            dv = ma["vmin"] - c["vmin"]
            flag = "" if abs(dv) < 2 else ("  <= mas lento" if dv < 0 else "  (mas rapido)")
            print(f"   T{c['n']:<2} apex {c['apex']:>5}m: {ma['vmin']:>5.1f} vs {c['vmin']:>5.1f} km/h ({dv:+.1f}){flag}")


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
        if len(secs) >= 3:
            sig = [st.pstdev([s[i] for s in secs]) for i in range(3)]
            worst = max(range(3), key=lambda i: sig[i])
            print(f"  consist. sector : S1 {sig[0]:.2f} · S2 {sig[1]:.2f} · S3 {sig[2]:.2f}s  "
                  f"(mas disperso S{worst+1})")


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

    # curvas (apex vmin) + coasting (gas y freno sueltos)
    if data.get("lap_dist") and data.get("speed_kmh"):
        d, s, th, br = _mono(data["lap_dist"], data["speed_kmh"], data["throttle"], data["brake"])
        cs = _corners(d, s)
        if cs:
            print(f"\n  curvas detectadas: {len(cs)}")
            for c in cs:
                print(f"   T{c['n']:<2} apex {c['apex']:>5}m  vmin {c['vmin']:>5.1f} km/h")
        zs = _coasting(d, th, br, s)
        if zs:
            print(f"  coasting: {len(zs)} tramos · {sum(z[1] for z in zs)}m total · "
                  f"mayor {max(z[1] for z in zs)}m")
    print(f"\n  (cargar en pandas: import pandas as pd; pd.read_csv(r'{path}'))")


def main():
    ap = argparse.ArgumentParser(description="Analisis de telemetria AMS2")
    ap.add_argument("folder", nargs="?", help="carpeta de sesion (default: la ultima)")
    ap.add_argument("--list", action="store_true", help="lista las sesiones")
    ap.add_argument("--lap", type=int, help="inspecciona la traza de esa vuelta")
    ap.add_argument("--vs", type=int, nargs="+", metavar="LAP",
                    help="compara vuelta A [B] (B por defecto: la mejor) — delta, vmin, coasting")
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
    if a.vs:
        report_vs(folder, a.vs[0], a.vs[1] if len(a.vs) > 1 else None)
    elif a.lap is not None:
        report_lap(folder, a.lap)
    else:
        report_session(folder)


if __name__ == "__main__":
    main()
