#!/usr/bin/env python3
"""Backtest de fiabilidad de la estrategia contra la telemetria REAL grabada.

Es el motor de "evaluar" del loop de mejora: toma una sesion grabada
(telemetry/<...>/summary.jsonl) y mide que tan bien habrian predicho los modelos
del director de estrategia lo que de verdad paso, vuelta a vuelta:

  * combustible: prediccion (media movil de las ultimas W vueltas) vs consumo real
    de la vuelta siguiente -> MAE y % de error. Mide la fiabilidad del numero #1.
  * lap-time: idem, base del calculo de vueltas-restantes en carrera por tiempo.
  * desgaste: linealidad de mTyreWear (R^2 de la recta) -> si el modelo lineal sirve.
  * margen: cuanto se desvio la peor vuelta respecto del promedio -> sugiere si el
    SAFETY_LAPS actual (1.5 en timed) cubre la varianza observada.

No depende del juego: corre sobre los .jsonl ya guardados. Solo stdlib.
    python tools/eval_strategy.py            # ultima sesion
    python tools/eval_strategy.py <carpeta>
"""
import glob
import json
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEM = os.path.join(HERE, "telemetry")
W = 5                      # ventana de media movil (igual que GREEN_KEEP del engine)
SAFETY_LAPS_TIMED = 1.5    # el que usa el engine; se evalua si alcanza


def _load(folder):
    laps = []
    f = os.path.join(folder, "summary.jsonl")
    if os.path.exists(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line:
                laps.append(json.loads(line))
    return laps


def _backtest(series):
    """Predice cada valor como media de los W previos; devuelve MAE y MAPE."""
    errs, perrs = [], []
    for i in range(2, len(series)):
        window = series[max(0, i - W):i]
        pred = sum(window) / len(window)
        act = series[i]
        errs.append(abs(pred - act))
        if act:
            perrs.append(abs(pred - act) / abs(act) * 100)
    mae = st.mean(errs) if errs else 0.0
    mape = st.mean(perrs) if perrs else 0.0
    return mae, mape, (max(errs) if errs else 0.0)


def _r2_linear(ys):
    """R^2 de un ajuste lineal simple (linealidad de la degradacion)."""
    n = len(ys)
    if n < 3:
        return None
    xs = list(range(n))
    mx, my = (n - 1) / 2.0, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return None
    b = sxy / sxx
    a = my - b * mx
    ss_res = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - my) ** 2 for y in ys)
    return 1 - ss_res / ss_tot if ss_tot else 1.0


def evaluate(folder):
    laps = _load(folder)
    print(f"\n=== EVAL fiabilidad · {os.path.basename(folder)} ===")
    valid = [l for l in laps if l.get("lap_time")]
    if len(valid) < 3:
        print(f"  faltan vueltas (hay {len(valid)}); maneja >=4 vueltas validas para evaluar.")
        return
    fuels = [l["fuel_used"] for l in valid if l.get("fuel_used") is not None]
    times = [l["lap_time"] for l in valid]
    degs = [max(l["wear_delta"]) for l in valid if l.get("wear_delta")]

    score = []   # (eje, nota 0-100, detalle)

    # --- combustible ---
    if len(fuels) >= 3:
        mae, mape, worst = _backtest(fuels)
        var = st.pstdev(fuels)
        # margen: peor desvio (vueltas) vs el SAFETY_LAPS configurado
        mean_f = st.mean(fuels)
        worst_over = max(fuels) - mean_f
        laps_over = worst_over / mean_f if mean_f else 0
        ok_margin = laps_over <= SAFETY_LAPS_TIMED
        nota = max(0, 100 - mape * 8)
        score.append(("combustible", nota,
                      f"MAE {mae:.2f}L · error {mape:.1f}% · sigma {var:.2f}L · "
                      f"peor vuelta +{laps_over:.2f}v {'(margen 1.5v OK)' if ok_margin else '(margen 1.5v CORTO!)'}"))

    # --- lap-time ---
    if len(times) >= 3:
        mae, mape, worst = _backtest(times)
        nota = max(0, 100 - mape * 12)
        score.append(("lap-time", nota,
                      f"MAE {mae:.2f}s · error {mape:.1f}% · sigma {st.pstdev(times):.2f}s"))

    # --- degradacion ---
    if len(degs) >= 3 and max(degs) > 1e-5:
        cum = []
        acc = 0.0
        for d in degs:
            acc += d
            cum.append(acc)
        r2 = _r2_linear(cum)
        nota = (r2 * 100) if r2 is not None else 50
        score.append(("desgaste", nota,
                      f"R^2 lineal {r2:.3f} · {st.mean(degs)*100:.2f}%/v "
                      f"{'(modelo lineal fiable)' if (r2 or 0) > 0.9 else '(no lineal: el modelo simple miente)'}"))
    elif degs:
        score.append(("desgaste", None, "mTyreWear plano (limitacion del dato en este auto)"))

    print(f"\n  {'EJE':>12}  {'NOTA':>5}  DETALLE")
    for eje, nota, det in score:
        ntxt = f"{nota:>4.0f}" if nota is not None else "  --"
        print(f"  {eje:>12}  {ntxt}  {det}")

    nums = [n for _, n, _ in score if n is not None]
    if nums:
        g = st.mean(nums)
        tag = "alta" if g >= 80 else "media" if g >= 60 else "baja"
        print(f"\n  FIABILIDAD GLOBAL: {g:.0f}/100 ({tag})")

    # sugerencias accionables para la proxima iteracion del loop
    tips = []
    if len(fuels) >= 3:
        _, mape, _ = _backtest(fuels)
        if mape > 6:
            tips.append("consumo ruidoso (>6%): subir el margen o excluir vueltas con trafico del promedio")
    if degs and max(degs) > 1e-5:
        r2 = _r2_linear([sum(degs[:i+1]) for i in range(len(degs))])
        if r2 is not None and r2 < 0.9:
            tips.append("desgaste no lineal: el horizonte de gomas lineal sub/sobre-estima; usar curva por fase")
    if tips:
        print("\n  sugerencias para el loop:")
        for t in tips:
            print(f"   - {t}")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg and os.path.isdir(arg):
        evaluate(arg)
        return
    subs = sorted([d for d in glob.glob(os.path.join(TELEM, "*")) if os.path.isdir(d)],
                  key=os.path.getmtime)
    if not subs:
        print(f"No hay sesiones en {TELEM}. Graba con el dash y volve.")
        return
    evaluate(subs[-1])


if __name__ == "__main__":
    main()
