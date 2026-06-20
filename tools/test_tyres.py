#!/usr/bin/env python3
"""Tests del analisis de gomas beta: presion TERMICA (centro vs bordes) + camber por rueda.

Construye una sesion con canales de goma controlados por rueda y verifica que tyres_struct
calcula dCenter/dEdge/psi (Bar*100 -> psi) y que _press_verdict da el veredicto correcto
(sobre/sub/OK presion). Correr: python tools/test_tyres.py
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


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


def build(tyres, n=300, lap=1, ltime=90.0):
    """tyres: {corner: {in,mid,out,bulk,press}} (press en Bar*100). 1 vuelta limpia."""
    d = tempfile.mkdtemp(prefix="tyr_")
    json.dump({"track": "T", "car": "C", "channels": T.HEADER},
              open(os.path.join(d, "session.json"), "w"))
    idx = {h: i for i, h in enumerate(T.HEADER)}
    rows = []
    for k in range(n):
        r = [0.0] * len(T.HEADER)
        r[idx["t"]] = round(k * 0.3, 3)
        r[idx["lap_dist"]] = round(k * 20, 2)
        r[idx["speed_kmh"]] = 150.0
        for c, v in tyres.items():
            r[idx[f"tyre_t_in_{c}"]] = v["in"]
            r[idx[f"tyre_t_mid_{c}"]] = v["mid"]
            r[idx[f"tyre_t_out_{c}"]] = v["out"]
            r[idx[f"tyre_temp_{c}"]] = v["bulk"]
            r[idx[f"tyre_press_{c}"]] = v["press"]
        rows.append(",".join(map(str, r)))
    tname = f"L{lap:03d}_{ltime:.3f}s.csv.gz"
    with gzip.open(os.path.join(d, tname), "wt", encoding="utf-8") as f:
        f.write(",".join(T.HEADER) + "\n")
        f.write("\n".join(rows) + "\n")
    open(os.path.join(d, "summary.jsonl"), "w").write(json.dumps(
        {"lap": lap, "lap_time": ltime, "valid": True, "samples": n,
         "sectors": [30, 30, 30], "trace": tname}))
    return d


def main():
    dirs = []
    # FL sobrepresion (centro +7, psi ~28.3); RR subpresion (bordes, centro -7, psi ~25.8); RL OK
    w = {"FL": {"in": 78, "mid": 85, "out": 78, "bulk": 84, "press": 195},
         "FR": {"in": 70, "mid": 71, "out": 72, "bulk": 71, "press": 183},
         "RL": {"in": 80, "mid": 80, "out": 80, "bulk": 80, "press": 185},
         "RR": {"in": 80, "mid": 73, "out": 80, "bulk": 78, "press": 178}}
    d = build(w); dirs.append(d)
    ts = A.tyres_struct(d)
    print("tyres_struct:")
    _ok("4 ruedas", bool(ts) and len(ts["wheels"]) == 4, list(ts["wheels"]) if ts else None)
    fl, rr, rl = ts["wheels"]["FL"], ts["wheels"]["RR"], ts["wheels"]["RL"]
    _ok("FL dCenter ~+7 (centro caliente)", abs(fl["dCenter"] - 7.0) < 0.5, fl["dCenter"])
    _ok("FL psi decodifica Bar*100 (~28)", fl["psi"] is not None and 27 < fl["psi"] < 29, fl["psi"])
    _ok("FL dEdge 0 (in=out)", abs(fl["dEdge"]) < 0.5, fl["dEdge"])
    _ok("RR dCenter ~-7 (bordes calientes)", abs(rr["dCenter"] + 7.0) < 0.5, rr["dCenter"])

    print("\n_press_verdict:")
    th_fl, win_fl = A._press_verdict(fl)
    _ok("FL -> SOBREpresion (termica)", "SOBRE" in th_fl, th_fl)
    _ok("FL -> psi sobre ventana", "sobre" in win_fl, win_fl)
    th_rr, win_rr = A._press_verdict(rr)
    _ok("RR -> SUBpresion (termica)", "SUB" in th_rr, th_rr)
    _ok("RL (dCenter 0) -> OK", "OK" in A._press_verdict(rl)[0], A._press_verdict(rl)[0])

    print("\nreport_tyres no-crash:")
    try:
        A.report_tyres(d)
        _ok("report_tyres corre", True)
    except Exception as e:
        _ok("report_tyres corre", False, repr(e))

    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    print("\ndone.")


if __name__ == "__main__":
    main()
