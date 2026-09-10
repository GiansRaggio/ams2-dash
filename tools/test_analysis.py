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


def write_contact_session(base, con_canales=True):
    """Sesion sintetica para el conteo de CONTACTOS, con el patron real del juego.

    V1: `coll_mag` arranca en 0, salta a 0.8 y se QUEDA en 0.8 durante 200 muestras
    (4 s), y despues salta a 1.5. Son DOS choques, no 400 -- el canal es estado
    sostenido, no un pulso.

    V2 (invalidada): las 500 muestras en 1.5, heredado de V1 al cruzar meta. Es la
    trampa que importa: si el conteo mirara el valor y no el cambio, esta vuelta sin
    ningun toque sumaria 500 contactos mas.

    Con `con_canales=False` la traza se escribe con el header VIEJO (sin coll_*), que
    es lo que tienen las sesiones anteriores al 2026-08-20.
    """
    d = os.path.join(base, "Contacto__Auto__race__20260101_000000")
    os.makedirs(d, exist_ok=True)
    header = T.HEADER if con_canales else ["t", "lap_dist", "speed_kmh", "throttle", "brake"]
    json.dump({"track": "Contacto", "car": "Auto"}, open(os.path.join(d, "session.json"), "w"))
    idx = {h: i for i, h in enumerate(header)}

    def _traza(nombre, mags, idxs):
        with gzip.open(os.path.join(d, nombre), "wt", encoding="utf-8") as f:
            f.write(",".join(header) + "\n")
            for k in range(len(mags)):
                r = [0.0] * len(header)
                r[idx["t"]] = round(k * 0.02, 3)              # 50 Hz, como el grabador
                r[idx["lap_dist"]] = round(k * 8.0, 2)
                r[idx["speed_kmh"]] = 150.0
                if con_canales:
                    r[idx["coll_mag"]] = mags[k]
                    r[idx["coll_idx"]] = idxs[k]
                    r[idx["race_pos"]] = 4.0
                f.write(",".join(map(str, r)) + "\n")

    mags = [0.0] * 100 + [0.8] * 200 + [1.5] * 200
    idxs = [-1.0] * 100 + [3.0] * 200 + [5.0] * 200
    _traza("L001_90.000s.csv.gz", mags, idxs)
    _traza("X002_95.000s.csv.gz", [1.5] * 500, [5.0] * 500)   # el valor heredado, sin toques
    with open(os.path.join(d, "timeline.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"type": "lap", "lap": 1, "kind": "flying", "lap_time": 90.0,
                            "trace": "L001_90.000s.csv.gz"}) + "\n")
        f.write(json.dumps({"type": "lap", "lap": 2, "kind": "invalid", "lap_time": 95.0,
                            "invalid": True, "trace": "X002_95.000s.csv.gz"}) + "\n")
    return d


def test_contactos():
    ok = True
    base = tempfile.mkdtemp(prefix="anacont_")
    try:
        cs, motivo = A.contactos_struct(write_contact_session(base))
        ok = _ok("hay estructura de contactos", cs is not None, motivo) and ok
        if cs:
            # EL test: 400 muestras con coll_mag>0 son 2 choques. Contar frames daria 400.
            ok = _ok("cuenta 2 contactos, no 400 (el canal es estado, no pulso)",
                     cs["n_contactos"] == 2, cs["n_contactos"]) and ok
            ok = _ok("la vuelta 2 hereda el valor y NO suma contactos",
                     all(e["vuelta"] == 1 for e in cs["contactos"]),
                     [e["vuelta"] for e in cs["contactos"]]) and ok
            ok = _ok("recorre tambien la invalidada (X###)", cs["n_vueltas_con_traza"] == 2,
                     cs["n_vueltas_con_traza"]) and ok
            ok = _ok("magnitudes: la de cada choque, no la maxima repetida",
                     [e["magnitud"] for e in cs["contactos"]] == [0.8, 1.5],
                     [e["magnitud"] for e in cs["contactos"]]) and ok
            ok = _ok("magnitud maxima 1.5", cs["magnitud_max"] == 1.5, cs["magnitud_max"]) and ok
            ok = _ok("anota contra quien (coll_idx)",
                     [e["rival"] for e in cs["contactos"]] == [3, 5],
                     [e["rival"] for e in cs["contactos"]]) and ok
            ok = _ok("tasa por 10 vueltas (compara sesiones de distinto largo)",
                     cs["contactos_por_10v"] == 10.0, cs["contactos_por_10v"]) and ok
            # La invalidacion sale de la MISMA funcion que usa evaluar(): una sola definicion.
            ok = _ok("junta las invalidadas y su tasa",
                     cs["invalidadas"] == 1 and cs["tasa_invalidacion_pct"] == 50.0,
                     (cs["invalidadas"], cs["tasa_invalidacion_pct"])) and ok
            ok = _ok("sin dano ni crash: sin avisos inventados", cs["avisos"] == [],
                     cs["avisos"]) and ok

        shutil.rmtree(base, ignore_errors=True)
        base = tempfile.mkdtemp(prefix="anacont0_")
        cs2, motivo2 = A.contactos_struct(write_contact_session(base, con_canales=False))
        ok = _ok("sesion vieja sin los canales: None, no 0 contactos", cs2 is None, cs2) and ok
        ok = _ok("y dice por que", bool(motivo2) and "2026-08-20" in motivo2, motivo2) and ok
        try:
            A.report_contactos(write_contact_session(base))
            ok = _ok("report_contactos corre", True) and ok
        except Exception as e:
            ok = _ok("report_contactos corre", False, repr(e)) and ok
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return ok


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

    print("\ntest consistencia (CV%, la metrica con la que se califica):")

    def _sesion_cv(base, tiempos, corridas=None):
        """Sesion minima: summary.jsonl, que es de donde sale el CV.

        `corridas` = [k1, k2, ...] cuantas vueltas cronometradas tiene cada corrida
        de pista; con eso se escribe tambien `timeline.jsonl` con la vuelta de
        salida que abre cada corrida y la vuelta de boxes que las separa. Sin
        `corridas` la sesion queda SIN linea de tiempo a proposito: ese es el
        fallback (primeras n de la carpeta) y hay que poder probarlo.
        """
        d = os.path.join(base, "CV__Auto__practice__20260101_000000")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "summary.jsonl"), "w", encoding="utf-8") as f:
            for i, t in enumerate(tiempos):
                f.write(json.dumps({"uid": i + 1, "lap": i + 1, "lap_time": t,
                                    "valid": True}) + "\n")
        tlf = os.path.join(d, "timeline.jsonl")
        if not corridas:
            # la carpeta se reusa entre casos: una linea de tiempo vieja aca
            # cambiaria el resultado del caso siguiente sin que se note
            if os.path.exists(tlf):
                os.remove(tlf)
            return d
        with open(tlf, "w", encoding="utf-8") as f:
            uid, lap = 0, 0
            for c, k in enumerate(corridas):
                if c:                                     # la vuelta de boxes: el BORDE
                    lap += 1
                    f.write(json.dumps({"type": "lap", "lap": lap, "lap_time": 120.0,
                                        "kind": "pit", "pit": True, "uid": None}) + "\n")
                lap += 1
                f.write(json.dumps({"type": "lap", "lap": lap, "lap_time": 110.0,
                                    "kind": "out", "out": True, "uid": None}) + "\n")
                for _ in range(k):
                    uid, lap = uid + 1, lap + 1
                    f.write(json.dumps({"type": "lap", "lap": lap, "kind": "flying",
                                        "lap_time": tiempos[uid - 1], "uid": uid}) + "\n")
        return d

    _b = tempfile.mkdtemp(prefix="anacv_")
    try:
        # 1) Ritmo parejo con DOS incidentes al principio. Es el caso real de Taruma:
        #    el MAD ignora bien los outliers, pero una pendiente por minimos cuadrados
        #    se deja arrastrar por ellos, inventa una tendencia y arruina el resto.
        c = A.consistency_struct(_sesion_cv(_b, [80.4, 85.1, 84.9, 80.4, 80.1, 80.5, 80.6, 80.4]))
        _ok("incidentes: los aparta en vez de promediarlos", c and c["incidentes"] == 2,
            c and c["incidentes"])
        _ok("incidentes: el CV queda bajo (ritmo real parejo)", c and c["cv_pct"] < 0.5,
            c and c["cv_pct"])
        _ok("incidentes: NO inventa tendencia", c and abs(c["deriva_pct_vuelta"]) < 0.15,
            c and c["deriva_pct_vuelta"])
        _ok("incidentes: califica igual", c and c["califica"], c and c["veredicto"])

        # 2) Mejora sostenida: no es dispersion, es que todavia esta aprendiendo el
        #    circuito. Marcarlo "disperso" seria penalizarlo por mejorar.
        c2 = A.consistency_struct(_sesion_cv(_b, [105.0, 104.0, 103.0, 102.0, 101.0,
                                                  100.0, 99.0, 98.0]))
        _ok("mejorando: no califica como consistencia", c2 and not c2["califica"],
            c2 and c2["veredicto"])
        _ok("mejorando: lo detecta por el signo", c2 and c2["deriva_pct_vuelta"] < 0,
            c2 and c2["deriva_pct_vuelta"])

        # 3) Degradacion: SI califica, pero avisa que el problema es otro.
        c3 = A.consistency_struct(_sesion_cv(_b, [98.0, 99.0, 100.0, 101.0, 102.0,
                                                  103.0, 104.0, 105.0]))
        _ok("degradando: califica", c3 and c3["califica"], c3 and c3["veredicto"])
        _ok("degradando: lo marca", c3 and c3["degradando"], c3 and c3["deriva_pct_vuelta"])

        # 4) Consistente de verdad
        c4 = A.consistency_struct(_sesion_cv(_b, [90.0, 90.1, 89.9, 90.05, 90.0,
                                                  89.95, 90.1, 90.0]))
        _ok("parejo: CV bajo y veredicto excelente",
            c4 and c4["cv_pct"] < 0.3 and c4["veredicto"] == "excelente",
            c4 and (c4["cv_pct"], c4["veredicto"]))

        # 5) Muestra corta: sin veredicto, no un numero inventado
        _ok("bajo 6 vueltas: None", A.consistency_struct(_sesion_cv(_b, [90.0, 90.1, 89.9])) is None)
        # 6-7 vueltas: se calcula y se muestra, pero NO califica (regla de las 8 de
        # docs/evaluacion.md). Antes salia con veredicto igual que una de 8.
        c7 = A.consistency_struct(_sesion_cv(_b, [90.0, 90.1, 89.9, 90.05, 90.0, 90.1, 89.95]))
        _ok("7 vueltas: hay CV pero no califica", c7 is not None and c7["corta"] and not c7["califica"]
            and "desde 8" in c7["veredicto"], c7 and c7["veredicto"])
        _ok("8 vueltas: califica", c4["califica"] and not c4["corta"])
        _ok("sin sectors.jsonl el CV por sector es None, no revienta", c4.get("cv_sector_pct") is None)

        # 6) La pendiente robusta es el nucleo del arreglo
        _ok("Theil-Sen ignora el outlier", abs(A._slope_robusta([10, 10, 10, 99, 10, 10])) < 0.6,
            A._slope_robusta([10, 10, 10, 99, 10, 10]))
        _ok("Theil-Sen ve la tendencia real", abs(A._slope_robusta([10, 11, 12, 13, 14]) - 1.0) < 0.01,
            A._slope_robusta([10, 11, 12, 13, 14]))

        print("\ntest cual es la tanda (el reconocimiento NO puntua):")
        # El protocolo: 5 vueltas de reconocimiento, PIT, y ahi si la tanda de 10.
        # Tomando las primeras 8 de la CARPETA calificaban las 5 lentas + 3 de la
        # tanda -- el peor de los dos mundos, porque ademas mezcla dos corridas.
        reco = [95.0, 94.0, 93.5, 93.0, 92.5]
        buena = [90.0, 90.1, 89.9, 90.05, 90.0, 89.95, 90.1, 90.0, 89.98, 90.02]
        d = _sesion_cv(_b, reco + buena, corridas=[5, 10])
        v, origen, k = A.vueltas_de_la_tanda(d)
        _ok("elige la ULTIMA corrida con 8+ vueltas", k == 2 and "corrida 2 de 2" in origen, origen)
        _ok("y son las 8 PRIMERAS de esa corrida, en orden",
            [l["lap_time"] for l in v] == buena[:8], [l["lap_time"] for l in v])
        c = A.consistency_struct(d)
        _ok("el CV sale de la tanda, no mezclado con el reconocimiento",
            c and c["cv_pct"] < 0.3 and c["veredicto"] == "excelente",
            c and (c["cv_pct"], c["veredicto"]))
        _ok("ninguna vuelta del reconocimiento entra al CV",
            c and not any(t in c["tiempos"] for t in reco), c and c["tiempos"])
        _ok("la mejor tambien es de la tanda (no la mejor de la carpeta)",
            A.mejor_de_la_tanda(d)["lap_time"] == 89.9, A.mejor_de_la_tanda(d)["lap_time"])
        _ok("y el struct dice de donde salio", c and c["tanda_corrida"] == 2, c and c["tanda_origen"])

        # tanda=k: el instructor manda cuando sabe cual quiere
        v1, origen1, k1 = A.vueltas_de_la_tanda(d, tanda=1)
        _ok("tanda=1 explicito: devuelve el reconocimiento",
            k1 == 1 and [l["lap_time"] for l in v1] == reco, (origen1, [l["lap_time"] for l in v1]))

        # sin linea de tiempo NO hay estructura que cortar: se cae a las primeras 8
        # de la carpeta, y se dice. Es el camino de las sesiones viejas.
        d2 = _sesion_cv(_b, reco + buena)
        v2, origen2, k2 = A.vueltas_de_la_tanda(d2)
        _ok("sin timeline: primeras 8 de la sesion", k2 is None and "primeras 8" in origen2, origen2)
        _ok("sin timeline: y avisa que no hay linea de tiempo",
            "sin linea de tiempo" in origen2, origen2)
        _ok("sin timeline: son literalmente las primeras 8 del summary",
            [l["lap_time"] for l in v2] == (reco + buena)[:8], [l["lap_time"] for l in v2])

        # una sesion cuya UNICA corrida no llega a 8: tampoco se inventa una tanda
        d3 = _sesion_cv(_b, reco + buena[:2], corridas=[5, 2])
        v3, origen3, k3 = A.vueltas_de_la_tanda(d3)
        _ok("ninguna corrida llega a 8: cae a la sesion y lo dice",
            k3 is None and "sin corrida de 8+" in origen3, origen3)
        _ok("y toma las 7 que hay, no menos", len(v3) == 7, len(v3))
    finally:
        shutil.rmtree(_b, ignore_errors=True)

    print("\ntest evaluacion (del archivo crudo a la nota):")
    _be = tempfile.mkdtemp(prefix="anaeval_")
    _refdir_orig = A.REFDIR
    try:
        A.REFDIR = os.path.join(_be, "refs")          # no tocar las referencias reales
        d = os.path.join(_be, "EV__Auto__practice__20260101_000000")
        os.makedirs(d, exist_ok=True)
        # el meta sale de session.json, no del nombre de la carpeta
        with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as f:
            json.dump({"car": "Auto", "track": "EV", "track_variation": "EV"}, f)
        tiempos = [90.0, 90.1, 89.95, 90.05, 90.0, 89.9, 90.1, 90.0]
        with open(os.path.join(d, "summary.jsonl"), "w", encoding="utf-8") as f:
            for i, t in enumerate(tiempos):
                f.write(json.dumps({"lap": i + 1, "lap_time": t, "valid": True, "rain": 0.0,
                                    "tc_setting": 2, "abs_setting": 3, "compound": "Soft",
                                    "sectors": [30.0, 30.0, t - 60.0]}) + "\n")
        with open(os.path.join(d, "timeline.jsonl"), "w", encoding="utf-8") as f:
            for i in range(10):                        # 10 de pista, 2 invalidadas -> 20%
                k = "invalid" if i < 2 else "flying"
                f.write(json.dumps({"type": "lap", "lap": i + 1, "kind": k}) + "\n")

        _ok("tasa de invalidacion sale del TIMELINE, no del summary",
            A.tasa_invalidacion(d) and A.tasa_invalidacion(d)["pct"] == 20.0,
            A.tasa_invalidacion(d))

        # sin referencia ni pauta: no hay nota, y dice por que
        e = A.evaluar(d)
        _ok("sin lo que falta: NO inventa nota total", e["nota"] is None, e["nota"])
        _ok("sin referencia: lo declara", any("tecnica" in x for x in e["faltantes"]),
            e["faltantes"])
        _ok("no normaliza sobre lo disponible", e.get("peso_cubierto", 0) < 1.0,
            e.get("peso_cubierto"))

        # con referencia guardada + pauta completa: nota entera
        os.makedirs(A.REFDIR, exist_ok=True)
        with open(os.path.join(A.REFDIR, "Auto__EV.json"), "w", encoding="utf-8") as f:
            json.dump({"car": "Auto", "track": "EV", "lap": 1, "lap_time": 88.2,
                       "sectors": [29.4, 29.4, 29.4],
                       "cond": {"mojado": False, "tc": 2, "abs": 3, "compuesto": "Soft"}}, f)
        e2 = A.evaluar(d, pauta={"racecraft": 80, "gestion": 70, "progreso": 60})
        _ok("con todo: emite nota total", e2["nota"] is not None, e2["nota"])
        _ok("con todo: cubre el 100% del peso", not e2["faltantes"], e2["faltantes"])
        gap = e2["dimensiones"]["tecnica"]["valor"]
        _ok("gap% contra la referencia", abs(gap - 100.0 * (89.9 - 88.2) / 88.2) < 0.05, gap)

        # LA PRUEBA QUE IMPORTA: misma carpeta -> misma nota. Si dos instructores
        # sacan numeros distintos con el mismo archivo, el instrumento no sirve.
        e3 = A.evaluar(d, pauta={"racecraft": 80, "gestion": 70, "progreso": 60})
        _ok("determinista: la misma sesion da la misma nota", e2["nota"] == e3["nota"],
            (e2["nota"], e3["nota"]))

        # referencia hecha en otras condiciones -> avisa que el gap no es limpio
        with open(os.path.join(A.REFDIR, "Auto__EV.json"), "w", encoding="utf-8") as f:
            json.dump({"car": "Auto", "track": "EV", "lap": 1, "lap_time": 88.2,
                       "sectors": [29.4, 29.4, 29.4],
                       "cond": {"mojado": False, "tc": 8, "abs": 8, "compuesto": "Soft"}}, f)
        e4 = A.evaluar(d, pauta={"racecraft": 80, "gestion": 70, "progreso": 60})
        _ok("referencia con otras ayudas: avisa que el gap no es limpio",
            "aviso" in e4["dimensiones"]["tecnica"], e4["dimensiones"]["tecnica"])

        # sesion que mezcla condiciones: se corta antes de calificar nada
        d2 = os.path.join(_be, "MX__Auto__race__20260101_000000")
        os.makedirs(d2, exist_ok=True)
        with open(os.path.join(d2, "session.json"), "w", encoding="utf-8") as f:
            json.dump({"car": "Auto", "track": "MX"}, f)
        with open(os.path.join(d2, "summary.jsonl"), "w", encoding="utf-8") as f:
            for i, t in enumerate(tiempos):
                f.write(json.dumps({"lap": i + 1, "lap_time": t, "valid": True,
                                    "rain": 0.0 if i < 4 else 0.5, "tc_setting": 2,
                                    "abs_setting": 3,
                                    "compound": "Soft" if i < 4 else "Wet"}) + "\n")
        e5 = A.evaluar(d2, pauta={"racecraft": 80, "gestion": 70, "progreso": 60})
        _ok("sesion mezclada: no califica ninguna dimension", not e5["dimensiones"],
            list(e5["dimensiones"]))
        _ok("sesion mezclada: sin nota", e5["nota"] is None)

        # la escala no puede dar sorpresas en los bordes
        _ok("nota: gap 0% -> 100", A._nota(0.0, A._ESCALA_GAP) == 100.0)
        _ok("nota: gap enorme se ancla, no se va a negativo",
            A._nota(50.0, A._ESCALA_GAP) == 20.0, A._nota(50.0, A._ESCALA_GAP))
        _ok("nota: interpola entre cortes", 85.0 > A._nota(2.75, A._ESCALA_GAP) > 70.0,
            A._nota(2.75, A._ESCALA_GAP))
    finally:
        A.REFDIR = _refdir_orig
        shutil.rmtree(_be, ignore_errors=True)

    print("\ntest comparabilidad (que dos vueltas se puedan comparar de verdad):")
    _bc = tempfile.mkdtemp(prefix="anacmp_")
    try:
        def _ses(nombre, filas, meta=None):
            d = os.path.join(_bc, nombre)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "summary.jsonl"), "w", encoding="utf-8") as f:
                for i, r in enumerate(filas):
                    base = {"lap": i + 1, "lap_time": 90.0 + i * 0.05, "valid": True,
                            "rain": 0.0, "tc_setting": 2, "abs_setting": 3, "compound": "Soft"}
                    base.update(r)
                    f.write(json.dumps(base) + "\n")
            if meta:
                with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f)
            return d

        ok = _ses("OK__Auto__practice__20260101_000000", [{}] * 8)
        dom, av = A.comparabilidad(ok)
        _ok("condiciones estables: sin avisos de cambio",
            not any("CAMBIO" in a for a in av), av)
        _ok("detecta que estuvo seco", dom and dom["mojado"] is False, dom)

        # goma cambiada a mitad: el caso real de Buenos Aires
        mix = _ses("MIX__Auto__race__20260101_000000",
                   [{}] * 4 + [{"compound": "Wet", "rain": 0.4}] * 4)
        _, av2 = A.comparabilidad(mix)
        _ok("cambio de compuesto: lo detecta", any("compuesto CAMBIO" in a for a in av2), av2)
        _ok("cambio de estado de pista: lo detecta",
            any("estado de la pista CAMBIO" in a for a in av2), av2)

        # y lo importante: el CV de esa sesion NO puede salir con veredicto bueno
        c = A.consistency_struct(mix)
        _ok("sesion mezclada: no califica", c and not c["califica"], c and c["veredicto"])
        _ok("sesion mezclada: queda marcada en el DATO, no solo en el texto",
            c and c["mezclada"] is True, c and c.get("mezclada"))

        # ayudas cambiadas
        _, av3 = A.comparabilidad(_ses("TC__Auto__race__20260101_000000",
                                       [{}] * 4 + [{"tc_setting": 5}] * 4))
        _ok("cambio de TC: lo detecta", any("TC CAMBIO" in a for a in av3), av3)

        # -1 = no poblado, NO "apagado": confundirlos haria comparables a un alumno
        # con TC 8 y a uno sin TC
        _ok("ayuda en -1 se lee como desconocida, no como 0",
            A._cond({"tc_setting": -1})["tc"] is None, A._cond({"tc_setting": -1}))
        _ok("ayuda en 0 se lee como 0", A._cond({"tc_setting": 0})["tc"] == 0)

        # nombre de referencia: la variante es parte de la identidad de la pista
        n1 = A._ref_nombre({"car": "GT3", "track": "Silverstone", "track_variation": "GP"})
        n2 = A._ref_nombre({"car": "GT3", "track": "Silverstone", "track_variation": "National"})
        _ok("dos variantes NO comparten archivo de referencia", n1 != n2, (n1, n2))
        _ok("sin variante util no ensucia el nombre",
            A._ref_nombre({"car": "GT3", "track": "Monza", "track_variation": "Monza"})
            == "GT3__Monza")
    finally:
        shutil.rmtree(_bc, ignore_errors=True)

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

    print("\ntest punto de frenada (referencias):")

    def _traza(puntos_freno, largo=3000, paso=2.0, apex=1500.0):
        """Traza sintetica: velocidad con un minimo en `apex`, y freno pisado en los
        tramos [ini, fin] que se pidan."""
        n = int(largo / paso)
        dist = [i * paso for i in range(n)]
        spd = [200.0 - 120.0 * max(0.0, 1.0 - abs(d - apex) / 300.0) for d in dist]
        br = []
        for d in dist:
            br.append(0.9 if any(i <= d <= f for i, f in puntos_freno) else 0.0)
        return {"lap_dist": dist, "speed_kmh": spd, "brake": br}

    # frenada simple: empieza en 1200
    tz = _traza([(1200.0, 1480.0)])
    _ok("frenada simple: detecta el inicio", abs(A._punto_frenada(tz, 1500.0) - 1200.0) <= 2.0,
        A._punto_frenada(tz, 1500.0))

    # frenada MODULADA: afloja 10 m y vuelve. Debe seguir dando 1200, no 1300.
    tz2 = _traza([(1200.0, 1290.0), (1300.0, 1480.0)])
    _ok("frenada modulada: no se parte en el hueco chico",
        abs(A._punto_frenada(tz2, 1500.0) - 1200.0) <= 2.0, A._punto_frenada(tz2, 1500.0))

    # dos frenadas SEPARADAS (curva anterior a 600, esta a 1200): toma la de esta curva
    tz3 = _traza([(500.0, 620.0), (1200.0, 1480.0)])
    _ok("frenada encadenada: toma la de ESTA curva, no la anterior",
        abs(A._punto_frenada(tz3, 1500.0) - 1200.0) <= 2.0, A._punto_frenada(tz3, 1500.0))

    # curva de apoyo: sin freno -> None
    _ok("curva sin frenada: None", A._punto_frenada(_traza([]), 1500.0) is None)

    # distancia fuera de rango: el guard devuelve None en vez de un metro absurdo
    tz4 = _traza([(0.0, 1480.0)])          # freno pisado desde el metro 0
    r4 = A._punto_frenada(tz4, 1500.0, max_atras_m=400.0)
    _ok("frenada mas larga que el tope de busqueda: None, no un metro inventado",
        r4 is None or 1500.0 - r4 <= 400.0, r4)

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
    print("\ntest contactos (limpieza medida: el canal es ESTADO, no pulso):")
    test_contactos()
    print("\ndone.")


if __name__ == "__main__":
    main()
