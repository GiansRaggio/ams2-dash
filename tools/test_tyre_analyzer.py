#!/usr/bin/env python3
"""Tests de ams2_tyres.TyreAnalyzer (el que alimenta la pagina GOMAS).

Existia con cobertura CERO: tools/test_tyres.py testea el analizador OFFLINE
(analyze_telemetry), no este modulo. Lo cazo una revision adversarial.

Cubre las propiedades que hacen HONESTO al instrumento -- las que, si se rompen,
vuelven a producir la clase de bug que costo dos redisenos:
  * rel es suma cero (no puede pintar las 4 ruedas del mismo lado)
  * los centinelas no generan veredicto
  * NaN/faltantes no envenenan el broadcast (un NaN en el JSON tumba el dash entero)
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ams2_tyres  # noqa: E402

NOWHERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sin_estado_")
_fails = []


def _ok(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        _fails.append(name)


class Snap:
    """Snapshot minimo con lo que lee TyreAnalyzer.update()."""

    def __init__(self, edges=62.0, carc=(83, 103, 56, 70), bulk=80.0, press=200.0, speed=0.0):
        self.mCarName = b"Test Car"
        self.mTyreCompound = [b"Liso"] * 4
        self.mSpeed = speed
        self.mAirPressure = [press] * 4
        self.mTyreCarcassTemp = [t + 273.15 for t in carc]     # KELVIN, como la SHM
        self.mTyreLayerTemp = [t + 273.15 for t in (49, 53, 43, 47)]
        self.mTyreTemp = [bulk] * 4                            # bulk ya viene en Celsius
        self.mBrakeTempCelsius = [189, 195, 77, 77]
        self.mTyreTempLeft = [edges] * 4
        self.mTyreTempRight = [edges] * 4


def _run(snap, n=40):
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    for _ in range(n):
        a.update(snap)
    return a.payload()


def test_centinela_de_bordes():
    """Bordes en 0.0 EXACTO = el auto no esta en pista, no una medicion.

    Regresion real: con el auto en garage los bordes leen 0.0 y la pagina dictaba
    'poco camber neg.' a partir de un spread de 0.0. Un neumatico real nunca marca
    0 C en el borde ni frio (el ambiente anda en 15-30)."""
    c = _run(Snap(edges=0.0))["corners"][0]
    _ok("bordes en 0 -> sin t_in/t_out", c["t_in"] is None and c["t_out"] is None)
    _ok("bordes en 0 -> sin spread", c["spread"] is None)
    _ok("bordes en 0 -> SIN veredicto de camber", c["camber"] is None, repr(c["camber"]))
    # pero un spread de 0 con bordes REALES si es medicion: ahi si se opina
    c2 = _run(Snap(edges=62.0))["corners"][0]
    _ok("bordes reales -> si hay veredicto", c2["camber"] is not None, repr(c2["camber"]))


def test_rel_es_suma_cero():
    """rel = carcasa - media(4). La propiedad que impide el modo de falla de la v3
    (todo rojo siempre): si suma cero, alguna esquina esta siempre bajo la media."""
    cs = _run(Snap(carc=(120, 118, 122, 119)))["corners"]
    rels = [c["rel"] for c in cs]
    _ok("rel presente en las 4", all(r is not None for r in rels), rels)
    if all(r is not None for r in rels):
        _ok("rel suma ~0", abs(sum(rels)) < 0.2, f"suma={sum(rels):.3f}")
        _ok("no todas del mismo lado", not (all(r > 0 for r in rels) or all(r < 0 for r in rels)))
    # carcasas ALTAS pero parejas: rel ~0 en todas -> el instrumento NO grita
    cs2 = _run(Snap(carc=(140, 140, 140, 140)))["corners"]
    _ok("carcasa alta pero pareja -> rel ~0 (no alarma por caliente)",
        all(abs(c["rel"]) < 0.5 for c in cs2 if c["rel"] is not None),
        [c["rel"] for c in cs2])


def test_nan_no_envenena():
    """Un NaN en el payload = json.dumps escribe 'NaN' = JSON invalido = el JSON.parse
    del navegador tira y el dash COMPLETO deja de actualizarse, no solo gomas."""
    import json
    s = Snap()
    s.mTyreCarcassTemp = [float("nan")] * 4
    s.mAirPressure = [float("inf")] * 4
    p = _run(s)
    try:
        json.dumps(p, allow_nan=False)
        _ok("payload con NaN/inf serializa a JSON estricto", True)
    except ValueError as e:
        _ok("payload con NaN/inf serializa a JSON estricto", False, str(e))


def test_campo_faltante():
    """Una version del juego (o un mock) sin un canal no debe tumbar el analizador:
    el bridge atrapa la excepcion pero se queda con el frame anterior en silencio."""
    s = Snap()
    del s.mTyreLayerTemp
    try:
        _run(s)
        _ok("canal faltante no revienta", True)
    except AttributeError:
        _ok("canal faltante no revienta", False, "AttributeError")





def test_no_opina_detenido():
    """Con el auto parado el patron termico deja de venir de la conduccion: se aplana
    solo y el rel se desploma contra su norma. Medido en vivo: 8 de 19 alarmas de una
    sesion de 40 min salieron bajo 15 km/h (boxes y grilla)."""
    # OJO con la semantica: tdev dispara con un CAMBIO contra la norma propia, no con
    # una asimetria estable (una constante la absorbe la EMA lenta y deja de ser noticia).
    # Asi que primero se establece la norma parejo, y RECIEN AHI se cocina una esquina.
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    for _ in range(400):                       # norma: las 4 parejas
        t += 0.5
        a.update(Snap(carc=(120, 120, 120, 120), speed=50.0), now=t)
    for _ in range(60):                        # rodando, la FR se cocina
        t += 0.5
        a.update(Snap(carc=(120, 150, 118, 119), speed=50.0), now=t)
    parado = a.payload()
    for _ in range(60):                        # mismo desbalance, pero DETENIDO
        t += 0.5
        a.update(Snap(carc=(120, 150, 118, 119), speed=0.0), now=t)
    p = a.payload()
    _ok("detenido -> sin alarma", all(c["tdev"] is None for c in p["corners"]),
        [c["tdev"] for c in p["corners"]])
    _ok("pero rodando si opinaba", any(c["tdev"] for c in parado["corners"]),
        [c["tdev"] for c in parado["corners"]])


def test_salto_de_sesion_no_alarma():
    """Las 4 carcasas saltando juntas = cambio de sesion o de gomas. Medido en vivo:
    125 -> 58 C en un frame al rotar de sesion, y la alarma sonaba en las 4 esquinas."""
    a = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    for _ in range(400):
        t += 0.5
        a.update(Snap(carc=(125, 129, 105, 106), speed=50.0), now=t)
    for _ in range(10):                        # gomas nuevas: las 4 se desploman juntas
        t += 0.5
        a.update(Snap(carc=(58, 51, 46, 53), speed=50.0), now=t)
    p = a.payload()
    _ok("salto simultaneo -> sin alarma", all(c["tdev"] is None for c in p["corners"]),
        [c["tdev"] for c in p["corners"]])
    # pero UNA sola esquina moviendose sigue siendo un evento de verdad
    b = ams2_tyres.TyreAnalyzer(base_dir=NOWHERE)
    t = 0.0
    for _ in range(400):
        t += 0.5
        b.update(Snap(carc=(120, 120, 120, 120), speed=50.0), now=t)
    for _ in range(60):
        t += 0.5
        b.update(Snap(carc=(120, 120, 145, 120), speed=50.0), now=t)
    p2 = b.payload()
    _ok("una sola esquina -> SI alarma", p2["corners"][2]["tdev"] == "hot",
        [c["tdev"] for c in p2["corners"]])


if __name__ == "__main__":
    print("== ams2_tyres.TyreAnalyzer ==")
    test_centinela_de_bordes()
    test_rel_es_suma_cero()
    test_nan_no_envenena()
    test_campo_faltante()
    test_no_opina_detenido()
    test_salto_de_sesion_no_alarma()
    print(f"\n{'todo verde' if not _fails else 'FALLAS: ' + ', '.join(_fails)}")
    sys.exit(1 if _fails else 0)
