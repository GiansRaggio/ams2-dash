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
import ams2_analysis as AN                                       # noqa: E402
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

def _vuelta(perdida=0.0, frena_antes=0.0, n=1200, lento=0.0):
    """Una vuelta sintetica con dos curvas.

    `lento` baja la velocidad en TODA la vuelta: sube el tiempo de vuelta parejo sin
    tocar la dispersion. Es la palanca del gap% en los tests de cierre, separada de
    la del CV% (`perdida`), que es justo la separacion que el informe tiene que
    mostrar como dos numeros distintos.

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
        v = max(50.0, v - lento)
        # trazado en forma de anillo: da posiciones reales para el mapa
        ang = 2 * math.pi * d / LARGO
        dist.append(d); spd.append(v); thr.append(th); brk.append(bk); tt.append(t)
        px.append(400.0 * math.cos(ang)); pz.append(260.0 * math.sin(ang))
        t += ds / (v / 3.6)
    return dist, spd, thr, brk, tt, px, pz, t


def escribir_sesion(base, nombre="Test__Car__practice__20260101_000000",
                    n_vueltas=8, con_pos=True, mezclada=False, sigma_frenada=0.0,
                    escala=1.0, lento=0.0, track="Test", car="Car"):
    """Una carpeta de sesion sintetica. `escala` mueve el CV%, `lento` mueve el ritmo.

    `track`/`car` estan para poder armar una sesion de OTRO combo, que es lo unico
    que el modo cierre tiene que rechazar de plano.
    """
    d = os.path.join(base, nombre)
    os.makedirs(d, exist_ok=True)
    # la fecha sale del nombre de la carpeta y no de una constante: el informe de
    # cierre imprime las dos fechas, y con un "started" fijo las dos salian iguales
    f = nombre.split("__")[-1][:8]
    started = (f"{f[:4]}-{f[4:6]}-{f[6:8]}T00:00:00" if len(f) == 8 and f.isdigit()
               else "2026-01-01T00:00:00")
    json.dump({"track": track, "car": car, "track_variation": f"{track}_GP",
               "started": started, "channels": T.HEADER},
              open(os.path.join(d, "session.json"), "w", encoding="utf-8"))
    idx = {h: i for i, h in enumerate(T.HEADER)}
    laps = []
    for k in range(n_vueltas):
        # la vuelta 0 es la mejor; el resto frena antes y pierde en las dos curvas
        perdida = 0.0 if k == 0 else (5.0 + 0.5 * k) * escala
        fa = 0.0 if k == 0 else 30.0
        if sigma_frenada:                    # el punto de frenada salta vuelta a vuelta
            fa = 40.0 + (sigma_frenada if k % 2 else -sigma_frenada)
        dist, spd, thr, brk, tt, px, pz, lt = _vuelta(perdida=perdida, frena_antes=fa,
                                                      lento=lento)
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
    _ok("banda CV 1.6 -> inicial (nombre de la rubrica)", I._banda(1.6, I.BANDAS_CV) == "inicial")
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


def _curva_cruda(paso=2.0, cambios=(), largo=1200.0, plana=False):
    """Traza CRUDA (como `_read_trace`) de una curva: frena 812 · apex 900 · a fondo 930.

    Es la vuelta de laboratorio de `test_analisis._sintetica`. `cambios` es una lista
    de (desde, hasta, throttle, brake) que pisa los pedales en [desde, hasta); None
    deja el canal como estaba. Con `plana` la curva se pasa sin soltar el gas.
    """
    dist = [round(i * paso, 3) for i in range(int(largo / paso) + 1)]
    spd, thr, brk = [], [], []
    for m in dist:
        if m < 812:
            v, t, b = 200.0, 1.0, 0.0
        elif m < 900:
            v, t, b = 200.0 - (m - 812) / 88.0 * (200.0 - 96.3), 0.0, 0.6
        elif m < 930:
            v, t, b = 96.3 + (m - 900) / 30.0 * 20.0, 0.40, 0.0
        else:
            v, t, b = 116.3 + (m - 930) * 0.3, 1.0, 0.0
        if plana:
            t, b = (0.85 if m < 930 else 1.0), 0.0
        for a, z, tc, bc in cambios:
            if a <= m < z:
                t = t if tc is None else tc
                b = b if bc is None else bc
        spd.append(v); thr.append(t); brk.append(b)
    return {"lap_dist": dist, "speed_kmh": spd, "throttle": thr, "brake": brk}


def _acel_visor(cruda):
    """`acel_m` como lo calcula el visor: la traza pasa por la grilla de 2 m de
    `traza()` y despues por `eventos()`, con la curva sacada de la curvatura."""
    cols = ["speed_kmh", "throttle", "brake"]
    xs, vs = AN._rejilla(cruda["lap_dist"], [cruda[c] for c in cols], AN.PASO_M)
    tr = {"paso_m": AN.PASO_M, "metros": [round(x, 1) for x in xs],
          "canales": {c: [round(v, 3) for v in col] for c, col in zip(cols, vs)}}
    e = AN.eventos(tr, [{"n": 1, "inicio": 860.0, "fin": 920.0}])[0]
    return e["acel_m"], e["apex_m"]


def _acel_informe(cruda, apex_m):
    """`acel` como lo calcula el informe en `_causa`: sobre la traza cruda, con la
    frenada de `_punto_frenada`."""
    return I._acelera_en(cruda, apex_m, frenada_m=A._punto_frenada(cruda, apex_m))


def test_acelera_misma_regla_que_el_visor():
    """El informe escrito y el visor dicen el MISMO metro para "donde vuelve al gas".

    Antes el informe buscaba el primer acelerador >= 50 % desde el apex y el visor
    (`acel_m`, el que usa la vista del profe) el primer >= 10 % sostenido despues de
    soltar el freno: en la referencia del curso (Spielberg corta, T1) uno decia "en
    el apex" y el otro "28 m antes". La regla vive ahora en un solo lugar
    (`analyze_telemetry._acelera`) y este test amarra las dos puntas.
    """
    print("\nacelera: el informe y el visor dicen el mismo metro:")
    casos = [
        ("vuelve al gas en el apex", (), 900.0),
        ("vuelve al gas ANTES del apex (la T1 de la referencia del curso)",
         [(880, 900, 0.3, 0.0), (840, 847, 0.6, None)], 880.0),
        ("frenada en dos tiempos: cuenta desde el toque",
         [(850, 900, 0.3, 0.0), (890, 895, 0.0, 0.1)], 896.0),
        ("pie izquierdo: vuelve a fondo con el freno pisado = a fondo",
         [(900, 930, 0.0, None), (930, 937, None, 0.2)], 930.0),
    ]
    for nombre, cambios, esperado in casos:
        cruda = _curva_cruda(cambios=cambios)
        visor, apex = _acel_visor(cruda)
        inf = _acel_informe(cruda, apex)
        _ok(nombre, visor == inf == esperado, (visor, inf))
    plana = _curva_cruda(plana=True)
    visor, apex = _acel_visor(plana)
    _ok("curva a fondo (nunca solto el gas): los dos dicen None",
        visor is None and _acel_informe(plana, apex) is None, (visor, _acel_informe(plana, apex)))

    # Un kink a fondo en el 1.150, con un levanton suelto en la recta (950-960) despues
    # de la curva frenada del 900. `_punto_frenada` del kink se trae la frenada del 812
    # (no tiene el tope del apex anterior); si esa frenada abriera la ventana, el
    # levanton pareceria "vuelve al gas en 960" en una curva que nunca solto el gas.
    kink = _curva_cruda(cambios=[(950, 960, 0.0, None)])
    for k, m in enumerate(kink["lap_dist"]):
        if 1130 <= m <= 1170:
            kink["speed_kmh"][k] -= 20.0 * (1 - abs(m - 1150) / 20.0)
    cols = ["speed_kmh", "throttle", "brake"]
    xs, vs = AN._rejilla(kink["lap_dist"], [kink[c] for c in cols], AN.PASO_M)
    ev = AN.eventos({"paso_m": AN.PASO_M, "metros": xs, "canales": dict(zip(cols, vs))},
                    [{"n": 1, "inicio": 860.0, "fin": 920.0}, {"n": 2, "inicio": 1130.0, "fin": 1170.0}])
    inf = I._acelera_en(kink, ev[1]["apex_m"], frenada_m=A._punto_frenada(kink, ev[1]["apex_m"]),
                        apex_prev_m=ev[0]["apex_m"])
    _ok("kink a fondo tras una curva frenada: los dos dicen None",
        ev[1]["apex_m"] == 1150.0 and ev[1]["acel_m"] is None and inf is None,
        (ev[1]["apex_m"], ev[1]["acel_m"], inf))

    # Con la resolucion de la traza cruda (50 Hz ~ 0,7 m a 130 km/h) el visor redondea
    # a su grilla de 2 m: la diferencia no puede pasar de un paso de grilla.
    for nombre, cambios, _esp in casos:
        cruda = _curva_cruda(paso=0.7, cambios=cambios)
        visor, apex = _acel_visor(cruda)
        inf = _acel_informe(cruda, apex)
        _ok(f"a 0,7 m entre muestras: {nombre}",
            visor is not None and inf is not None and abs(visor - inf) <= AN.PASO_M,
            (visor, inf))


def test_texto_del_gas():
    """"Abre el gas N m mas tarde" sigue siendo cierto con la regla de `acel_m`, que
    puede caer antes del apex: ahi el texto no puede decir "a la salida"."""
    print("\nel texto del gas dice lo que mide:")
    curvas = [{"apex": 900.0, "prom": 100.0}]
    ref = _curva_cruda(cambios=[(860, 900, 0.3, 0.0)])          # vuelve al gas en 860
    tarde = _curva_cruda(cambios=[(884, 900, 0.3, 0.0)])        # 24 m despues, antes del apex
    c = I._causa(tarde, ref, 800, 950, curvas, set())
    _ok("detecta que vuelves al gas 24 m mas tarde",
        c is not None and "vuelves al gas 24 m más tarde" in c["texto"], c and c["texto"])
    _ok("antes del apex no dice 'a la salida'", c is not None and "a la salida" not in c["texto"],
        c and c["texto"])
    salida = _curva_cruda(cambios=[(900, 920, 0.0, None)])      # vuelve al gas en 920
    c2 = I._causa(salida, _curva_cruda(), 800, 950, curvas, set())
    _ok("despues del apex si dice 'a la salida'",
        c2 is not None and "vuelves al gas 20 m más tarde a la salida" in c2["texto"],
        c2 and c2["texto"])


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
    # Una T suelta esta prohibida: la numeracion del visor es POR SESION (sale de la
    # vuelta mas rapida de cada una) y ninguna coincide con el catalogo del
    # circuito, asi que "T4" sin mas significa cosas distintas segun el dia. Desde
    # que `--frenadas`, el visor y el informe comparten la T de la frenada, puede
    # aparecer, pero SIEMPRE con su metro al lado.
    _ok("nombra las curvas por metros", "curva del metro" in h or "(metro " in h)
    suelta = re.search(r"\bT\d+\b(?! \(metro )", h)
    _ok("y nunca por numero de curva suelto (T4 sin su metro)", suelta is None,
        (suelta or [""])[0])
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
    # el mapa NO depende de tener con que comparar: medido sobre el corpus, 238 de
    # 338 sesiones no juntan las 3 vueltas de la comparacion y se quedaban sin
    # imagen teniendo la traza completa guardada.
    mp = I.mapa(d, per)
    _ok("igual dibuja el mapa (la traza existe)", mp is not None and "polyline" in mp["svg"])
    _ok("pero sin referencia superpuesta ni tramos marcados",
        mp and not mp["superpone"] and 'class="pin"' not in mp["svg"])
    h = I.render(I.armar(d, nivel=1))
    _ok("y el informe se genera igual", h.startswith("<!doctype") and "Tu nivel" in h)
    _ok("con la seccion del mapa incluida", "El mapa de tu vuelta" in h)


def test_frenada_dispersa(base):
    print("\nalumno con el punto de frenada disperso:")
    d = escribir_sesion(base, nombre="Disp__Car__practice__20260505_000000", sigma_frenada=40.0)
    num = I.numeros(d)
    num["foco"] = "consistencia"
    cosa = I.una_cosa(d, I.perdidas(d), num)
    _ok("la 'una sola cosa' pasa a ser una meta de PROCESO",
        "mismo punto" in cosa["titulo"], cosa["titulo"])
    _ok("y dice cuanto se mueve hoy (el rango, como el panel)",
        cosa["medida"] and "se mueve" in cosa["medida"] and "m entre vueltas" in cosa["medida"],
        cosa["medida"])
    # el informe lo recibe el alumno y el Nivel 1 no ve σ (regla del curso)
    h_disp = I.render(I.armar(d, nivel=1))
    _ok("el informe del alumno no muestra σ en ninguna parte",
        "σ" not in h_disp and "sigma" not in h_disp.lower(), cosa["medida"])
    # La anilla sintetica no tiene curvas por curvatura donde frena, asi que ahi la
    # frenada sale por metro. Para ver el camino con T se le da al informe la
    # numeracion del visor a mano: T4 en el apex del metro 1.000, T7 en el 2.500.
    orig = A._t_del_visor
    try:
        A._t_del_visor = lambda folder, traza: ([(4, 1000.0), (7, 2500.0)], [],
                                                {"fuente": "pista", "archivo": "x.json"})
        cosa = I.una_cosa(d, I.perdidas(d), num)
        _ok("con la T del visor la frenada se nombra como en el visor, con su metro",
            re.search(r"en la T[47] \(metro [\d.]+\)$", cosa["titulo"]) is not None,
            cosa["titulo"])
        _ok("y el titulo sigue siendo corto", len(cosa["titulo"]) < 70, len(cosa["titulo"]))
        h = I.render(I.armar(d, nivel=2))
        suelta = re.search(r"\bT\d+\b(?! \(metro )", h)
        _ok("en el HTML la T va siempre con su metro", suelta is None, (suelta or [""])[0])
    finally:
        A._t_del_visor = orig


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


def test_cierre_del_curso(base):
    """El producto del curso: el delta entre la medicion de entrada y la de salida.

    El curso abre con la medicion de entrada (clase 2) y cierra con la de salida
    (clase 8) en el mismo combo ancla. Lo que se entrega no es el informe de la
    clase 8: es cuanto se movieron los dos numeros y la imagen de las dos vueltas
    sobre el mismo trazado. Lo que se prueba aca es que ese delta no se pueda
    fabricar: mismo combo obligatorio, y si falta una punta se dice.
    """
    print("\ncierre del curso (--contra la medicion de entrada):")
    # entrada: mas lenta (peor gap) y mas dispersa (peor CV) que la salida
    ent = escribir_sesion(base, nombre="Test__Car__practice__20260107_000000",
                          escala=2.5, lento=8.0)
    sal = escribir_sesion(base, nombre="Test__Car__practice__20260808_000000")
    _ok("mismo combo: la validacion pasa", I.validar_par(sal, ent) is None,
        I.validar_par(sal, ent))

    ref_orig = A.REFDIR
    A.REFDIR = os.path.join(base, "_refs_cierre")
    try:
        os.makedirs(A.REFDIR, exist_ok=True)
        meta, _ = A._load(sal)
        with open(os.path.join(A.REFDIR, A._ref_nombre(meta) + ".json"), "w",
                  encoding="utf-8") as f:
            json.dump({"car": "Car", "track": "Test", "track_variation": "Test_GP", "lap": 1,
                       "lap_time": round(A.mejor_de_la_tanda(sal)["lap_time"], 3),
                       "sectors": [30.0, 30.0, 30.0],
                       "cond": {"mojado": False, "tc": 0, "abs": 0, "compuesto": "Lisos"}}, f)
        n_sal, n_ent = I.numeros(sal), I.numeros(ent)
        dl = I.delta_numeros(n_sal, n_ent)
        _ok("la entrada era mas lenta que la salida", n_ent["gap"]["valor"] > 0.5,
            n_ent["gap"] and n_ent["gap"]["valor"])
        _ok("Δgap = salida - entrada, negativo porque mejoro",
            dl["gap"]["valor"] < 0 and
            abs(dl["gap"]["valor"] - (n_sal["gap"]["valor"] - n_ent["gap"]["valor"])) < 0.011,
            dl["gap"]["valor"])
        _ok("Δcv tambien con signo, negativo porque se junto",
            dl["cv"]["valor"] < 0 and
            abs(dl["cv"]["valor"] - (n_sal["cv"]["valor"] - n_ent["cv"]["valor"])) < 0.011,
            dl["cv"]["valor"])
        _ok("el delta guarda las dos puntas, no solo la resta",
            dl["gap"]["entrada"] == n_ent["gap"]["valor"] and
            dl["gap"]["salida"] == n_sal["gap"]["valor"])

        d = I.armar(sal, nivel=2, alumno="Alumna Cierre", contra=ent)
        h = I.render(d)
        _ok("la cabecera dice que es el cierre del curso",
            "Informe de cierre del curso" in h)
        _ok("y lleva las DOS fechas", "2026-01-07" in h and "2026-08-08" in h)
        _ok("el HTML muestra el Δ del curso", "Δ del curso" in h)
        _ok("y lo dice en palabras, no solo con el signo", "bajaste" in h)
        # el mapa es el producto: TU vuelta de entrada contra TU vuelta de salida
        mp = d["mapa"]
        _ok("el mapa superpone las dos vueltas del alumno", mp and mp["superpone"])
        _ok("la leyenda dice entrada y salida con la fecha",
            mp and mp["etq_otra"] == "entrada 07-01" and mp["etq_tuya"] == "salida 08-08",
            mp and (mp["etq_tuya"], mp["etq_otra"]))
        _ok("la referencia de la escuela NO se dibuja en el cierre",
            "referencia</span>" not in h and "entrada 07-01" in h)
        _ok("y dice por que no esta", "es tuya contra ti mismo" in h)
        _ok("el logro de progreso sale de las dos carpetas explicitas",
            any("medición de entrada del 07-01" in x for x in d["logros"]), d["logros"])
    finally:
        A.REFDIR = ref_orig

    # Sin --contra la linea de progreso sigue existiendo, pero (a) sale de la tanda y
    # no de la mejor vuelta del dia -- ese era el sesgo: el minimo de una muestra baja
    # solo con mas intentos -- y (b) se presenta como lo que es, la carpeta anterior
    # del combo que hay en el disco, no la medicion de entrada del curso.
    print("\n  sin --contra, la sesion anterior se nombra como lo que es:")
    d_solo = I.armar(sal, nivel=1)
    linea = next((x for x in d_solo["logros"] if "bajó" in x), None)
    _ok("hay linea de progreso contra la sesion anterior del combo", bool(linea), linea)
    _ok("y dice que es la anterior en este combo, con la fecha",
        linea and "tu sesión anterior en este combo (la del 07-01)" in linea, linea)
    _ok("mide con la mejor de la TANDA, no con la mejor del dia",
        linea and f"{A.mejor_de_la_tanda(ent)['lap_time'] - A.mejor_de_la_tanda(sal)['lap_time']:.2f} s"
        in linea, linea)

    print("\n  el delta no se fabrica:")
    otro = escribir_sesion(base, nombre="Otro__Car__practice__20260505_000000", track="Otro")
    _ok("otro combo se rechaza", I.validar_par(sal, otro) is not None)
    _ok("y el motivo nombra que cambio",
        "pista" in (I.validar_par(sal, otro) or ""), I.validar_par(sal, otro))
    try:
        I.armar(sal, nivel=2, contra=otro)
        _ok("armar revienta con un combo distinto", False)
    except ValueError as e:
        _ok("armar revienta con un combo distinto", "mismo combo" in str(e), str(e))

    corta = escribir_sesion(base, nombre="Test__Car__practice__20260202_000000", n_vueltas=2)
    dl = I.delta_numeros(I.numeros(sal), I.numeros(corta))
    _ok("con una entrada de 2 vueltas el Δcv se declara faltante",
        dl["cv"]["valor"] is None and dl["cv"]["falta"])
    _ok("y el motivo dice que la punta que falla es la entrada",
        "en la medición de entrada" in dl["cv"]["falta"], dl["cv"]["falta"])
    h = I.render(I.armar(sal, nivel=1, contra=corta))
    _ok("el informe lo dice y no estima", "No se puede calcular" in h and
        "No se estima con otra cosa" in h)


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
        test_acelera_misma_regla_que_el_visor()
        test_texto_del_gas()
        d = test_sesion_normal(base)
        test_niveles(d)
        test_html(d)
        test_condiciones_mezcladas(base)
        test_sin_posicion(base)
        test_pocas_vueltas(base)
        test_frenada_dispersa(base)
        test_gap_solo_de_la_tanda(base)
        test_cierre_del_curso(base)
        test_archivo(base, d)
    finally:
        A.TELEM, A.REFDIR = old_telem, old_ref
        shutil.rmtree(base, ignore_errors=True)
    print(f"\ndone. {_TOTAL[0]} PASS · {_TOTAL[1]} FAIL")
    return 1 if _TOTAL[1] else 0


if __name__ == "__main__":
    sys.exit(main())
