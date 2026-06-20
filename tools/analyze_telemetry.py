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
import shutil
import statistics as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEM = os.path.join(HERE, "telemetry")
REFDIR = os.path.join(HERE, "references")   # mejores vueltas guardadas por auto+pista (benchmark)
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


def _load_sectors(folder):
    """Registros de sectores por vuelta (sectors.jsonl): sectores correctos + validez por
    sector, incluso de vueltas invalidadas (para rescatar sectores limpios). [] si no hay."""
    out = []
    p = os.path.join(folder, "sectors.jsonl")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


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


def _lap_sectors(l):
    """Sectores [S1,S2,S3] de una vuelta, recuperando el S1 si el logger lo guardo roto.

    Bug del logger (pre-fix): al cruzar meta leia mCurrentSector1Time de la vuelta
    NUEVA (ya reseteado a ~0) en vez del de la completada, dejando S1~=0. Como S2/S3 y
    lap_time son correctos, se recupera S1 = lap_time - S2 - S3. None si no hay datos.
    """
    s = l.get("sectors")
    lt = l.get("lap_time")
    if not s or len(s) != 3 or lt is None:
        return None
    s1, s2, s3 = s
    if s1 < 1.0 and s2 > 0 and s3 > 0:        # S1 roto -> recuperar del total
        s1 = lt - s2 - s3
    if s1 <= 0 or s2 <= 0 or s3 <= 0:
        return None
    return [round(s1, 3), s2, s3]


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

    # --- sectores + vuelta ideal (rescata el mejor sector LIMPIO de cada vuelta) ---
    # Con sectors.jsonl (logger nuevo): sectores correctos + validez por sector, incluso de
    # vueltas invalidadas -> se rescata un sector bueno de una vuelta con error en otro sector.
    srecs = _load_sectors(folder)
    if srecs:
        sl = [{"lap": r["lap"], "s": r["sectors"], "t": r.get("lap_time"),
               "v": r.get("sec_valid", [True, True, True]), "inv": r.get("invalid", False)}
              for r in srecs if r.get("sectors") and len(r["sectors"]) == 3
              and all(x > 0 for x in r["sectors"])]
    else:                                  # sesiones viejas: del summary, con S1 recuperado
        sl = []
        for l in laps:
            s = _lap_sectors(l) if l.get("lap_time") else None
            if s:
                sl.append({"lap": l["lap"], "s": s, "t": l["lap_time"],
                           "v": [True, True, True], "inv": False})
    if sl:
        clean_t = [r["t"] for r in sl if r["t"] and not r["inv"]]
        best_lap_time = min(clean_t) if clean_t else min(r["t"] for r in sl if r["t"])
        best_i = []                        # vuelta dueña de cada sector (solo sectores LIMPIOS)
        for i in range(3):
            cands = [r for r in sl if r["v"][i]]
            best_i.append(min(cands, key=lambda r: r["s"][i]) if cands else None)
        print(f"\n  {'LAP':>3} {'S1':>10} {'S2':>10} {'S3':>10}")
        for r in sl:
            tags = []
            if r["t"] and r["t"] > best_lap_time * 1.03:
                tags.append("lenta/calentando")
            if r["inv"]:
                tags.append("invalidada")
            cells = []
            for i in range(3):
                own = best_i[i] is not None and best_i[i]["lap"] == r["lap"]
                mark = "*" if own else (" " if r["v"][i] else "x")   # x = sector sucio (no elegible)
                cells.append(f"{r['s'][i]:8.3f}{mark}")
            tail = ("  (" + ", ".join(tags) + ")") if tags else ""
            print(f"  {r['lap']:>3} " + " ".join(cells) + tail)
        if all(best_i):
            best_s = [best_i[i]["s"][i] for i in range(3)]
            ideal = sum(best_s)
            owners = " · ".join(
                f"S{i+1} {best_s[i]:.3f} (L{best_i[i]['lap']}{'^' if best_i[i]['inv'] else ''})"
                for i in range(3))
            print(f"  mejores sectores: {owners}   (* dueña · x sucio · ^ rescatado de inválida)")
            print(f"  vuelta ideal    : {_fmt_t(ideal).strip()}  ·  tu mejor "
                  f"{_fmt_t(best_lap_time).strip()}  ·  {ideal - best_lap_time:+.3f}s a ganar uniendo sectores")
        repr_secs = [r["s"] for r in sl
                     if r["t"] and not r["inv"] and r["t"] <= best_lap_time * 1.03]
        if len(repr_secs) >= 3:
            sig = [st.pstdev([s[i] for s in repr_secs]) for i in range(3)]
            worst = max(range(3), key=lambda i: sig[i])
            print(f"  consist. sector : S1 {sig[0]:.2f} · S2 {sig[1]:.2f} · S3 {sig[2]:.2f}s  "
                  f"(mas disperso S{worst+1}; sobre {len(repr_secs)} vueltas limpias repr.)")


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


# ====================== MOTOR DE INSIGHTS (CLI-first) ======================
# Convierte el analisis en 2-4 consejos accionables (veredicto+accion+magnitud, C1),
# priorizados por tiempo recuperable. Referencia = tu propia mejor vuelta limpia.
# Funciones *_struct devuelven datos (no print) -> inicio del refactor a estructuras.

def clean_laps(folder):
    """Vueltas limpias representativas (lap_time <= best*1.03; el summary ya excluye
    invalidadas y out-laps). Centraliza el filtro de los insights y sus guards."""
    _, laps = _load(folder)
    timed = [l for l in laps if l.get("lap_time")]
    if not timed:
        return {"best_lap_time": None, "n_clean": 0, "clean": [], "ref_lap": None}
    best = min(l["lap_time"] for l in timed)
    clean = sorted((l for l in timed if l["lap_time"] <= best * 1.03), key=lambda l: l["lap_time"])
    return {"best_lap_time": best, "n_clean": len(clean),
            "clean": [l["lap"] for l in clean], "ref_lap": clean[0]["lap"] if clean else None}


def sectors_struct(folder):
    """Mejores sectores limpios, vuelta ideal, gap por sector (mediana de vueltas limpias -
    mejor sector) y sigma. Fuente de R2 (peor sector). Usa sectors.jsonl o el summary."""
    srecs = _load_sectors(folder)
    _, laps = _load(folder)
    if srecs:
        sl = [{"lap": r["lap"], "s": r["sectors"], "t": r.get("lap_time"),
               "v": r.get("sec_valid", [True, True, True]), "inv": r.get("invalid", False)}
              for r in srecs if r.get("sectors") and len(r["sectors"]) == 3
              and all(x > 0 for x in r["sectors"])]
    else:
        sl = [{"lap": l["lap"], "s": _lap_sectors(l), "t": l["lap_time"],
               "v": [True, True, True], "inv": False}
              for l in laps if l.get("lap_time") and _lap_sectors(l)]
    if not sl:
        return None
    clean_t = [r["t"] for r in sl if r["t"] and not r["inv"]]
    best_lap_time = min(clean_t) if clean_t else min(r["t"] for r in sl if r["t"])
    best_i = []
    for i in range(3):
        cands = [r for r in sl if r["v"][i]]
        best_i.append(min(cands, key=lambda r: r["s"][i]) if cands else None)
    if not all(best_i):
        return None
    best_s = [best_i[i]["s"][i] for i in range(3)]
    reps = [r["s"] for r in sl if r["t"] and not r["inv"] and r["t"] <= best_lap_time * 1.03]
    gaps = [round(st.median([s[i] for s in reps]) - best_s[i], 3) if len(reps) >= 3 else None
            for i in range(3)]
    sig = [round(st.pstdev([s[i] for s in reps]), 3) if len(reps) >= 3 else None for i in range(3)]
    return {"best_lap_time": best_lap_time, "ideal": sum(best_s), "best_s": best_s,
            "owners": [best_i[i]["lap"] for i in range(3)], "gaps": gaps, "sigma": sig,
            "n_clean_sec": len(reps)}


def corners_vs_struct(folder, lap_a, ref_lap):
    """Deficit de vmin por curva de lap_a vs ref + tiempo perdido en el tramo del apex."""
    ta, tb = load_trace(folder, lap_a), load_trace(folder, ref_lap)
    if not ta or not tb:
        return []
    da, sa = _mono(ta["lap_dist"], ta["speed_kmh"])[:2]
    db, sb = _mono(tb["lap_dist"], tb["speed_kmh"])[:2]
    da2, t_a = _mono(ta["lap_dist"], ta["t"])
    db2, t_b = _mono(tb["lap_dist"], tb["t"])
    ca = _corners(da, sa)
    out = []
    for c in _corners(db, sb):
        ma = min((x for x in ca if abs(x["apex"] - c["apex"]) < 120),
                 key=lambda x: abs(x["apex"] - c["apex"]), default=None)
        if not ma:
            continue
        x0, x1 = c["apex"] - 50, c["apex"] + 150
        seg = ((_interp(da2, t_a, x1) - _interp(db2, t_b, x1)) -
               (_interp(da2, t_a, x0) - _interp(db2, t_b, x0)))
        out.append({"n": c["n"], "apex": c["apex"], "vmin_a": ma["vmin"], "vmin_ref": c["vmin"],
                    "deficit": round(c["vmin"] - ma["vmin"], 1), "t_perdido_s": round(seg, 3)})
    return out


def coasting_struct(folder, lap):
    """Tramos de coasting + si caen en la zona de entrada (justo antes de un apex)."""
    t = load_trace(folder, lap)
    if not t:
        return []
    d, s, th, br = _mono(t["lap_dist"], t["speed_kmh"], t["throttle"], t["brake"])
    cs = _corners(d, s)
    out = []
    for dist_ini, largo in _coasting(d, th, br, s):
        end = dist_ini + largo
        ap = min(cs, key=lambda c: abs(c["apex"] - end), default=None) if cs else None
        out.append({"dist_ini": dist_ini, "largo_m": largo,
                    "apex_n": ap["n"] if ap else None,
                    "en_zona": bool(ap and (ap["apex"] - 80) <= end <= ap["apex"])})
    return out


def load_reference(folder):
    """Referencia guardada (mejor vuelta) del auto+pista de esta sesion, o None."""
    meta, _ = _load(folder)
    car, track = meta.get("car"), meta.get("track")
    if not car or not track:
        return None
    p = os.path.join(REFDIR, f"{car}__{track}.json")
    return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None


def save_reference(folder, lap=None, force=False):
    """Guarda la mejor vuelta limpia (o LAP) como referencia del auto+pista. Solo sobreescribe
    si es mas rapida que la guardada (o force=True). Copia la traza para comparaciones futuras."""
    meta, laps = _load(folder)
    car, track = meta.get("car"), meta.get("track")
    if not car or not track:
        return "sin metadata de auto/pista en la sesion"
    lap = lap if lap is not None else clean_laps(folder)["ref_lap"]
    if lap is None:
        return "no hay vuelta limpia para guardar"
    m = [l for l in laps if l["lap"] == lap and l.get("lap_time")]
    if not m:
        return f"vuelta {lap} no encontrada o sin tiempo"
    lt = m[0]["lap_time"]
    existing = load_reference(folder)
    if existing and not force and existing.get("lap_time", 9e9) <= lt:
        return (f"la referencia actual ({existing['lap_time']:.3f}s) ya es mas rapida que la vuelta "
                f"{lap} ({lt:.3f}s); usa --save-ref con la vuelta y force si quieres sobreescribir")
    sec = next((r["sectors"] for r in _load_sectors(folder) if r["lap"] == lap), None) or _lap_sectors(m[0])
    os.makedirs(REFDIR, exist_ok=True)
    name = f"{car}__{track}"
    data = {"car": car, "track": track, "track_variation": meta.get("track_variation"),
            "lap": lap, "lap_time": round(lt, 3), "sectors": sec, "session": os.path.basename(folder)}
    src = os.path.join(folder, m[0].get("trace", "") or "")
    if m[0].get("trace") and os.path.exists(src):
        shutil.copy(src, os.path.join(REFDIR, name + ".csv.gz"))
        data["trace"] = name + ".csv.gz"
    with open(os.path.join(REFDIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return f"referencia guardada: {car} @ {track} = {lt:.3f}s (vuelta {lap})"


def reference_struct(folder):
    """Compara tus mejores sectores de la sesion vs tu referencia guardada (benchmark)."""
    ref = load_reference(folder)
    ss = sectors_struct(folder)
    if not ref or not ss:
        return None
    rsec = ref.get("sectors")
    if not rsec or len(rsec) != 3:
        return None
    your = ss["best_s"]
    return {"ref_lap_time": ref["lap_time"], "ref_sectors": rsec, "your_best_s": your,
            "your_lap": ss["best_lap_time"], "sector_deltas": [round(your[i] - rsec[i], 2) for i in range(3)]}


def build_insights(folder):
    """Motor v1: sector debil vs tu REFERENCIA guardada (R2-ref, sirve con >=1 vuelta) o vs tu
    ideal de sesion (R2, >=3 limpias); deficit de vmin (R1) y coasting (R3) intra-sesion (>=3).
    Guards: piso de ruido, repeticion en >=2 vueltas, procedencia, dedup. (header, insights, status)."""
    meta, _ = _load(folder)
    cl = clean_laps(folder)
    n = cl["n_clean"]
    rs = reference_struct(folder)
    header = {"car": meta.get("car", "?"), "track": meta.get("track", "?"), "n_clean": n,
              "best_lap_time": cl["best_lap_time"], "ref_lap_time": rs["ref_lap_time"] if rs else None}
    out = []

    # --- sector debil: vs tu REFERENCIA guardada (medido, sirve con pocas vueltas) o vs tu ideal ---
    if rs and n >= 1:
        d = rs["sector_deltas"]
        order = sorted(range(3), key=lambda i: d[i], reverse=True)
        w = order[0]
        if d[w] >= 0.30 and (d[w] - d[order[1]]) >= 0.15:
            out.append({"regla": "R2-ref", "t": d[w], "proc": "medido",
                        "msg": f"S{w + 1}: +{d[w]:.2f}s vs tu referencia ({rs['ref_sectors'][w]:.3f}s) — "
                               f"es tu mayor brecha al benchmark, foco ahi."})
    elif n >= 3:
        ss = sectors_struct(folder)
        if ss and ss["n_clean_sec"] >= 3 and all(g is not None for g in ss["gaps"]):
            order = sorted(range(3), key=lambda i: ss["gaps"][i], reverse=True)
            w = order[0]
            gap, gap2 = ss["gaps"][w], ss["gaps"][order[1]]
            if gap >= 0.30 and (gap - gap2) >= 0.15:
                out.append({"regla": "R2", "t": gap, "proc": "medido",
                            "msg": f"S{w + 1} es tu sector debil: pierdes {gap:.2f}s vs tu mejor S{w + 1} "
                                   f"({ss['best_s'][w]:.3f}s, L{ss['owners'][w]}) — ya lo hiciste mas rapido."})

    # --- curva/coasting: necesitan repeticion en >=2 vueltas limpias (>=3 limpias en total) ---
    if n >= 3:
        ref_lap = cl["ref_lap"]
        others = [lp for lp in cl["clean"] if lp != ref_lap]
        agg = {}
        for lp in others:
            for c in corners_vs_struct(folder, lp, ref_lap):
                if c["deficit"] >= 3.0:
                    a = agg.setdefault(c["n"], {"defs": [], "tp": [], "apex": c["apex"], "vref": c["vmin_ref"]})
                    a["defs"].append(c["deficit"])
                    a["tp"].append(max(0.0, c["t_perdido_s"]))
        r1_corners = set()
        for cn, a in agg.items():
            if len(a["defs"]) >= 2:                       # repeticion -> no es trafico/one-off
                md, tp = st.median(a["defs"]), st.median(a["tp"])
                if md >= 3.0 and tp >= 0.05:
                    r1_corners.add(cn)
                    out.append({"regla": "R1", "t": tp, "proc": "estimado",
                                "msg": f"T{cn} (apex {a['apex']}m): vmin {a['vref'] - md:.0f} km/h, {md:.0f} bajo "
                                       f"tu mejor ({a['vref']:.0f}) — ~{tp:.2f}s. Gira antes y carga mas velocidad "
                                       f"de paso (menos freno a la entrada)."})
        coast = {}
        for lp in cl["clean"]:
            for z in coasting_struct(folder, lp):
                if z["largo_m"] >= 25 and z["en_zona"] and z["apex_n"]:
                    coast.setdefault(z["apex_n"], []).append(z["largo_m"])
        for cn, largos in coast.items():
            if len(largos) >= 2 and cn not in r1_corners:
                largo = st.median(largos)
                acortar = min(largo - 10, 15)             # guardrail C3: nunca >15m de golpe
                if acortar >= 3:
                    out.append({"regla": "R3", "t": 0.0, "proc": "metros",
                                "msg": f"T{cn}: coasting {largo:.0f}m antes del apex (flotando, gas y freno sueltos). "
                                       f"Frena ~{acortar:.0f}m mas tarde y mantente en el freno hasta soltar el volante."})

    out.sort(key=lambda x: x["t"], reverse=True)
    status = "ok" if (out or n >= 3) else "insuficiente"
    return header, out[:4], status


def report_insights(folder):
    header, insights, status = build_insights(folder)
    print(f"\n=== Insights · {header['car']} @ {header['track']} · "
          f"{header['n_clean']} vueltas limpias ===")
    if header.get("ref_lap_time"):
        rs = reference_struct(folder)
        yb = _fmt_t(rs["your_lap"]).strip() if rs and rs.get("your_lap") else "—"
        line = f"  referencia guardada {_fmt_t(header['ref_lap_time']).strip()}  ·  tu mejor sesion {yb}"
        if rs:
            line += "  ·  delta sectores " + " / ".join(f"{x:+.2f}" for x in rs["sector_deltas"])
        print(line)
    if status == "insuficiente":
        print(f"  N insuficiente ({header['n_clean']} limpias) y sin referencia guardada: maneja >=3 "
              "vueltas limpias o guarda una referencia (--save-ref). Descriptivo:")
        report_session(folder)
        return
    if not insights:
        print("  Tanda pareja, sin deficit sobre el ruido — sube tu ritmo de referencia.")
        return
    for i, ins in enumerate(insights, 1):
        head = f"~{ins['t']:.2f}s" if ins["t"] > 0 else "magnitud"
        print(f"  [{i}] {head} · {ins['msg']} ({ins['proc']})")


def main():
    ap = argparse.ArgumentParser(description="Analisis de telemetria AMS2")
    ap.add_argument("folder", nargs="?", help="carpeta de sesion (default: la ultima)")
    ap.add_argument("--list", action="store_true", help="lista las sesiones")
    ap.add_argument("--lap", type=int, help="inspecciona la traza de esa vuelta")
    ap.add_argument("--vs", type=int, nargs="+", metavar="LAP",
                    help="compara vuelta A [B] (B por defecto: la mejor) — delta, vmin, coasting")
    ap.add_argument("--insights", action="store_true",
                    help="motor de insights: 2-4 consejos accionables priorizados (>=3 vueltas limpias)")
    ap.add_argument("--save-ref", nargs="?", type=int, const=-1, metavar="LAP", default=None,
                    help="guarda la mejor vuelta limpia (o LAP) como referencia del auto+pista")
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
    if a.save_ref is not None:
        print(save_reference(folder, None if a.save_ref == -1 else a.save_ref))
    elif a.vs:
        report_vs(folder, a.vs[0], a.vs[1] if len(a.vs) > 1 else None)
    elif a.lap is not None:
        report_lap(folder, a.lap)
    elif a.insights:
        report_insights(folder)
    else:
        report_session(folder)


if __name__ == "__main__":
    main()
