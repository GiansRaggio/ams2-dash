#!/usr/bin/env python3
"""Tests del analisis post-stint (analyze_telemetry): curvas, coasting, delta.

Genera trazas sinteticas con curvas (dips de velocidad) y zonas de coasting, y
verifica deteccion de apex, coasting, interpolacion y los reportes end-to-end.
Correr: python tools/test_analysis.py
"""
import gzip
import json
import math
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))
import ams2_telemetry as T
import analyze_telemetry as A

CORNER_DS = (1000.0, 2500.0)     # apex de 2 curvas
LENGTH = 4000.0


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


def make_arrays(n=500, corner_slow=0.0):
    """Devuelve dist, speed, throttle, brake, t de una vuelta sintetica."""
    dist, spd, thr, brk, tt = [], [], [], [], []
    t = 0.0
    ds = LENGTH / n
    for k in range(n):
        d = k * ds
        v = 200.0
        for cd in CORNER_DS:
            v -= 120.0 * math.exp(-((d - cd) / 160.0) ** 2)   # baja a ~80 en el apex
        if abs(d - CORNER_DS[0]) < 200:                        # vuelta lenta: pierde en T1
            v -= corner_slow
        v = max(55.0, v)
        # coast 100m antes de frenar, luego freno hasta el apex, luego acelera
        th, bk = 1.0, 0.0
        for cd in CORNER_DS:
            if cd - 250 <= d < cd - 150:
                th, bk = 0.0, 0.0
            elif cd - 150 <= d < cd:
                th, bk = 0.0, 1.0
        dist.append(d); spd.append(v); thr.append(th); brk.append(bk); tt.append(t)
        t += ds / (v / 3.6)
    return dist, spd, thr, brk, tt, t


def write_session(base):
    d = os.path.join(base, "Test__Car__practice__x")
    os.makedirs(d)
    json.dump({"track": "Test", "car": "Car", "channels": T.HEADER},
              open(os.path.join(d, "session.json"), "w"))
    idx = {h: i for i, h in enumerate(T.HEADER)}
    laps = []
    for lap, slow in ((1, 25.0), (2, 0.0)):           # lap1 mas lenta en T1, lap2 = mejor
        dist, spd, thr, brk, tt, ltime = make_arrays(corner_slow=slow)
        rows = []
        for k in range(len(dist)):
            r = [0.0] * len(T.HEADER)
            r[idx["t"]] = round(tt[k], 3)
            r[idx["lap_dist"]] = round(dist[k], 2)
            r[idx["speed_kmh"]] = round(spd[k], 2)
            r[idx["throttle"]] = thr[k]
            r[idx["brake"]] = brk[k]
            rows.append(",".join(map(str, r)))
        tname = f"L{lap:03d}_{ltime:.3f}s.csv.gz"
        with gzip.open(os.path.join(d, tname), "wt", encoding="utf-8") as f:
            f.write(",".join(T.HEADER) + "\n")
            f.write("\n".join(rows) + "\n")
        laps.append({"lap": lap, "lap_time": round(ltime, 3), "valid": True,
                     "samples": len(dist), "fuel_used": 2.5,
                     "wear_delta": [0.02, 0.02, 0.03, 0.03],
                     "tyre_temp_avg": [88, 88, 90, 90],
                     "sectors": [round(ltime / 3, 3)] * 3, "compound": "Soft",
                     "trace": tname})
    open(os.path.join(d, "summary.jsonl"), "w").write(
        "\n".join(json.dumps(l) for l in laps))
    # sectors.jsonl: 2 limpias + 1 invalidada con buen S1/S2 a rescatar (S3 sucio)
    secrecs = [
        {"lap": 1, "lap_time": 95.0, "sectors": [30.0, 33.0, 32.0], "sec_valid": [True, True, True], "invalid": False},
        {"lap": 2, "lap_time": 93.0, "sectors": [29.5, 32.0, 31.5], "sec_valid": [True, True, True], "invalid": False},
        {"lap": 3, "lap_time": 99.0, "sectors": [29.0, 31.0, 39.0], "sec_valid": [True, True, False], "invalid": True},
    ]
    open(os.path.join(d, "sectors.jsonl"), "w").write("\n".join(json.dumps(r) for r in secrecs))
    return d


def write_sat_session(base, ax_signo=-1.0):
    """Sesion sintetica para el reporte de SATURACION, armada con el patron REAL medido:
    en una curva la rueda DESCARGADA satura casi todo el tiempo (mediana 81% en el
    corpus) y la CARGADA mucho menos (26%). Con accel_x negativo cargan las IZQUIERDAS,
    asi que FL/RL son las que miden y FR/RR las que no significan nada.

    Si el veredicto sale FR o RR, el analizador esta acusando a la rueda ociosa -- que es
    justo el bug que esto cubre (pasaba en 66 de 67 curvas del corpus)."""
    d = os.path.join(base, "SatTest__Car__practice__x")
    os.makedirs(d)
    json.dump({"track": "SatTest", "car": "Car", "channels": T.HEADER},
              open(os.path.join(d, "session.json"), "w"))
    idx = {h: i for i, h in enumerate(T.HEADER)}
    laps = []
    for lap in (1, 2, 3):
        dist, spd, thr, brk, tt, ltime = make_arrays()
        rows = []
        for k in range(len(dist)):
            r = [0.0] * len(T.HEADER)
            r[idx["t"]] = round(tt[k], 3)
            r[idx["lap_dist"]] = round(dist[k], 2)
            r[idx["speed_kmh"]] = round(spd[k], 2)
            r[idx["throttle"]] = thr[k]
            r[idx["brake"]] = brk[k]
            # dentro de la curva: G lateral fuerte y saturacion invertida a proposito
            en_curva = any(abs(dist[k] - cd) < 180 for cd in CORNER_DS)
            r[idx["accel_x"]] = (12.0 * ax_signo) if en_curva else 0.0
            # margen de agarre: 0 = saturada. descargadas SIEMPRE en cero dentro de la
            # curva; cargadas solo en un tercio (k % 3 == 0)
            desc_sat, carg_sat = 0.0, (0.0 if k % 3 == 0 else 0.9)
            cargadas = ("FL", "RL") if ax_signo < 0 else ("FR", "RR")
            for w in ("FL", "FR", "RL", "RR"):
                dentro = carg_sat if w in cargadas else desc_sat
                r[idx["tyre_grip_" + w]] = dentro if en_curva else 0.9
            rows.append(",".join(map(str, r)))
        tname = f"L{lap:03d}_{ltime:.3f}s.csv.gz"
        with gzip.open(os.path.join(d, tname), "wt", encoding="utf-8") as f:
            f.write(",".join(T.HEADER) + "\n")
            f.write("\n".join(rows) + "\n")
        laps.append({"lap": lap, "lap_time": round(ltime, 3), "valid": True,
                     "samples": len(dist), "fuel_used": 2.5, "compound": "Soft",
                     "sectors": [round(ltime / 3, 3)] * 3, "trace": tname})
    open(os.path.join(d, "summary.jsonl"), "w").write(
        "\n".join(json.dumps(l) for l in laps))
    return d


def test_saturacion_solo_rueda_cargada():
    """El veredicto de saturacion tiene que salir de la rueda que la curva CARGA.

    Medido sobre 67 curvas del corpus: la descargada satura una mediana de 81.5% del
    tiempo contra 26.1% la cargada, porque tyre_grip es margen SIN USAR y una goma sin
    carga casi no tiene margen que dar. Con el criterio viejo (maximo de las cuatro) el
    veredicto caia en una rueda descargada en 66 de 67 curvas."""
    ok = True
    for signo, cargadas, descargadas in ((-1.0, ("FL", "RL"), ("FR", "RR")),
                                         (+1.0, ("FR", "RR"), ("FL", "RL"))):
        base = tempfile.mkdtemp(prefix="sattest_")
        try:
            sat = A.saturation_struct(write_sat_session(base, signo))
            lado = "izquierdas" if signo < 0 else "derechas"
            if not sat or not sat["corners"]:
                ok = _ok(f"cargan las {lado}: hay reporte", False) and ok
                continue
            c = sat["corners"][0]
            ok = _ok(f"cargan las {lado}: el veredicto es de una rueda CARGADA",
                     c["peor"] in cargadas, f"peor={c['peor']} pct={c['peor_pct']}") and ok
            ok = _ok(f"cargan las {lado}: NO acusa a la ociosa (que satura 100%)",
                     c["peor"] not in descargadas,
                     {w: c["pct"][w] for w in ("FL", "FR", "RL", "RR")}) and ok
            ok = _ok(f"cargan las {lado}: marca cuales cargan",
                     set(c["cargadas"]) == set(cargadas), c["cargadas"]) and ok
        finally:
            shutil.rmtree(base, ignore_errors=True)
    # sin canal de G lateral (grabaciones viejas) no se opina, en vez de adivinar
    base = tempfile.mkdtemp(prefix="sattest_")
    try:
        folder = write_sat_session(base, -1.0)
        for lp in [f for f in os.listdir(folder) if f.endswith(".csv.gz")]:
            p = os.path.join(folder, lp)
            with gzip.open(p, "rt") as f:
                head = f.readline().strip().split(",")
                cuerpo = f.read()
            i = head.index("accel_x")
            head.pop(i)
            filas = []
            for ln in cuerpo.splitlines():
                r = ln.split(",")
                r.pop(i)
                filas.append(",".join(r))
            with gzip.open(p, "wt", encoding="utf-8") as f:
                f.write(",".join(head) + "\n")
                f.write("\n".join(filas) + "\n")
        sat = A.saturation_struct(folder)
        c = sat["corners"][0] if sat and sat["corners"] else None
        ok = _ok("sin canal lateral -> sin veredicto (no adivina)",
                 c is not None and c["peor"] is None and c["lado"] == "=",
                 None if c is None else f"peor={c['peor']} lado={c['lado']}") and ok
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return ok


def write_min_session(telem, track, car, ftype, ts, laps):
    """Sesion minima (session.json + summary.jsonl, sin trazas) para tests del modo combo.
    laps = lista de (lap_time, fuel_used)."""
    d = os.path.join(telem, f"{track}__{car}__{ftype}__{ts}")
    os.makedirs(d)
    json.dump({"track": track, "car": car}, open(os.path.join(d, "session.json"), "w"))
    with open(os.path.join(d, "summary.jsonl"), "w") as f:
        for i, (lt, fu) in enumerate(laps, 1):
            f.write(json.dumps({"lap": i, "lap_time": lt, "valid": True,
                                "fuel_used": fu, "trace": None}) + "\n")
    return d


def main():
    print("test_corners / coasting / interp:")
    dist, spd, thr, brk, tt, _ = make_arrays()
    cs = A._corners(dist, spd)
    _ok("detecta 2 curvas", len(cs) == 2, [c["apex"] for c in cs])
    if len(cs) == 2:
        _ok("apex cerca de 1000m", abs(cs[0]["apex"] - 1000) < 120, cs[0]["apex"])
        _ok("apex cerca de 2500m", abs(cs[1]["apex"] - 2500) < 120, cs[1]["apex"])
        _ok("vmin del apex ~80 km/h", cs[0]["vmin"] < 95, cs[0]["vmin"])
    zs = A._coasting(dist, thr, brk, spd)
    _ok("detecta >=2 tramos de coasting", len(zs) >= 2, [z[1] for z in zs])
    _ok("interp lineal correcta", abs(A._interp([0, 10], [0, 100], 5) - 50) < 1e-6)
    dm, sm = A._mono([0, 1, 2, 1, 3], [9, 8, 7, 6, 5])[:2]
    _ok("_mono recorta el wrap de meta", dm == [0, 1, 2, 3], dm)

    print("\ntest recuperacion de sectores / vuelta ideal:")
    rec = A._lap_sectors({"lap_time": 122.42, "sectors": [0.027, 48.42, 48.70]})
    _ok("recupera S1 roto (~25.30)", rec is not None and abs(rec[0] - 25.30) < 0.02, rec)
    intact = A._lap_sectors({"lap_time": 90.0, "sectors": [30.0, 30.0, 30.0]})
    _ok("sectores sanos quedan intactos", intact == [30.0, 30.0, 30.0], intact)
    _ok("None si falta lap_time", A._lap_sectors({"sectors": [10, 10, 10]}) is None)
    _ok("None si S1 recuperado da <=0", A._lap_sectors({"lap_time": 50.0, "sectors": [0.02, 30.0, 30.0]}) is None)

    print("\ntest vuelta ideal: NO se rescatan sectores de vueltas sucias:")
    _b = tempfile.mkdtemp(prefix="anaideal_")
    try:
        _f = write_session(_b)
        # El fixture trae la vuelta 3 INVALIDA con sec_valid=[True,True,False] y los
        # mejores S1 (29.0) y S2 (31.0) de la sesion. Esa atribucion no es de fiar -- el
        # flag de invalidacion de AMS2 llega tarde, ver el comentario en ams2_telemetry.py
        # -- asi que la vuelta ideal tiene que ignorar la vuelta entera y armarse solo con
        # las limpias: S1 29.5 + S2 32.0 + S3 31.5 = 93.0, la mejor vuelta limpia.
        _st = A.sectors_struct(_f)
        _ok("la ideal no toma el S1 de la invalidada", _st and _st["best_s"][0] == 29.5,
            _st and _st["best_s"])
        _ok("la ideal no toma el S2 de la invalidada", _st and _st["best_s"][1] == 32.0,
            _st and _st["best_s"])
        _ok("ninguna vuelta sucia es duena de un sector", _st and 3 not in _st["owners"],
            _st and _st["owners"])
        _ok("la ideal da 93.0 (todas limpias)", _st and abs(_st["ideal"] - 93.0) < 0.01,
            _st and _st["ideal"])
    finally:
        shutil.rmtree(_b, ignore_errors=True)

    print("\ntest reportes end-to-end (sin crash):")
    base = tempfile.mkdtemp(prefix="anatest_")
    try:
        folder = write_session(base)
        for fn, label in ((lambda: A.report_session(folder), "report_session"),
                          (lambda: A.report_lap(folder, 1), "report_lap"),
                          (lambda: A.report_vs(folder, 1, None), "report_vs (1 vs mejor)")):
            try:
                fn()
                _ok(f"{label} corre", True)
            except Exception as e:
                _ok(f"{label} corre", False, repr(e))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\ntest modo combo (agrega por auto+pista):")
    base = tempfile.mkdtemp(prefix="combotest_")
    old_telem, old_ref = A.TELEM, A.REFDIR
    try:
        telem = os.path.join(base, "telemetry")
        os.makedirs(telem)
        A.TELEM = telem
        A.REFDIR = os.path.join(base, "norefs")        # aislar de las referencias reales
        # combo A: Monza + GTX, 2 practicas + 1 carrera (atipica, NO debe ensuciar el consumo)
        write_min_session(telem, "Monza", "GTX", "practice", "20260101_100000",
                          [(90.0, 3.0), (89.5, 3.1), (89.8, 2.9)])
        write_min_session(telem, "Monza", "GTX", "practice", "20260101_110000",
                          [(89.2, 3.0), (89.0, 3.0)])
        write_min_session(telem, "Monza", "GTX", "race", "20260101_120000",
                          [(95.0, 5.0), (94.0, 5.0)])
        write_min_session(telem, "Spa", "GTY", "practice", "20260101_130000",
                          [(120.0, 3.5), (119.5, 3.4)])
        write_min_session(telem, "x", "x", "race", "20260101_140000", [(80.0, 4.0)])   # placeholder

        groups = A._group_combos()
        _ok("agrupa en 2 combos (ignora placeholder x)", len(groups) == 2, list(groups))
        _ok("combo Monza/GTX = 3 sesiones",
            ("GTX", "Monza") in groups and len(groups[("GTX", "Monza")]) == 3, list(groups))
        r = A._resolve_combo("gtx")
        _ok("resuelve combo por filtro (substring)",
            r is not None and r[0] == "GTX" and len(r[2]) == 3, r and (r[0], len(r[2])))

        cs = A.combo_struct("GTX", "Monza", groups[("GTX", "Monza")])
        _ok("mejor vuelta del combo = 89.0", abs(cs["best_overall"] - 89.0) < 1e-6, cs["best_overall"])
        _ok("consumo SOLO de practicas (~3.0, sin la carrera de 5.0)",
            abs(cs["consumption"] - 3.0) < 0.05, cs["consumption"])
        _ok("ritmo medio de practicas (~89.5)", abs(cs["lap_time"] - 89.5) < 1.0, cs["lap_time"])

        rf = A.race_fuel_struct(cs, minutes=30)           # 1800/89.5=20.1 -> 20+1 = 21 vueltas
        _ok("race-fuel timed: ~21 vueltas", rf["laps"] == 21, rf["laps"])
        _ok("race-fuel timed: carga > combustible para terminar", rf["load"] > rf["to_finish"],
            (round(rf["load"], 1), round(rf["to_finish"], 1)))
        rl = A.race_fuel_struct(cs, laps=20)
        _ok("race-fuel laps: 20 vueltas exactas", rl["laps"] == 20, rl["laps"])
        _ok("race-fuel laps: carga = 20*3 + margen (63)", abs(rl["load"] - 63.0) < 0.1, rl["load"])
        _ok("_session_type parsea el tipo", A._session_type("Monza__GTX__race__ts") == "race",
            A._session_type("Monza__GTX__race__ts"))
    finally:
        A.TELEM, A.REFDIR = old_telem, old_ref
        shutil.rmtree(base, ignore_errors=True)

    print("\ntest saturacion (solo la rueda que la curva CARGA):")
    test_saturacion_solo_rueda_cargada()
    print("\ndone.")


if __name__ == "__main__":
    main()
