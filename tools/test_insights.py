#!/usr/bin/env python3
"""Tests del motor de insights v1 (R2 peor sector, R1 deficit de vmin, R3 coasting).

Foco: que cada regla emita en su caso happy, y que los GUARDS anti-falso-positivo callen
(min 3 vueltas limpias, piso de ruido, warmup descartado, empate de sectores). Genera
sesiones sinteticas (trazas + summary + sectors.jsonl) en dirs temporales.
Correr: python tools/test_insights.py
"""
import gzip
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))
import ams2_telemetry as T
import analyze_telemetry as A
import test_analysis as TA   # reusa make_arrays (curvas T1@1000m, T2@2500m)


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


def _trace(folder, lap, ltime, corner_slow=0.0, coast_entry=False):
    dist, spd, thr, brk, tt, _ = TA.make_arrays(corner_slow=corner_slow)
    if coast_entry:                       # coasting inyectado en la entrada de T1 (apex 1000m)
        for k, dd in enumerate(dist):
            if 910 <= dd <= 980:
                thr[k] = 0.0
                brk[k] = 0.0
    idx = {h: i for i, h in enumerate(T.HEADER)}
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
    with gzip.open(os.path.join(folder, tname), "wt", encoding="utf-8") as f:
        f.write(",".join(T.HEADER) + "\n")
        f.write("\n".join(rows) + "\n")
    return tname


def build(specs):
    """specs: [{lap, time, sectors, sec_valid?, invalid?, corner_slow?, coast_entry?}] -> carpeta."""
    d = tempfile.mkdtemp(prefix="ins_")
    json.dump({"track": "T", "car": "C", "channels": T.HEADER},
              open(os.path.join(d, "session.json"), "w"))
    summ, secs = [], []
    for s in specs:
        tname = _trace(d, s["lap"], s["time"], s.get("corner_slow", 0.0), s.get("coast_entry", False))
        if not s.get("invalid"):
            summ.append({"lap": s["lap"], "lap_time": s["time"], "valid": True, "samples": 500,
                         "fuel_used": 2.5, "wear_delta": [0.02] * 4, "tyre_temp_avg": [88] * 4,
                         "sectors": s["sectors"], "compound": "Soft", "trace": tname})
        secs.append({"lap": s["lap"], "lap_time": s["time"], "sectors": s["sectors"],
                     "sec_valid": s.get("sec_valid", [True, True, True]), "invalid": s.get("invalid", False)})
    open(os.path.join(d, "summary.jsonl"), "w").write("\n".join(json.dumps(x) for x in summ))
    open(os.path.join(d, "sectors.jsonl"), "w").write("\n".join(json.dumps(x) for x in secs))
    return d


def _rules(ins, r):
    return [x for x in ins if x["regla"] == r]


def main():
    dirs = []

    print("R2 (peor sector, medido):")
    d = build([{"lap": 1, "time": 90.40, "sectors": [30.0, 30.40, 30.0]},
               {"lap": 2, "time": 90.00, "sectors": [30.0, 30.00, 30.0]},
               {"lap": 3, "time": 90.45, "sectors": [30.0, 30.45, 30.0]}]); dirs.append(d)
    _, ins, status = A.build_insights(d)
    r2 = _rules(ins, "R2")
    _ok("R2 emite", len(r2) == 1, [x["msg"] for x in ins])
    _ok("R2 nombra S2", bool(r2) and "S2" in r2[0]["msg"], r2[0]["msg"] if r2 else "")
    _ok("R2 procedencia medido", bool(r2) and r2[0]["proc"] == "medido")

    print("\nGuard: <3 vueltas limpias -> motor calla:")
    d = build([{"lap": 1, "time": 90.0, "sectors": [30.0, 30.0, 30.0]},
               {"lap": 2, "time": 90.2, "sectors": [30.0, 30.2, 30.0]}]); dirs.append(d)
    _, ins, status = A.build_insights(d)
    _ok("status insuficiente con 2 limpias", status == "insuficiente" and ins == [], status)

    print("\nGuard: piso de ruido (gap de sector < 0.30s) -> sin R2:")
    d = build([{"lap": 1, "time": 90.0, "sectors": [30.0, 30.00, 30.0]},
               {"lap": 2, "time": 90.1, "sectors": [30.0, 30.10, 30.0]},
               {"lap": 3, "time": 90.2, "sectors": [30.0, 30.20, 30.0]}]); dirs.append(d)
    _, ins, _ = A.build_insights(d)
    _ok("ruido < piso: sin R2", _rules(ins, "R2") == [], [x["msg"] for x in ins])

    print("\nGuard: vuelta calentando NO distorsiona el peor sector:")
    d = build([{"lap": 1, "time": 90.0, "sectors": [30.0, 30.0, 30.0]},
               {"lap": 2, "time": 90.1, "sectors": [30.0, 30.0, 30.0]},
               {"lap": 3, "time": 90.2, "sectors": [30.0, 30.0, 30.0]},
               {"lap": 9, "time": 100.0, "sectors": [30.0, 40.0, 30.0]}]); dirs.append(d)   # warmup
    _, ins, _ = A.build_insights(d)
    _ok("warmup (S2=40) excluida -> sin R2 falso", _rules(ins, "R2") == [], [x["msg"] for x in ins])

    print("\nGuard: empate de sectores (worst - 2nd < 0.15s) -> sin R2:")
    d = build([{"lap": 1, "time": 90.0, "sectors": [30.0, 30.40, 30.35]},
               {"lap": 2, "time": 90.1, "sectors": [30.0, 30.40, 30.35]},
               {"lap": 3, "time": 90.2, "sectors": [30.0, 30.40, 30.35]}]); dirs.append(d)
    _, ins, _ = A.build_insights(d)
    _ok("empate de sectores -> sin R2", _rules(ins, "R2") == [], [x["msg"] for x in ins])

    print("\nR1 (deficit de vmin en T1, estimado):")
    d = build([{"lap": 1, "time": 90.0, "sectors": [30.0, 30.0, 30.0], "corner_slow": 0.0},
               {"lap": 2, "time": 90.5, "sectors": [30.0, 30.0, 30.0], "corner_slow": 25.0},
               {"lap": 3, "time": 90.6, "sectors": [30.0, 30.0, 30.0], "corner_slow": 25.0}]); dirs.append(d)
    _, ins, _ = A.build_insights(d)
    r1 = _rules(ins, "R1")
    _ok("R1 emite por deficit repetido", len(r1) >= 1, [x["msg"] for x in ins])
    _ok("R1 menciona vmin", bool(r1) and "vmin" in r1[0]["msg"], r1[0]["msg"] if r1 else "")

    print("\nGuard: deficit en 1 sola vuelta (no repetido) -> sin R1 (posible trafico):")
    d = build([{"lap": 1, "time": 90.0, "sectors": [30.0, 30.0, 30.0], "corner_slow": 0.0},
               {"lap": 2, "time": 90.5, "sectors": [30.0, 30.0, 30.0], "corner_slow": 25.0},
               {"lap": 3, "time": 90.1, "sectors": [30.0, 30.0, 30.0], "corner_slow": 0.0}]); dirs.append(d)
    _, ins, _ = A.build_insights(d)
    _ok("deficit 1 vuelta -> sin R1", _rules(ins, "R1") == [], [x["msg"] for x in ins])

    print("\nR3 (coasting en la entrada, metros):")
    d = build([{"lap": 1, "time": 90.0, "sectors": [30.0, 30.0, 30.0], "coast_entry": True},
               {"lap": 2, "time": 90.1, "sectors": [30.0, 30.0, 30.0], "coast_entry": True},
               {"lap": 3, "time": 90.2, "sectors": [30.0, 30.0, 30.0], "coast_entry": True}]); dirs.append(d)
    _, ins, _ = A.build_insights(d)
    r3 = _rules(ins, "R3")
    _ok("R3 emite por coasting repetido", len(r3) >= 1, [x["msg"] for x in ins])
    _ok("R3 da accion de frenada", bool(r3) and "Frena" in r3[0]["msg"], r3[0]["msg"] if r3 else "")

    print("\nDeterminismo / no-crash:")
    try:
        A.report_insights(dirs[0])
        # sesion sin sectors.jsonl (fallback al summary)
        d2 = build([{"lap": 1, "time": 90.0, "sectors": [30.0, 30.4, 30.0]},
                    {"lap": 2, "time": 90.1, "sectors": [30.0, 30.0, 30.0]},
                    {"lap": 3, "time": 90.2, "sectors": [30.0, 30.45, 30.0]}]); dirs.append(d2)
        os.remove(os.path.join(d2, "sectors.jsonl"))
        A.build_insights(d2)
        _ok("report_insights + fallback sin sectors.jsonl corren", True)
    except Exception as e:
        _ok("report_insights + fallback corren", False, repr(e))

    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    print("\ndone.")


if __name__ == "__main__":
    main()
