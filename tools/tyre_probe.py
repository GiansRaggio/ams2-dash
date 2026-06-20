#!/usr/bin/env python3
"""Verifica los canales de temperatura/presion de goma de AMS2 antes de diagnosticar setup.

Analogo a damper_probe.py, pero para GOMAS. El diseno de un diagnostico de camber/
presion (spread inner/center/outer por neumatico) asume que estos canales se pueblan
de verdad en AMS2 -- y eso NO esta verificado. Hay un caveat fuerte conocido
(SimHub #632): en AMS2 inner/outer pueden venir CRUZADOS y el center leer temp de
SUPERFICIE (no bulk). Antes de construir nada encima, hay que medirlo en pista.

Muestrea ~15 s manejando con gomas EN TEMPERATURA (corre 2-3 vueltas antes) y reporta,
por esquina, mTyreTempLeft/Center/Right + mTyreTemp (bulk) + mTyreCarcassTemp + mAirPressure,
y responde empiricamente:
  1) Se pueblan L/C/R? (no en 0, no identicos entre si)
  2) El center cae ENTRE los bordes? (min(L,R) <= C <= max(L,R)) -> en que % de muestras
  3) En que UNIDAD/rango cae mAirPressure (~24 psi vs ~250 = Bar x100)? -> factor a fijar
  4) mTyreCarcassTemp parece Kelvin (-273.15 da un C plausible)?

Corre su propio Reader, en paralelo al bridge (lectura read-only, no molesta).
Uso:  .venv\\Scripts\\python.exe tools\\tyre_probe.py
Solo Windows.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ams2_shm  # noqa: E402

CORNERS = ["FL", "FR", "RL", "RR"]
CAPTURE_S = 15.0
WAIT_S = 180.0
SPEED_GO = 20.0   # km/h para considerar "en movimiento"
_LIVE = (ams2_shm.GAME_INGAME_PLAYING, ams2_shm.GAME_INGAME_INMENU_TIME_TICKING)


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def _median(a):
    if not a:
        return 0.0
    s = sorted(a)
    n = len(s)
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def main():
    r = ams2_shm.Reader().open()
    print("[probe] esperando a que entres a pista y te muevas (>20 km/h)...")
    print("[probe] IDEAL: corre 2-3 vueltas antes para tener la goma EN TEMPERATURA.")
    t0 = time.perf_counter()
    while True:
        d = r.snapshot()
        if d.mGameState in _LIVE and d.mSpeed * 3.6 > SPEED_GO:
            break
        if time.perf_counter() - t0 > WAIT_S:
            print("[probe] no detecte movimiento; abortando. Entra a pista y reintenta.")
            r.close()
            return
        time.sleep(0.1)

    print(f"[probe] EN MARCHA: capturando {CAPTURE_S:.0f} s. SIGUE MANEJANDO (curvas, carga lateral).")
    # por esquina: listas de left/center/right/bulk/carcass/press
    L = [[] for _ in range(4)]
    C = [[] for _ in range(4)]
    R = [[] for _ in range(4)]
    B = [[] for _ in range(4)]
    K = [[] for _ in range(4)]
    P = [[] for _ in range(4)]
    last_seq = -1
    frames = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < CAPTURE_S:
        d = r.snapshot()
        if d.mSequenceNumber == last_seq or d.mSpeed * 3.6 < 5:
            time.sleep(0.0005)
            continue
        last_seq = d.mSequenceNumber
        frames += 1
        for i in range(4):
            L[i].append(d.mTyreTempLeft[i])
            C[i].append(d.mTyreTempCenter[i])
            R[i].append(d.mTyreTempRight[i])
            B[i].append(d.mTyreTemp[i])
            K[i].append(d.mTyreCarcassTemp[i])
            P[i].append(d.mAirPressure[i])
    r.close()
    dt = time.perf_counter() - t0
    print(f"\n[probe] {frames} frames en {dt:.1f}s  ->  ~{frames / dt:.0f} Hz\n")
    if frames < 50:
        print("[probe] muy pocas muestras; reintenta manejando mas rato.")
        return

    # --- (1) y (2): se pueblan L/C/R y el center cae entre bordes? ---
    print(f"{'esq':4} {'L(in?)':>8} {'C(mid)':>8} {'R(out?)':>8} {'bulk':>8} {'carc.K':>8} {'carc.C':>8} {'press':>8}")
    populated = True
    between_pct = []
    for i in range(4):
        l, c, rr = _mean(L[i]), _mean(C[i]), _mean(R[i])
        b, k, p = _mean(B[i]), _mean(K[i]), _median(P[i])
        print(f"{CORNERS[i]:4} {l:8.1f} {c:8.1f} {rr:8.1f} {b:8.1f} {k:8.1f} {k-273.15:8.1f} {p:8.1f}")
        # poblado si los 3 no son ~0 y no son los 3 identicos
        spread = max(l, c, rr) - min(l, c, rr)
        if (abs(l) + abs(c) + abs(rr)) < 1.0 or spread < 0.05:
            populated = False
        btw = sum(1 for j in range(len(C[i]))
                  if min(L[i][j], R[i][j]) - 0.1 <= C[i][j] <= max(L[i][j], R[i][j]) + 0.1)
        between_pct.append(100.0 * btw / max(1, len(C[i])))

    print("\n--- VEREDICTO ---")
    print(f"(1) L/C/R poblados y distintos: {'SI' if populated else 'NO (0 o identicos -> spread INUTIL)'}")
    print(f"(2) center entre bordes (% muestras): " +
          " · ".join(f"{CORNERS[i]} {between_pct[i]:.0f}%" for i in range(4)))
    if min(between_pct) < 70:
        print("    OJO: en alguna esquina el center NO cae entre bordes consistentemente")
        print("    -> coherente con SimHub #632 (inner/outer cruzados / center surface-temp).")

    # --- (3): unidad de mAirPressure (resolver UNA vez con la mediana global) ---
    allp = [v for i in range(4) for v in P[i]]
    medp = _median(allp)
    if 150 <= medp <= 350:
        unit = f"Bar x100  ->  {medp/100:.2f} bar  ->  {medp/100*14.5038:.1f} psi"
    elif 18 <= medp <= 40:
        unit = f"psi plausible directo (~{medp:.1f} psi)"
    elif 1.0 <= medp <= 3.5:
        unit = f"bar directo  ->  {medp*14.5038:.1f} psi"
    else:
        unit = "AMBIGUO / fuera de rango conocido -> NO usar el numero crudo"
    print(f"(3) mAirPressure mediana cruda = {medp:.1f}  ->  {unit}")

    # --- (4): carcass temp en Kelvin? ---
    kmean = _mean([v for i in range(4) for v in K[i]])
    plausible = 40 <= (kmean - 273.15) <= 130
    print(f"(4) mTyreCarcassTemp media cruda = {kmean:.1f}  ->  -273.15 = {kmean-273.15:.1f} C "
          f"({'Kelvin plausible' if plausible else 'NO parece Kelvin/poblado'})")

    print("\n[probe] Pega esta salida en el chat: define si el diagnostico de camber/presion")
    print("        es construible en AMS2 o queda como 'beta no accionable'.")


if __name__ == "__main__":
    try:
        main()
    except ams2_shm.SharedMemoryUnavailable as e:
        print(f"[probe] {e}")
    except KeyboardInterrupt:
        print("\n[probe] cancelado")
