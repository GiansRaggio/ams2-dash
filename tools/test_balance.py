#!/usr/bin/env python3
"""Tests de R6: balance sobre/subviraje por curva + momento de inestabilidad trasera.

Construye trazas sinteticas con slip por rueda controlado (trasero vs delantero) y un 'spike'
de slip trasero en una vuelta, y verifica balance_struct + el momento. Correr: python tools/test_balance.py
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


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


def _trace(folder, lap, ltime, rear=4.0, front=4.0, spike=None):
    """spike: (dist_lo, dist_hi, rear_val) inyecta un slide trasero ahi. Curva (dip) en 1000m."""
    idx = {h: i for i, h in enumerate(T.HEADER)}
    n, t, rows = 400, 0.0, []
    for k in range(n):
        dist = k * 15.0
        spd = max(60.0, 200.0 - 120.0 * math.exp(-((dist - 1000) / 160.0) ** 2))
        r = [0.0] * len(T.HEADER)
        r[idx["t"]] = round(t, 3)
        r[idx["lap_dist"]] = round(dist, 2)
        r[idx["speed_kmh"]] = round(spd, 2)
        rv = spike[2] if (spike and spike[0] <= dist <= spike[1]) else rear
        for c in ("RL", "RR"):
            r[idx[f"tyre_slip_{c}"]] = rv
        for c in ("FL", "FR"):
            r[idx[f"tyre_slip_{c}"]] = front
        rows.append(",".join(map(str, r)))
        t += 15.0 / (spd / 3.6)
    tname = f"L{lap:03d}_{ltime:.3f}s.csv.gz"
    with gzip.open(os.path.join(folder, tname), "wt", encoding="utf-8") as f:
        f.write(",".join(T.HEADER) + "\n")
        f.write("\n".join(rows) + "\n")
    return tname, t


def build(specs):
    d = tempfile.mkdtemp(prefix="bal_")
    json.dump({"track": "T", "car": "C", "channels": T.HEADER}, open(os.path.join(d, "session.json"), "w"))
    summ = []
    for s in specs:
        tname, ltime = _trace(d, s["lap"], s["time"], s.get("rear", 4.0), s.get("front", 4.0), s.get("spike"))
        summ.append({"lap": s["lap"], "lap_time": s["time"], "valid": True, "samples": 400,
                     "sectors": [ltime / 3, ltime / 3, ltime / 3], "trace": tname})
    open(os.path.join(d, "summary.jsonl"), "w").write("\n".join(json.dumps(x) for x in summ))
    return d


def main():
    dirs = []
    print("balance_struct: sobreviraje en la curva (rear > front):")
    d = build([{"lap": 1, "time": 90.0, "rear": 6.0, "front": 4.0},
               {"lap": 2, "time": 90.2, "rear": 6.0, "front": 4.0},
               {"lap": 3, "time": 90.4, "rear": 6.0, "front": 4.0}]); dirs.append(d)
    bal = A.balance_struct(d)
    _ok("detecta curva(s)", bool(bal) and len(bal["corners"]) >= 1, [c["n"] for c in bal["corners"]] if bal else None)
    _ok("curva sobreviraje (R/F ~1.5)", any(c["bal"] == "sobreviraje" for c in bal["corners"]),
        [(c["n"], c["ratio"]) for c in bal["corners"]])

    print("\nsubviraje (front > rear):")
    bal2 = A.balance_struct(build([{"lap": 1, "time": 90.0, "rear": 3.0, "front": 5.0},
                                   {"lap": 2, "time": 90.2, "rear": 3.0, "front": 5.0},
                                   {"lap": 3, "time": 90.4, "rear": 3.0, "front": 5.0}]))
    _ok("curva subviraje (R/F ~0.6)", any(c["bal"] == "subviraje" for c in bal2["corners"]),
        [(c["n"], c["ratio"]) for c in bal2["corners"]])

    print("\nmomento: una vuelta con spike de slip trasero:")
    d3 = build([{"lap": 1, "time": 90.0, "rear": 5.0, "front": 5.0},
                {"lap": 2, "time": 90.5, "rear": 5.0, "front": 5.0, "spike": (3000, 3300, 20.0)},
                {"lap": 3, "time": 90.4, "rear": 5.0, "front": 5.0}]); dirs.append(d3)
    bal3 = A.balance_struct(d3)
    _ok("detecta MOMENTO de inestabilidad", bool(bal3) and bal3["moment"] is not None,
        bal3["moment"] if bal3 else None)

    print("\nsin spike -> sin momento:")
    bal4 = A.balance_struct(build([{"lap": 1, "time": 90.0, "rear": 5.0, "front": 5.0},
                                   {"lap": 2, "time": 90.2, "rear": 5.0, "front": 5.0},
                                   {"lap": 3, "time": 90.4, "rear": 5.0, "front": 5.0}]))
    _ok("slip parejo -> momento None", bal4 is not None and bal4["moment"] is None,
        bal4["moment"] if bal4 else None)

    print("\nguard: <3 vueltas -> None:")
    bal5 = A.balance_struct(build([{"lap": 1, "time": 90.0}, {"lap": 2, "time": 90.2}]))
    _ok("<3 limpias -> None", bal5 is None)

    print("\nreport_balance no-crash:")
    try:
        A.report_balance(d3)
        _ok("report_balance corre", True)
    except Exception as e:
        _ok("report_balance corre", False, repr(e))

    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    print("\ndone.")


if __name__ == "__main__":
    main()
