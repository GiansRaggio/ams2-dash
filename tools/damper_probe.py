#!/usr/bin/env python3
"""Caracteriza mSuspensionVelocity de AMS2 antes de fijar el histograma de dampers.

Muestrea las 4 esquinas a la maxima tasa posible (cuando avanza mSequenceNumber)
mientras manejas, y reporta unidades/rango/signo/tasa real. Con eso elegimos bins
y umbrales low/high-speed correctos (en vez de adivinar si es m/s o mm/s).

Uso:  .venv\\Scripts\\python.exe tools\\damper_probe.py
Espera a que entres a pista y te muevas, luego captura ~12 s. Maneja curvas y,
si puedes, pisa un piano/borde para ver el contenido de alta velocidad.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import ams2_shm  # noqa: E402

CORNERS = ["FL", "FR", "RL", "RR"]
CAPTURE_S = 12.0
WAIT_S = 180.0
SPEED_GO = 20.0  # km/h para considerar "en movimiento"


def main():
    r = ams2_shm.Reader().open()
    print("[probe] esperando a que entres a pista y te muevas (>20 km/h)...")
    t0 = time.perf_counter()
    while True:
        d = r.snapshot()
        if d.mGameState in (2, 3, 4) and d.mSpeed * 3.6 > SPEED_GO:
            break
        if time.perf_counter() - t0 > WAIT_S:
            print("[probe] no detecte movimiento; abortando. Entra a pista y reintenta.")
            r.close()
            return
        time.sleep(0.1)

    print(f"[probe] EN MARCHA: capturando {CAPTURE_S:.0f} s. SIGUE MANEJANDO (curvas + un piano).")
    vel = [[] for _ in range(4)]
    trav = [[] for _ in range(4)]
    last_seq = -1
    frames = 0
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < CAPTURE_S:
        d = r.snapshot()
        if d.mSequenceNumber == last_seq:
            time.sleep(0.0003)
            continue
        last_seq = d.mSequenceNumber
        frames += 1
        for i in range(4):
            vel[i].append(d.mSuspensionVelocity[i])
            trav[i].append(d.mSuspensionTravel[i])
    r.close()
    dt = time.perf_counter() - t0

    print(f"\n[probe] {frames} frames unicos en {dt:.1f}s  ->  ~{frames / dt:.0f} Hz efectivos\n")

    def pct(sorted_a, p):
        n = len(sorted_a)
        return sorted_a[min(n - 1, int(p / 100 * n))]

    print(f"{'esq':4} {'min':>9} {'max':>9} {'mean':>9} {'|p50|':>9} {'|p90|':>9} {'|p99|':>9}")
    for i in range(4):
        a = vel[i]
        if not a:
            continue
        s = sorted(a)
        absa = sorted(abs(x) for x in a)
        print(f"{CORNERS[i]:4} {s[0]:9.4f} {s[-1]:9.4f} {sum(a)/len(a):9.4f} "
              f"{pct(absa,50):9.4f} {pct(absa,90):9.4f} {pct(absa,99):9.4f}")

    print("\nRango de mSuspensionTravel (referencia de unidades):")
    for i in range(4):
        t = trav[i]
        if t:
            ts = sorted(t)
            print(f"  {CORNERS[i]}: min={ts[0]:.5f}  max={ts[-1]:.5f}")

    # Pista sobre unidades
    mx = max((max(abs(x) for x in vel[i]) for i in range(4) if vel[i]), default=0)
    if mx < 5:
        print(f"\n[probe] max |vel| ~ {mx:.3f}  ->  probablemente m/s (multiplicar x1000 para mm/s)")
    else:
        print(f"\n[probe] max |vel| ~ {mx:.1f}  ->  probablemente ya en mm/s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[probe] cancelado")
