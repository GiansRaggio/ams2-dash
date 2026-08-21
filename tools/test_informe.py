#!/usr/bin/env python3
"""Tests del informe del alumno (tools/informe.py).

Lo que se prueba aca no es que el HTML salga bonito: es que el informe no
INVENTE. Con esto se le habla a personas reales, asi que cada regla del metodo
que el codigo implementa tiene su test:

  - sin referencia guardada, el gap% se declara faltante y NUNCA se rellena
  - una misma curva no puede ser la causa de dos tramos
  - un tramo sin causa identificable no se lista
  - el nivel jamas se deduce de los numeros
  - una sesion con condiciones mezcladas no emite ningun numero

Correr: python tools/test_informe.py
"""
import gzip
import json
import math
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))
import ams2_telemetry as T                                       # noqa: E402
import analyze_telemetry as A                                    # noqa: E402
import informe as I                                              # noqa: E402

LARGO = 4000.0
APEX = (1000.0, 2500.0)

_TOTAL = [0, 0]

# La consola de Windows entrega cp1252 y el informe lleva acentos y sigmas. Sin esto
# el suite MUERE a mitad de camino por un UnicodeEncodeError al imprimir un PASS, y
# el resumen final no se llega a escribir: la corrida se ve como si no hubiera fallado.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _ok(name, cond, extra=""):
    _TOTAL[0 if cond else 1] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    return cond


# --- sesion sintetica -----------------------------------------------------------

def _vuelta(perdida=0.0, frena_antes=0.0, n=1200):
    """Una vuelta sintetica con dos curvas.

    `frena_antes` corre el inicio de la frenada hacia atras y baja la velocidad en
    todo ese tramo: es el error que el informe tiene que detectar y nombrar.
    Ademas, despues del primer apex hay una ganancia y otra perdida, para que la
    MISMA curva genere dos rachas de delta separadas -- ese es el patron que
    obliga al candado de "una curva no puede ser causa de dos tramos".

    n=1200 muestras a proposito: `braking_struct` descarta como truncada cualquier
    traza con menos de 1000, asi que una vuelta mas corta no tendria frenadas.
    """
    dist, spd, thr, brk, tt, px, pz = [], [], [], [], [], [], []
    t, ds = 0.0, LARGO / n
    for k in range(n):
        d = k * ds
        v = 200.0
        for cd in APEX:
            v -= 120.0 * math.exp(-((d - cd) / 160.0) ** 2)
        th, bk = 1.0, 0.0
        for cd in APEX:
            if cd - 250 - frena_antes <= d < cd - 150 - frena_antes:
                th, bk = 0.0, 0.0            # coasting
            elif cd - 150 - frena_antes <= d < cd:
                th, bk = 0.0, 1.0            # frenada
                v -= perdida                 # frenar antes = llegar mas lento
        if perdida:
            if 1000 <= d < 1050:
                v += perdida                 # ganancia: parte la perdida en dos rachas
            elif 1050 <= d < 1150:
                v -= perdida
        v = max(50.0, v)
        # trazado en forma de anillo: da posiciones reales para el mapa
        ang = 2 * math.pi * d / LARGO
        dist.append(d); spd.append(v); thr.append(th); brk.append(bk); tt.append(t)
        px.append(400.0 * math.cos(ang)); pz.append(260.0 * math.sin(ang))
        t += ds / (v / 3.6)
    return dist, spd, thr, brk, tt, px, pz, t


def escribir_sesion(base, nombre="Test__Car__practice__20260101_000000",
                    n_vueltas=8, con_pos=True, mezclada=False, sigma_frenada=0.0):
    d = os.path.join(base, nombre)
    os.makedirs(d, exist_ok=True)
    json.dump({"track": "Test", "car": "Car", "track_variation": "Test_GP",
               "started": "2026-01-01T00:00:00", "channels": T.HEADER},
              open(os.path.join(d, "session.json"), "w", encoding="utf-8"))
    idx = {h: i for i, h in enumerate(T.HEADER)}
    laps = []
    for k in range(n_vueltas):
        # la vuelta 0 es la mejor; el resto frena antes y pierde en las dos curvas
        perdida = 0.0 if k == 0 else 5.0 + 0.5 * k
        fa = 0.0 if k == 0 else 30.0
        if sigma_frenada:                    # el punto de frenada salta vuelta a vuelta
            fa = 40.0 + (sigma_frenada if k % 2 else -sigma_frenada)
        dist, spd, thr, brk, tt, px, pz, lt = _vuelta(perdida=perdida, frena_antes=fa)
        rows = []
        for j in range(len(dist)):
            r = [0.0] * len(T.HEADER)
            r[idx["t"]] = round(tt[j], 4)
            r[idx["lap_dist"]] = round(dist[j], 2)
            r[idx["speed_kmh"]] = round(spd[j], 2)
            r[idx["throttle"]] = thr[j]
            r[idx["brake"]] = brk[j]
            if con_pos:
                r[idx["pos_x"]] = round(px[j], 2)
                r[idx["pos_z"]] = round(pz[j], 2)
            rows.append(",".join(map(str, r)))
        tname = f"L{k + 1:03d}_{lt:.3f}s.csv.gz"
        with gzip.open(os.path.join(d, tname), "wt", encoding="utf-8") as f:
            f.write(",".join(T.HEADER) + "\n" + "\n".join(rows) + "\n")
        laps.append({"uid": k + 1, "lap": k + 1, "lap_time": round(lt, 3), "valid": True,
                     "samples": len(dist), "sectors": [round(lt / 3, 3)] * 3,
                     "compound": "Lisos" if not (mezclada and k > 3) else "Lluvia",
                     "tc_setting": 0, "abs_setting": 0, "rain": 0.0, "trace": tname})
    with open(os.path.join(d, "summary.jsonl"), "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(x) for x in laps))
    return d


# --- tests ----------------------------------------------------------------------

def test_formato():
    print("\nformato y escalas:")
    _ok("_m pone punto de miles", I._m(1180) == "1.180", I._m(1180))
    _ok("_m redondea", I._m(3983.6) == "3.984", I._m(3983.6))
    _ok("banda gap 1.9 -> excelente", I._banda(1.9, I.BANDAS_GAP) == "excelente")
    _ok("banda gap 2.0 -> bueno (el corte es exclusivo abajo)",
        I._banda(2.0, I.BANDAS_GAP) == "bueno", I._banda(2.0, I.BANDAS_GAP))
    _ok("banda gap 99 -> se ancla en la ultima", I._banda(99.0, I.BANDAS_GAP) == "inicial")
    _ok("banda CV 0.29 -> excelente", I._banda(0.29, I.BANDAS_CV) == "excelente")
    _ok("banda CV 1.6 -> dispersa", I._banda(1.6, I.BANDAS_CV) == "dispersa")
    _ok("_t formatea sobre el minuto", I._t(95.395) == "1:35.395", I._t(95.395))


def test_foco():
    print("\nla matriz gap x CV decide el FOCO, no el nivel:")
    _ok("gap bajo + CV bajo = listo", I.foco(1.5, 0.5) == "listo para el desafío")
    _ok("gap bajo + CV alto = consistencia", I.foco(1.5, 2.0) == "consistencia")
    _ok("gap medio + CV bajo = tecnica", I.foco(3.0, 0.5) == "técnica")
    _ok("gap medio + CV medio = las dos", I.foco(3.0, 1.0) == "las dos parejo")
    _ok("gap alto + CV alto = fundamentos", I.foco(7.0, 2.0) == "fundamentos")
    # el caso que importa hoy: sin combo ancla no hay gap
    _ok("sin gap y CV alto -> igual pide consistencia", I.foco(None, 2.0) == "consistencia")
    _ok("sin gap y CV bueno -> NO inventa foco", I.foco(None, 0.4) is None,
        I.foco(None, 0.4))
    _ok("sin ninguno -> None", I.foco(None, None) is None)


def test_marco_de_coordenadas():
    print("\nel mapa no dibuja dos trazados que no comparten ejes:")
    a = {"pos_x": [0.0, 100.0, 200.0], "pos_z": [0.0, 50.0, 100.0]}
    ceros = {"pos_x": [0.0, 0.0, 0.0], "pos_z": [0.0, 0.0, 0.0]}
    lejos = {"pos_x": [9000.0, 9100.0, 9200.0], "pos_z": [9000.0, 9050.0, 9100.0]}
    chico = {"pos_x": [0.0, 10.0, 20.0], "pos_z": [0.0, 5.0, 10.0]}
    _ok("una traza consigo misma comparte marco", I._mismo_marco(a, a))
    _ok("posiciones en cero (sesion importada) = sin posicion", not I._tiene_posicion(ceros))
    _ok("no superpone contra una traza en cero", not I._mismo_marco(a, ceros))
    _ok("no superpone si el trazado esta en otro lado", not I._mismo_marco(a, lejos))
    _ok("no superpone si la escala no calza", not I._mismo_marco(a, chico))


def test_variante():
    print("\nla variante solo si agrega algo:")
    _ok("Spielberg_Modern -> Modern",
        I._variante({"track": "Spielberg", "track_variation": "Spielberg_Modern"}) == "Modern")
    _ok("variante igual a la pista -> vacio",
        I._variante({"track": "Monza", "track_variation": "Monza"}) == "")
    _ok("sin variante -> vacio", I._variante({"track": "Monza"}) == "")


def test_sesion_normal(base):
    print("\nsesion completa (8 vueltas, sin referencia guardada):")
    d = escribir_sesion(base)
    num = I.numeros(d)
    _ok("sin referencia el gap NO se calcula", num["gap"] is None)
    _ok("y se declara por que falta",
        any(f.startswith("ritmo:") and "referencia" in f for f in num["faltan"]))
    _ok("el CV si se calcula", num["cv"] is not None and num["cv"]["valor"] >= 0,
        num["cv"] and num["cv"]["valor"])

    num["foco"] = I.foco(None, num["cv"]["valor"] if num["cv"] else None)
    per = I.perdidas(d)
    _ok("encuentra al menos un tramo de perdida", len(per["tramos"]) >= 1, len(per["tramos"]))
    _ok("sin referencia compara contra tu propia mejor vuelta",
        per["es_ref"] is False and "mejor vuelta" in (per["contra"] or ""), per["contra"])
    apexes = [t["apex"] for t in per["tramos"]]
    _ok("una misma curva no es causa de dos tramos", len(apexes) == len(set(apexes)), apexes)
    # el candado tiene que ser el que descarta, no la casualidad: la vuelta sintetica
    # genera MAS rachas de perdida que tramos listados, y las de sobra son de la
    # curva del metro 1.000, que ya se reporto.
    ta, tb = I.par_trazas(d)[:2]
    grid, delta = I._delta_grilla(ta, tb)
    rachas = [r for r in I._runs(grid, delta, +1) if r["s"] >= I.PISO_S]
    _ok("hay mas rachas de perdida que tramos listados",
        len(rachas) > len(per["tramos"]), (len(rachas), len(per["tramos"])))
    _ok("la curva del metro 1.000 aparece una sola vez",
        sum(1 for a in apexes if abs(a - 1000) < 120) == 1, apexes)
    _ok("todo tramo listado trae accion ejecutable",
        all(t.get("accion") and t.get("titulo") for t in per["tramos"]))
    _ok("ningun tramo bajo el piso de resolucion",
        all(t["s"] >= I.PISO_S for t in per["tramos"]))
    _ok("los tramos vienen ordenados de mayor a menor",
        [t["s"] for t in per["tramos"]] == sorted([t["s"] for t in per["tramos"]], reverse=True))

    cosa = I.una_cosa(d, per, num)
    _ok("la 'una sola cosa' trae titulo, porque y como",
        all(cosa.get(k) for k in ("titulo", "porque", "como")))
    _ok("el titulo es corto (no es la accion completa)", len(cosa["titulo"]) < 70,
        len(cosa["titulo"]))

    mp = I.mapa(d, per)
    _ok("dibuja el mapa cuando hay posicion", mp is not None and "polyline" in mp["svg"])
    _ok("no superpone referencia si no hay", mp and mp["superpone"] is False)
    _ok("marca en el mapa tantos pines como tramos",
        mp and mp["svg"].count('class="pin"') == len(per["tramos"]),
        mp and mp["svg"].count('class="pin"'))
    return d


def test_niveles(d):
    print("\nel mismo informe no sirve para los tres niveles:")
    n1 = I.armar(d, nivel=1)
    n3 = I.armar(d, nivel=3)
    _ok("nivel 1 recibe UN tramo en el HTML",
        I._sec_tramos(n1).count("<li>") == min(1, len(n1["perdidas"]["tramos"])),
        I._sec_tramos(n1).count("<li>"))
    _ok("nivel 3 recibe la lista completa",
        I._sec_tramos(n3).count("<li>") == len(n3["perdidas"]["tramos"]),
        I._sec_tramos(n3).count("<li>"))
    _ok("nivel 3 NO lleva el bloque de logros", n3["logros"] == [])
    _ok("nivel 1 lo lleva (si hay algo medido)", isinstance(n1["logros"], list))

    print("\nel nivel es un permiso, no una nota:")
    sin = I.armar(d, nivel=None)
    _ok("sin --nivel el informe no deduce ninguno", sin["nivel"] is None)
    h = I.render(sin)
    _ok("y lo dice: el nivel lo define el instructor", "lo define tu instructor" in h)
    _ok("los tres niveles se muestran como permisos",
        h.count("<td class=\"n\">") == 3, h.count("<td class=\"n\">"))
    _ok("declara que la velocidad no otorga el nivel",
        "no sube por ser rápido" in h)


def test_html(d):
    print("\nel HTML se sostiene solo:")
    h = I.render(I.armar(d, nivel=2, alumno="Alumno Prueba"))
    _ok("empieza con doctype", h.startswith("<!doctype html>"))
    _ok("cierra html", h.rstrip().endswith("</html>"))
    _ok("un solo <title>", h.count("<title>") == 1)
    _ok("declara utf-8", 'charset="utf-8"' in h)
    _ok("no pide nada por red (se abre sin internet)",
        "http://" not in h and "https://" not in h)
    _ok("lleva el nombre del alumno", "Alumno Prueba" in h)
    _ok("las 5 secciones en orden",
        [h.index(x) for x in ("Tus dos números", "el tiempo", "El mapa de tu vuelta",
                              "Una sola cosa", "Tu nivel")] ==
        sorted(h.index(x) for x in ("Tus dos números", "el tiempo", "El mapa de tu vuelta",
                                    "Una sola cosa", "Tu nivel")))
    # el numero de curva esta prohibido en los informes: las dos herramientas del
    # dash las numeran distinto entre si y ninguna coincide con el catalogo del
    # circuito, asi que "T4" significa cosas distintas segun quien lo diga.
    _ok("nombra las curvas por metros", "curva del metro" in h)
    _ok("y nunca por numero de curva (T1, T4...)",
        re.search(r"\bT\d+\b", h) is None,
        (re.search(r"\bT\d+\b", h) or [""])[0])
    _ok("dice que no hay comparacion con otros alumnos",
        "nada está comparado con otro alumno" in h)
    _ok("advierte que el delta no es un cronometro exacto",
        "no como cronómetro exacto" in h)


def test_condiciones_mezcladas(base):
    print("\nuna sesion con condiciones mezcladas no emite numeros:")
    d = escribir_sesion(base, nombre="Mix__Car__practice__20260202_000000", mezclada=True)
    num = I.numeros(d)
    _ok("se detecta la mezcla", num["mezclada"] is True)
    _ok("no emite gap", num["gap"] is None)
    _ok("no emite CV", num["cv"] is None)
    h = I.render(I.armar(d, nivel=2))
    _ok("el informe lo dice derecho", "no se puede medir" in h)
    _ok("y no muestra ninguna cifra de rubrica",
        "gap%" not in h and "CV%" not in h)
    _ok("igual muestra el nivel (que no depende de esta tanda)", "Tu nivel" in h)


def test_sin_posicion(base):
    print("\nsesion sin canal de posicion (importada de otro piloto):")
    d = escribir_sesion(base, nombre="NoPos__Car__practice__20260303_000000", con_pos=False)
    per = I.perdidas(d)
    _ok("no dibuja mapa", I.mapa(d, per) is None)
    h = I.render(I.armar(d, nivel=2))
    _ok("el informe sale igual, sin la seccion del mapa", "El mapa de tu vuelta" not in h)
    _ok("y sigue teniendo las otras secciones", "Una sola cosa" in h and "Tu nivel" in h)


def test_pocas_vueltas(base):
    print("\nsesion de 2 vueltas (no alcanza para nada):")
    d = escribir_sesion(base, nombre="Corta__Car__practice__20260404_000000", n_vueltas=2)
    num = I.numeros(d)
    _ok("sin CV, y declarado", num["cv"] is None and
        any(f.startswith("consistencia:") for f in num["faltan"]))
    per = I.perdidas(d)
    _ok("sin tramos (no hay vuelta tipica contra la cual comparar)", per["tramos"] == [],
        len(per["tramos"]))
    cosa = I.una_cosa(d, per, num)
    _ok("la 'una cosa' pasa a ser juntar la tanda", "8 vueltas" in cosa["titulo"],
        cosa["titulo"])
    h = I.render(I.armar(d, nivel=1))
    _ok("y el informe se genera igual", h.startswith("<!doctype") and "Tu nivel" in h)


def test_frenada_dispersa(base):
    print("\nalumno con el punto de frenada disperso:")
    d = escribir_sesion(base, nombre="Disp__Car__practice__20260505_000000", sigma_frenada=40.0)
    num = I.numeros(d)
    num["foco"] = "consistencia"
    cosa = I.una_cosa(d, I.perdidas(d), num)
    _ok("la 'una sola cosa' pasa a ser una meta de PROCESO",
        "mismo punto" in cosa["titulo"], cosa["titulo"])
    _ok("y dice cuanto varia hoy", cosa["medida"] and "m en el punto de frenada" in cosa["medida"],
        cosa["medida"])


def test_gap_solo_de_la_tanda(base):
    """El gap% sale de las PRIMERAS 8, no de la mejor vuelta del dia.

    docs/evaluacion.md fija el numero de vueltas de antemano porque el minimo de una
    muestra mejora solo por tener mas intentos. Antes de este test el analizador
    media contra `clean_laps()["best_lap_time"]` -- la mejor de TODA la sesion -- y
    con eso el alumno que gira 20 vueltas sale mejor que el que gira 8 sin manejar
    mejor. Lo encontro el informe, al mostrar dos "mejor vuelta" distintas.
    """
    print("\nel gap% se mide sobre la tanda que califica, no sobre el dia entero:")
    d = escribir_sesion(base, nombre="Tanda__Car__practice__20260606_000000", n_vueltas=8)
    antes = A.mejor_de_la_tanda(d)["lap_time"]
    with open(os.path.join(d, "summary.jsonl"), "a", encoding="utf-8") as f:
        f.write("\n" + json.dumps({"uid": 99, "lap": 99, "lap_time": round(antes - 5.0, 3),
                                   "valid": True, "compound": "Lisos", "tc_setting": 0,
                                   "abs_setting": 0, "rain": 0.0}))
    _ok("una 9a vuelta 5 s mas rapida NO mueve la mejor de la tanda",
        A.mejor_de_la_tanda(d)["lap_time"] == antes, A.mejor_de_la_tanda(d)["lap_time"])
    _ok("y clean_laps si la ve (por eso no sirve para calificar)",
        A.clean_laps(d)["best_lap_time"] < antes, A.clean_laps(d)["best_lap_time"])

    ref_orig = A.REFDIR
    A.REFDIR = os.path.join(base, "_refs_tanda")
    try:
        os.makedirs(A.REFDIR, exist_ok=True)
        meta, _ = A._load(d)
        nom = A._ref_nombre(meta)
        with open(os.path.join(A.REFDIR, nom + ".json"), "w", encoding="utf-8") as f:
            json.dump({"car": "Car", "track": "Test", "track_variation": "Test_GP",
                       "lap": 1, "lap_time": round(antes, 3), "sectors": [30.0, 30.0, 30.0],
                       "cond": {"mojado": False, "tc": 0, "abs": 0, "compuesto": "Lisos"}}, f)
        gap = A.evaluar(d)["dimensiones"]["tecnica"]["valor"]
        _ok("gap 0.00% contra una referencia igual a tu mejor de la tanda",
            abs(gap) < 0.01, gap)
        h = I.render(I.armar(d, nivel=2))
        _ok("el informe explica por que cuenta la tanda y no el dia",
            "el que gira más saldría mejor" in h)
    finally:
        A.REFDIR = ref_orig


def test_archivo(base, d):
    print("\nescritura del archivo:")
    ruta = I.generar(d, nivel=2, alumno="Ñuñoa Tester",
                     salida=os.path.join(base, "salida.html"))
    _ok("escribe el archivo", os.path.exists(ruta))
    txt = open(ruta, encoding="utf-8").read()
    _ok("se relee en utf-8 con acentos intactos", "Ñuñoa Tester" in txt and "sesión" in txt)
    _ok("tiene tamano razonable para un correo", 8000 < len(txt) < 400000, len(txt))


def main():
    base = tempfile.mkdtemp(prefix="informe_test_")
    old_telem, old_ref = A.TELEM, A.REFDIR
    A.TELEM = base
    A.REFDIR = os.path.join(base, "_refs")     # vacio: no hay referencia guardada
    try:
        test_formato()
        test_foco()
        test_marco_de_coordenadas()
        test_variante()
        d = test_sesion_normal(base)
        test_niveles(d)
        test_html(d)
        test_condiciones_mezcladas(base)
        test_sin_posicion(base)
        test_pocas_vueltas(base)
        test_frenada_dispersa(base)
        test_gap_solo_de_la_tanda(base)
        test_archivo(base, d)
    finally:
        A.TELEM, A.REFDIR = old_telem, old_ref
        shutil.rmtree(base, ignore_errors=True)
    print(f"\ndone. {_TOTAL[0]} PASS · {_TOTAL[1]} FAIL")
    return 1 if _TOTAL[1] else 0


if __name__ == "__main__":
    sys.exit(main())
