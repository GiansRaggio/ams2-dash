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
import math
import os
import shutil
import statistics as st

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEM = os.path.join(HERE, "telemetry")
REFDIR = os.path.join(HERE, "references")   # mejores vueltas guardadas por auto+pista (benchmark)
_LAST = None                                # --last N: analizar solo las N vueltas validas mas recientes
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
    if _LAST:
        laps = laps[-_LAST:]               # las N validas mas recientes (el archivo esta en orden de uid)
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
    if _LAST and out:                      # alinear con las vueltas recientes del summary (por uid)
        if out[0].get("uid") is not None:
            _, laps = _load(folder)
            uids = [l.get("uid") for l in laps if l.get("uid") is not None]
            if uids:
                lo = min(uids)
                out = [r for r in out if (r.get("uid") or 0) >= lo]
        else:
            out = out[-_LAST:]
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


def _read_trace(path):
    """Lee una traza .csv.gz como dict canal->lista de floats. None si no existe."""
    if not path or not os.path.exists(path):
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


def load_trace(folder, lap_no, laps=None):
    """Carga la traza de una vuelta por NUMERO. Si varias comparten numero (el contador se
    resetea al volver al garage), usa la mas rapida (la representativa)."""
    if laps is None:
        _, laps = _load(folder)
    m = [l for l in laps if l["lap"] == lap_no and l.get("trace")]
    if not m:
        return None
    m.sort(key=lambda l: l.get("lap_time") or 9e9)
    return _read_trace(os.path.join(folder, m[0]["trace"]))


def _lap_trace(folder, rec):
    """Carga la traza de un REGISTRO de vuelta por su nombre de archivo (identidad unica,
    inmune a numeros de vuelta repetidos por reset del garage)."""
    return _read_trace(os.path.join(folder, rec["trace"])) if rec and rec.get("trace") else None


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
    """Vueltas limpias representativas (lap_time <= best*1.03). Devuelve los REGISTROS (no
    solo numeros) para identificar cada vuelta por su traza unica -> no confunde vueltas que
    comparten numero cuando el contador se resetea (visita al garage)."""
    _, laps = _load(folder)
    timed = [l for l in laps if l.get("lap_time")]
    if not timed:
        return {"best_lap_time": None, "n_clean": 0, "clean": [], "ref": None}
    best = min(l["lap_time"] for l in timed)
    clean = sorted((l for l in timed if l["lap_time"] <= best * 1.03), key=lambda l: l["lap_time"])
    return {"best_lap_time": best, "n_clean": len(clean), "clean": clean, "ref": clean[0] if clean else None}


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


def _corners_vs(ta, tb):
    """Deficit de vmin por curva de la traza A vs la traza B (referencia) + tiempo perdido
    en el tramo del apex. Sirve intra-sesion o contra la traza de tu referencia guardada."""
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


def corners_vs_struct(folder, lap_a, ref_lap):
    """Deficit de vmin por curva de lap_a vs ref_lap (ambas en la sesion)."""
    return _corners_vs(load_trace(folder, lap_a), load_trace(folder, ref_lap))


def coasting_struct(folder, rec):
    """Tramos de coasting + si caen en la zona de entrada, para un REGISTRO de vuelta."""
    t = _lap_trace(folder, rec)
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


def load_reference_trace(folder):
    """Traza completa de la referencia guardada del auto+pista, o None."""
    ref = load_reference(folder)
    if not ref or not ref.get("trace"):
        return None
    return _read_trace(os.path.join(REFDIR, ref["trace"]))


def save_reference(folder, lap=None, force=False):
    """Guarda la mejor vuelta limpia (o LAP) como referencia del auto+pista. Solo sobreescribe
    si es mas rapida que la guardada (o force=True). Copia la traza para comparaciones futuras."""
    meta, laps = _load(folder)
    car, track = meta.get("car"), meta.get("track")
    if not car or not track:
        return "sin metadata de auto/pista en la sesion"
    if lap is None:
        rec = clean_laps(folder)["ref"]
    else:
        cands = sorted((l for l in laps if l["lap"] == lap and l.get("lap_time")),
                       key=lambda l: l["lap_time"])
        rec = cands[0] if cands else None
    if not rec:
        return "no hay vuelta limpia para guardar"
    lt = rec["lap_time"]
    existing = load_reference(folder)
    if existing and not force and existing.get("lap_time", 9e9) <= lt:
        return (f"la referencia actual ({existing['lap_time']:.3f}s) ya es mas rapida que la vuelta "
                f"{rec['lap']} ({lt:.3f}s); usa --save-ref con la vuelta y force si quieres sobreescribir")
    sec = next((r["sectors"] for r in _load_sectors(folder)
                if r["lap"] == rec["lap"] and abs((r.get("lap_time") or 0) - lt) < 0.01), None) or _lap_sectors(rec)
    os.makedirs(REFDIR, exist_ok=True)
    name = f"{car}__{track}"
    data = {"car": car, "track": track, "track_variation": meta.get("track_variation"),
            "lap": rec["lap"], "lap_time": round(lt, 3), "sectors": sec, "session": os.path.basename(folder)}
    src = os.path.join(folder, rec.get("trace", "") or "")
    if rec.get("trace") and os.path.exists(src):
        shutil.copy(src, os.path.join(REFDIR, name + ".csv.gz"))
        data["trace"] = name + ".csv.gz"
    with open(os.path.join(REFDIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return f"referencia guardada: {car} @ {track} = {lt:.3f}s (vuelta {rec['lap']})"


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


def _sector_bounds(folder):
    """Distancia donde terminan S1 y S2, desde la vuelta de referencia (sus sector times)."""
    rec = clean_laps(folder)["ref"]
    if not rec or not rec.get("sectors"):
        return None
    t = _lap_trace(folder, rec)
    if not t:
        return None
    s = rec["sectors"]

    def dist_at(target):
        for i, x in enumerate(t["t"]):
            if x >= target:
                return t["lap_dist"][i]
        return t["lap_dist"][-1]

    return [dist_at(s[0]), dist_at(s[0] + s[1])]


def balance_struct(folder):
    """Balance del auto por curva (slip TRASERO vs delantero -> sobre/subviraje) + 'momento' de
    inestabilidad (pico de slip trasero en algunas vueltas, con ubicacion/sector). Sobre las vueltas
    limpias. None si <3 limpias o faltan los canales de slip (sesiones viejas no afectadas: slip existe)."""
    cl = clean_laps(folder)
    if cl["n_clean"] < 3:
        return None
    traces = [(rec, _lap_trace(folder, rec)) for rec in cl["clean"]]
    traces = [(rec, t) for rec, t in traces if t and "tyre_slip_RL" in t and t.get("lap_dist")]
    if len(traces) < 3:
        return None
    rt = traces[0][1]
    d, s = _mono(rt["lap_dist"], rt["speed_kmh"])[:2]
    corners = []
    for c in _corners(d, s):
        fr, re = [], []
        for _, t in traces:
            idx = [i for i, x in enumerate(t["lap_dist"]) if c["apex"] - 60 <= x <= c["apex"] + 60]
            if idx:
                fr.append(st.median([(t["tyre_slip_FL"][i] + t["tyre_slip_FR"][i]) / 2 for i in idx]))
                re.append(st.median([(t["tyre_slip_RL"][i] + t["tyre_slip_RR"][i]) / 2 for i in idx]))
        if len(fr) < 2:
            continue
        f, r = st.median(fr), st.median(re)
        ratio = round(r / f, 2) if f > 0.1 else 1.0
        corners.append({"n": c["n"], "apex": c["apex"], "vmin": round(c["vmin"]),
                        "front": round(f, 1), "rear": round(r, 1), "ratio": ratio,
                        "bal": "sobreviraje" if ratio >= 1.25 else "subviraje" if ratio <= 0.8 else "neutro"})
    # momento de inestabilidad POR SECTOR: el sector con mayor spike de slip trasero entre vueltas
    # (la peor vuelta muy por encima de la mediana = el trasero se suelta de forma inconsistente ahi).
    bounds = _sector_bounds(folder)
    moment = None
    if bounds:
        best = 0.0
        for si, (lo, hi) in enumerate([(0, bounds[0]), (bounds[0], bounds[1]), (bounds[1], 9e9)]):
            peaks = []
            for _, t in traces:
                vals = [(t["tyre_slip_RL"][i] + t["tyre_slip_RR"][i]) / 2
                        for i in range(len(t["lap_dist"])) if lo <= t["lap_dist"][i] < hi]
                if vals:
                    peaks.append(max(vals))
            if len(peaks) < 3:
                continue
            med = sorted(peaks)[len(peaks) // 2]
            worst = max(peaks)
            if med > 0.1 and worst >= med * 1.5 and (worst - med) > best:
                best = worst - med
                moment = {"sector": f"S{si + 1}", "peak": round(worst, 1), "median": round(med, 1),
                          "spike_pct": int((worst / med - 1) * 100)}
    return {"corners": corners, "moment": moment}


def build_insights(folder):
    """Motor v1: sector debil vs tu REFERENCIA (R2-ref) o vs tu ideal (R2); sector INCONSISTENTE
    (R-consist); deficit de vmin (R1/R1-ref) y coasting (R3). Identifica vueltas por su traza unica
    (no por numero, que se repite tras el garage). Guards: piso de ruido, repeticion, procedencia."""
    meta, _ = _load(folder)
    cl = clean_laps(folder)
    n = cl["n_clean"]
    rs = reference_struct(folder)
    ref_trace = load_reference_trace(folder) if rs else None
    ss = sectors_struct(folder)
    header = {"car": meta.get("car", "?"), "track": meta.get("track", "?"), "n_clean": n,
              "best_lap_time": cl["best_lap_time"], "ref_lap_time": rs["ref_lap_time"] if rs else None}
    out = []

    # --- sector debil: vs tu REFERENCIA guardada (medido, >=1 vuelta) o vs tu ideal de sesion ---
    if rs and n >= 1:
        d = rs["sector_deltas"]
        order = sorted(range(3), key=lambda i: d[i], reverse=True)
        w = order[0]
        if d[w] >= 0.30 and (d[w] - d[order[1]]) >= 0.15:
            out.append({"regla": "R2-ref", "loc": f"S{w + 1}", "t": d[w], "proc": "medido",
                        "msg": f"S{w + 1}: +{d[w]:.2f}s vs tu referencia ({rs['ref_sectors'][w]:.3f}s) — "
                               f"es tu mayor brecha al benchmark, foco ahi."})
    elif ss and ss["n_clean_sec"] >= 3 and all(g is not None for g in ss["gaps"]):
        order = sorted(range(3), key=lambda i: ss["gaps"][i], reverse=True)
        w = order[0]
        gap, gap2 = ss["gaps"][w], ss["gaps"][order[1]]
        if gap >= 0.30 and (gap - gap2) >= 0.15:
            out.append({"regla": "R2", "loc": f"S{w + 1}", "t": gap, "proc": "medido",
                        "msg": f"S{w + 1} es tu sector debil: pierdes {gap:.2f}s vs tu mejor S{w + 1} "
                               f"({ss['best_s'][w]:.3f}s, L{ss['owners'][w]}) — ya lo hiciste mas rapido."})

    # --- sector INCONSISTENTE: el de mayor dispersion vuelta a vuelta (repetibilidad) ---
    if ss and ss["sigma"] and all(s is not None for s in ss["sigma"]):
        order = sorted(range(3), key=lambda i: ss["sigma"][i], reverse=True)
        w = order[0]
        sig, sig2 = ss["sigma"][w], ss["sigma"][order[1]]
        if sig >= 0.30 and (sig - sig2) >= 0.15:
            out.append({"regla": "R-consist", "loc": f"S{w + 1}", "t": round(sig, 2), "proc": "medido",
                        "msg": f"S{w + 1} es tu sector INCONSISTENTE: varia {sig:.2f}s vuelta a vuelta "
                               f"(los otros mas parejos) — trabaja la repetibilidad: mismo punto de frenada y linea."})

    # --- deficit de vmin por curva: vs tu REFERENCIA (>=2 vueltas) o vs tu mejor de sesion (>=3) ---
    # Con referencia guardada baja el minimo a 2 vueltas (cada una vs el benchmark, repeticion >=2);
    # sin referencia usa tu mejor vuelta de la tanda y necesita 3 (best + 2 que repitan el deficit).
    use_ref = ref_trace is not None and n >= 2
    if use_ref:
        comps = [_corners_vs(_lap_trace(folder, rec), ref_trace) for rec in cl["clean"]]
        r1_label, r1_vs = "R1-ref", "tu referencia"
    elif n >= 3:
        ref_rec = cl["ref"]
        comps = [_corners_vs(_lap_trace(folder, rec), _lap_trace(folder, ref_rec))
                 for rec in cl["clean"] if rec is not ref_rec]
        r1_label, r1_vs = "R1", "tu mejor"
    else:
        comps, r1_label = [], None
    agg = {}
    for comp in comps:
        for c in comp:
            if c["deficit"] >= 3.0:
                a = agg.setdefault(c["n"], {"defs": [], "tp": [], "apex": c["apex"], "vref": c["vmin_ref"]})
                a["defs"].append(c["deficit"])
                a["tp"].append(max(0.0, c["t_perdido_s"]))
    r1_corners = set()
    if r1_label:
        for cn, a in agg.items():
            if len(a["defs"]) >= 2:                       # repeticion -> no es trafico/one-off
                md, tp = st.median(a["defs"]), st.median(a["tp"])
                if md >= 3.0 and tp >= 0.05:
                    r1_corners.add(cn)
                    out.append({"regla": r1_label, "loc": f"T{cn}", "t": tp, "proc": "estimado",
                                "msg": f"T{cn} (apex {a['apex']}m): vmin {a['vref'] - md:.0f} km/h, {md:.0f} bajo "
                                       f"{r1_vs} ({a['vref']:.0f}) — ~{tp:.2f}s. Gira antes y carga mas velocidad de paso."})

    # --- coasting en la entrada: intra-sesion, repeticion en >=2 vueltas (>=3 limpias) ---
    if n >= 3:
        coast = {}
        for rec in cl["clean"]:
            for z in coasting_struct(folder, rec):
                if z["largo_m"] >= 25 and z["en_zona"] and z["apex_n"]:
                    coast.setdefault(z["apex_n"], []).append(z["largo_m"])
        for cn, largos in coast.items():
            if len(largos) >= 2 and cn not in r1_corners:
                largo = st.median(largos)
                acortar = min(largo - 10, 15)             # guardrail C3: nunca >15m de golpe
                if acortar >= 3:
                    out.append({"regla": "R3", "loc": f"T{cn}", "t": 0.0, "proc": "metros",
                                "msg": f"T{cn}: coasting {largo:.0f}m antes del apex (flotando, gas y freno sueltos). "
                                       f"Frena ~{acortar:.0f}m mas tarde y mantente en el freno hasta soltar el volante."})

    # --- R6: balance (sobre/subviraje) por curva + momento de inestabilidad trasera (consistencia) ---
    bal = balance_struct(folder)
    if bal:
        m = bal["moment"]
        if m:
            out.append({"regla": "R6", "t": 0.0, "proc": "medido",
                        "msg": f"{m['sector']}: el tren TRASERO se suelta en tus vueltas malas (pico de slip "
                               f"{m['peak']} vs {m['median']} normal, +{m['spike_pct']}%) — sobreviraje a velocidad: "
                               f"te cuesta consistencia. Estabiliza atras (ARB tras. mas blanda / diff coast / mas "
                               f"ala) o suaviza la entrada."})
        else:
            cor = [c for c in bal["corners"] if c["bal"] != "neutro"]
            w = max(cor, key=lambda c: abs(c["ratio"] - 1.0)) if cor else None
            if w and abs(w["ratio"] - 1.0) >= 0.3:
                tip = ("estabiliza atras (ARB tras. mas blanda / diff coast / mas ala)" if w["bal"] == "sobreviraje"
                       else "ayuda la rotacion (ARB del. mas blanda / mas camber del. / menos ala)")
                out.append({"regla": "R6", "t": 0.0, "proc": "medido",
                            "msg": f"T{w['n']} (apex {w['apex']}m): {w['bal']} marcado (slip R/F {w['ratio']}) — {tip}."})

    out.sort(key=lambda x: x["t"], reverse=True)
    status = "ok" if (out or n >= 3 or rs) else "insuficiente"
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


# ====================== GOMAS (BETA): presion/camber por rueda ======================
# Ventanas de referencia GT (AMS2 no publica un valor canonico -> beta, afinar por sensacion).
TEMP_COLD_T, TEMP_OPT_LO, TEMP_OPT_HI, TEMP_HOT_T = 70, 80, 90, 100   # C
PSI_WIN_LO, PSI_WIN_HI = 26.0, 27.5                                   # psi en caliente (ref)
DCENTER_OK = 5.0                                                      # |centro - bordes| <= 5C = presion termica OK


def tyres_struct(folder):
    """Gomas por rueda sobre las vueltas limpias: temps L/C/R, presion en caliente, dCenter
    (centro vs bordes -> presion TERMICA, inmune al bug Bar*100) y dEdge (camber, beta)."""
    clean = clean_laps(folder)["clean"]
    if not clean:
        return None
    CORN = ("FL", "FR", "RL", "RR")
    acc = {c: {"in": [], "mid": [], "out": [], "bulk": [], "press": []} for c in CORN}
    for rec in clean:
        d = _lap_trace(folder, rec)
        if not d:
            continue
        for c in CORN:
            for k, ch in (("in", f"tyre_t_in_{c}"), ("mid", f"tyre_t_mid_{c}"),
                          ("out", f"tyre_t_out_{c}"), ("bulk", f"tyre_temp_{c}"),
                          ("press", f"tyre_press_{c}")):
                acc[c][k] += d.get(ch, [])
    wheels = {}
    for c in CORN:
        a = acc[c]
        if not a["mid"]:
            continue
        i, mi, o = (sum(a[k]) / len(a[k]) for k in ("in", "mid", "out"))
        b = sum(a["bulk"]) / len(a["bulk"])
        pr = st.median(a["press"]) if a["press"] else 0.0
        psi = pr / 100 * 14.5038 if 100 < pr < 400 else (pr if 15 < pr < 40 else None)
        wheels[c] = {"in": round(i, 1), "mid": round(mi, 1), "out": round(o, 1), "bulk": round(b, 1),
                     "dCenter": round(mi - (i + o) / 2, 1), "dEdge": round(i - o, 1),
                     "psi": round(psi, 1) if psi else None}
    return {"wheels": wheels, "n_clean": len(clean)} if wheels else None


def _press_verdict(w):
    """Veredicto de presion por rueda: termico (centro vs bordes, robusto) + ventana (ref)."""
    dC, psi = w["dCenter"], w["psi"]
    if dC > DCENTER_OK:
        therm = f"centro caliente ({dC:+.1f}C): posible SOBREpresion, baja en frio"
    elif dC < -DCENTER_OK:
        therm = f"bordes calientes ({dC:+.1f}C): posible SUBpresion, sube en frio"
    else:
        therm = f"centro/bordes OK ({dC:+.1f}C)"
    if psi is None:
        win = "psi no fiable"
    elif psi > PSI_WIN_HI:
        win = f"{psi:.1f} psi (sobre ~{PSI_WIN_LO:.0f}-{PSI_WIN_HI:.0f}, baja un toque en frio)"
    elif psi < PSI_WIN_LO:
        win = f"{psi:.1f} psi (bajo ~{PSI_WIN_LO:.0f}-{PSI_WIN_HI:.0f}, sube un toque en frio)"
    else:
        win = f"{psi:.1f} psi (en ventana)"
    return therm, win


def report_tyres(folder):
    meta, _ = _load(folder)
    ts = tyres_struct(folder)
    print(f"\n=== Gomas (BETA) · {meta.get('car', '?')} @ {meta.get('track', '?')} ===")
    if not ts:
        print("  sin vueltas limpias con traza todavia.")
        return
    print("  live-beta: temps sin calibrar en caliente (SimHub #632: el SIGNO del spread de bordes es")
    print("  'probable', no medido). La presion se juzga TERMICA (centro vs bordes), que es robusta.")
    print(f"  ventana ref: 80-90C optima · {PSI_WIN_LO:.0f}-{PSI_WIN_HI:.0f} psi caliente (afinar por sensacion)")
    print(f"\n  {'rueda':5} {'in/mid/out':>13} {'bulk':>6} {'estado':>9}  presion")
    for c, w in ts["wheels"].items():
        b = w["bulk"]
        state = ("FRIA" if b < TEMP_COLD_T else "caliente" if b > TEMP_HOT_T
                 else "optima" if TEMP_OPT_LO <= b <= TEMP_OPT_HI else "ok")
        therm, win = _press_verdict(w)
        lcr = f"{w['in']:.0f}/{w['mid']:.0f}/{w['out']:.0f}"
        print(f"  {c:5} {lcr:>13} {b:6.1f} {state:>9}  {therm}; {win}")
    print("\n  camber (BETA, magnitud del spread de bordes; signo 'probable'):")
    for ax, l, r in (("delantero", "FL", "FR"), ("trasero", "RL", "RR")):
        if l in ts["wheels"] and r in ts["wheels"]:
            print(f"   {ax}: {l} dEdge {ts['wheels'][l]['dEdge']:+.1f}C · {r} dEdge {ts['wheels'][r]['dEdge']:+.1f}C "
                  "(|spread| alto = un borde trabaja mas)")
    ws = ts["wheels"]
    if all(k in ws for k in ("FL", "FR", "RL", "RR")):
        left = (ws["FL"]["bulk"] + ws["RL"]["bulk"]) / 2
        right = (ws["FR"]["bulk"] + ws["RR"]["bulk"]) / 2
        front = (ws["FL"]["bulk"] + ws["FR"]["bulk"]) / 2
        rear = (ws["RL"]["bulk"] + ws["RR"]["bulk"]) / 2
        print(f"\n  asimetria: izq {left:.0f}C vs der {right:.0f}C ({left - right:+.0f}) · "
              f"del {front:.0f}C vs tras {rear:.0f}C ({front - rear:+.0f})")
        if abs(left - right) >= 5:
            side, turns = ("izquierdo", "derecha") if left > right else ("derecho", "izquierda")
            print(f"   lado {side} mas caliente -> pista dominada por curvas a {turns} (carga ese lado): "
                  "es de la pista, no se corrige con presion; balancea cada goma a su ventana.")


def report_balance(folder):
    meta, _ = _load(folder)
    bal = balance_struct(folder)
    print(f"\n=== Balance sobre/subviraje · {meta.get('car', '?')} @ {meta.get('track', '?')} ===")
    if not bal:
        print("  faltan >=3 vueltas limpias con canal de slip por rueda.")
        return
    print("  slip por rueda (trasero vs delantero) en el apex; R/F >1.25 = sobreviraje, <0.8 = subviraje.")
    print(f"\n  {'curva':6} {'apex':>6} {'vmin':>5} {'slipF':>6} {'slipR':>6} {'R/F':>5}  balance")
    for c in bal["corners"]:
        print(f"  T{c['n']:<5} {c['apex']:>6} {c['vmin']:>5} {c['front']:>6.1f} {c['rear']:>6.1f} "
              f"{c['ratio']:>5.2f}  {c['bal']}")
    m = bal["moment"]
    if m:
        print(f"\n  MOMENTO de inestabilidad: {m['sector']} — pico de slip trasero {m['peak']} vs {m['median']} "
              f"normal (+{m['spike_pct']}%): el trasero se suelta de forma inconsistente ahi.")


# ==================== modo COMBO: agrega todas las sesiones de un auto+pista ====================
# Margenes de combustible para --race-fuel (espejo de la politica de ams2_strategy; se duplican
# para que este analizador siga siendo SOLO stdlib, sin importar el motor en vivo).
_SAFETY_LAPS_TIMED = 1.5
_SAFETY_LAPS_LAPS = 1.0
_MARGIN_PCT = 0.02


def _session_type(folder):
    """Tipo de sesion (practice/qualify/race) desde el nombre Pista__Auto__tipo__fecha."""
    parts = os.path.basename(folder).split("__")
    return parts[2] if len(parts) >= 3 else "?"


def _combo_of(folder):
    """(car, track) de una sesion, o None si falta/placeholder la metadata."""
    meta, _ = _load(folder)
    car, track = meta.get("car"), meta.get("track")
    if not car or not track or car == "x" or track == "x":   # "x" = placeholder de sesiones malas
        return None
    return (car, track)


def _group_combos():
    """Agrupa las sesiones por (car, track). dict[(car,track)] = [folders] (cronologico)."""
    groups = {}
    for d in _sessions():                       # _sessions ya viene ordenado por mtime
        k = _combo_of(d)
        if k:
            groups.setdefault(k, []).append(d)
    return groups


def _resolve_combo(filtro):
    """(car, track, [folders], [otros_combos]) para el filtro. filtro vacio/None -> combo de la
    ultima sesion. Si matchea varios combos usa el mas reciente y devuelve los otros. None si nada."""
    groups = _group_combos()
    if not groups:
        return None
    if not filtro:
        k = _combo_of(_sessions()[-1])
        return (k[0], k[1], groups[k], []) if k in groups else None
    f = filtro.lower()
    matched = {k: v for k, v in groups.items()
               if f in (k[0] or "").lower() or f in (k[1] or "").lower()}
    if not matched:
        return None
    best = max(matched, key=lambda k: os.path.getmtime(matched[k][-1]))
    return (best[0], best[1], matched[best], [k for k in matched if k != best])


def combo_struct(car, track, folders):
    """Agrega todas las sesiones de un auto+pista: mejor vuelta, tendencia, insights RECURRENTES
    (en >=2 sesiones) y consumo/ritmo. El consumo/ritmo PREFIERE practica/quali (la carrera es
    atipica: mojado, trafico, ahorro de combustible); cae a todo si no hay practicas."""
    sessions, rec = [], {}                      # rec: (regla_base, loc) -> {n, msg, t}
    best_overall = None
    for d in folders:
        cl = clean_laps(d)
        b = cl["best_lap_time"]
        if b and (best_overall is None or b < best_overall):
            best_overall = b
        _, laps = _load(d)
        s_fuels = [l["fuel_used"] for l in laps
                   if l.get("fuel_used") is not None and 0.3 < l["fuel_used"] < 15]   # cordura L/vuelta
        try:
            _, ins = build_insights(d)
        except Exception:
            ins = []                                       # sin trazas/datos: la sesion no aporta insights
        seen = set()
        for it in ins:
            k = (it["regla"].replace("-ref", ""), it.get("loc", ""))   # R1 y R1-ref = mismo problema
            if k in seen:
                continue
            seen.add(k)
            r = rec.setdefault(k, {"n": 0, "msg": it.get("msg", ""), "t": it.get("t", 0) or 0})
            r["n"] += 1
            r["t"] = max(r["t"], it.get("t", 0) or 0)
        sessions.append({"type": _session_type(d), "n_clean": cl["n_clean"], "best": b,
                         "fuels": s_fuels, "lap_times": [l["lap_time"] for l in cl["clean"]]})
    recurring = sorted(((k, v) for k, v in rec.items() if v["n"] >= 2),
                       key=lambda kv: (kv[1]["n"], kv[1]["t"]), reverse=True)

    def _pool(types):
        fu, lt = [], []
        for s in sessions:
            if s["type"] in types:
                fu += s["fuels"]
                lt += s["lap_times"]
        return fu, lt
    fuels, lap_times = _pool({"practice", "qualify"})      # base limpia para consumo/ritmo
    if not fuels:                                          # fallback: lo que haya (incl. carrera)
        fuels, lap_times = _pool({"practice", "qualify", "race", "test", "?"})
    return {"car": car, "track": track, "n_sessions": len(folders), "sessions": sessions,
            "best_overall": best_overall, "recurring": recurring,
            "consumption": st.median(fuels) if fuels else None,
            "lap_time": st.median(lap_times) if lap_times else None, "n_fuel": len(fuels)}


def report_combo(filtro):
    r = _resolve_combo(filtro)
    if not r:
        print("No encontre sesiones para ese filtro. Proba --list para ver las combinaciones.")
        return
    car, track, folders, others = r
    cs = combo_struct(car, track, folders)
    print(f"\n=== Combo · {car} @ {track} · {cs['n_sessions']} sesiones ===")
    if others:
        print("  (el filtro tambien matcheo: "
              + ", ".join(f"{c[0]}@{c[1]}" for c in others) + " — uso el mas reciente)")
    print("  sesiones (cronologico):")
    for s in cs["sessions"]:
        print(f"    {s['type']:>9}  {s['n_clean']:>2} limpias  mejor {_fmt_t(s['best']).strip()}")
    print(f"  mejor vuelta del combo : {_fmt_t(cs['best_overall']).strip()}")
    pbests = [s["best"] for s in cs["sessions"] if s["type"] == "practice" and s["best"]]
    if len(pbests) >= 2:                          # tendencia solo entre practicas (condiciones parejas)
        delta = pbests[-1] - pbests[0]
        tag = "bajando" if delta < -0.1 else "subiendo" if delta > 0.1 else "plano"
        print(f"  tendencia entre practicas (1ra->ult): {delta:+.2f}s  ({tag})")
    if cs["consumption"]:
        print(f"  consumo medio: {cs['consumption']:.2f} L/v  ·  ritmo medio "
              f"{_fmt_t(cs['lap_time']).strip()}  (n={cs['n_fuel']} vueltas)")
    if cs["recurring"]:
        print("\n  INSIGHTS RECURRENTES (en >=2 sesiones = lo persistente, no un mal dia):")
        for (regla, loc), v in cs["recurring"][:6]:
            print(f"    [{v['n']} ses · {regla}] {v['msg']}")
    else:
        print("\n  (sin insights recurrentes aun: corre mas tandas del combo o falta data de trazas)")
    print()


def race_fuel_struct(cs, minutes=None, laps=None):
    """Estima vueltas + carga de combustible desde el consumo/ritmo agregado del combo."""
    fpl, lt = cs["consumption"], cs["lap_time"]
    if not fpl or not lt:
        return None
    if laps:
        laps_total, safety, basis = int(laps), _SAFETY_LAPS_LAPS, f"{int(laps)} vueltas"
    else:
        laps_total = math.floor(minutes * 60 / lt) + 1     # + vuelta de cierre (cruzas tras el reloj 0)
        safety, basis = _SAFETY_LAPS_TIMED, f"{minutes:.0f} min"
    to_finish = laps_total * fpl
    margin = max(safety * fpl, _MARGIN_PCT * to_finish)
    return {"basis": basis, "laps": laps_total, "fpl": fpl, "lap_time": lt,
            "to_finish": to_finish, "margin": margin, "load": to_finish + margin}


def report_race_fuel(filtro, minutes=None, laps=None):
    r = _resolve_combo(filtro)
    if not r:
        print("No encontre sesiones para ese combo. Proba --list.")
        return
    car, track, folders, _ = r
    cs = combo_struct(car, track, folders)
    rf = race_fuel_struct(cs, minutes=minutes, laps=laps)
    if not rf:
        print(f"Sin consumo medido para {car} @ {track} (faltan vueltas con fuel_used). Maneja unas vueltas grabando.")
        return
    print(f"\n=== Plan de combustible · {car} @ {track} · carrera de {rf['basis']} ===")
    print(f"  base ({cs['n_sessions']} sesiones): {rf['fpl']:.2f} L/v · ritmo "
          f"{_fmt_t(rf['lap_time']).strip()} (n={cs['n_fuel']} vueltas)")
    print(f"  vueltas estimadas : ~{rf['laps']}")
    print(f"  para terminar     : {rf['to_finish']:.1f} L")
    print(f"  CARGA recomendada : {rf['load']:.0f} L  (+{rf['margin']:.1f} L de margen)")
    print("  ojo: combustible = peso, no sobrecargues; en vivo el ⚙ del dash lo afina con tu consumo real.")
    print()


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
    ap.add_argument("--tyres", action="store_true",
                    help="gomas (beta): presion termica + camber por rueda sobre las vueltas limpias")
    ap.add_argument("--balance", action="store_true",
                    help="balance sobre/subviraje por curva + momento de inestabilidad (slip por rueda)")
    ap.add_argument("--last", type=int, metavar="N",
                    help="analizar solo las N vueltas validas mas recientes (aislar el stint/setup actual)")
    ap.add_argument("--combo", nargs="?", const="", metavar="FILTRO", default=None,
                    help="agrega TODAS las sesiones de un auto+pista (insights recurrentes, tendencia, "
                         "consumo). FILTRO opcional (substring de auto/pista); sin filtro usa la ultima combinacion")
    ap.add_argument("--race-fuel", type=float, metavar="MIN", dest="race_fuel",
                    help="carga estimada de combustible para una carrera por TIEMPO (minutos) desde el consumo del combo")
    ap.add_argument("--race-laps", type=int, metavar="N", dest="race_laps",
                    help="carga estimada para una carrera por VUELTAS (N) desde el consumo del combo")
    a = ap.parse_args()
    if a.last:
        global _LAST
        _LAST = a.last

    if a.list:
        groups = _group_combos()
        if not groups:
            print(f"sin sesiones en {TELEM}")
            return
        print(f"sesiones en {TELEM} (por combinacion auto+pista):")
        for k in sorted(groups, key=lambda k: os.path.getmtime(groups[k][-1]), reverse=True):
            car, track = k
            fs = groups[k]
            total = sum(len(_load(d)[1]) for d in fs)
            print(f"  {track} · {car}  — {len(fs)} sesiones · {total} vueltas")
            for d in fs:
                _, laps = _load(d)
                ts = os.path.basename(d).split("__")[-1]
                print(f"      {_session_type(d):>9} {ts}  ({len(laps)} v)")
        return

    # modo COMBO / plan de combustible desde el historico (agrega por auto+pista)
    if a.race_fuel is not None or a.race_laps is not None:
        report_race_fuel(a.combo, minutes=a.race_fuel, laps=a.race_laps)
        return
    if a.combo is not None:
        report_combo(a.combo)
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
    elif a.tyres:
        report_tyres(folder)
    elif a.balance:
        report_balance(folder)
    else:
        report_session(folder)


if __name__ == "__main__":
    main()
