#!/usr/bin/env python3
"""Informe de UNA pagina para el alumno, desde la carpeta cruda de una sesion.

Es la ultima pieza de software que el curso necesita: el zip llega, esto corre, y
sale un HTML autocontenido que el instructor manda. Sin interfaz y sin que nadie
tenga que entrar a alimentar un sistema.

La estructura y el orden salen de docs/diagnostico.md de la escuela y no se
reordenan aca:

  1. Tus dos numeros, con su escala al lado. Sin compararte con nadie.
  2. Los tramos donde mas tiempo pierdes, POR DISTANCIA EN METROS y en palabras.
  3. El mapa de la vuelta con tu trazada y la de referencia encima.
  4. UNA sola cosa para trabajar.
  5. Tu nivel y que significa, en permisos.

Cuatro reglas del metodo que estan implementadas y no son decoracion:

  - "Ningun dato se muestra sin la accion ejecutable que lo acompana." Un tramo
    sin causa identificable NO se lista: seria una discrepancia sin ruta, que es
    la categoria de feedback que mide peor.
  - Una curva no puede ser la causa de dos tramos. Una curva ocupa mas de 50 m y
    reparte su perdida en varios segmentos de la grilla; sin este candado, el
    mismo error del alumno le llega como tres problemas distintos.
  - "El mismo informe no sirve para los tres niveles." Nivel 1 abre con lo que ya
    le sale y recibe UN tramo; Nivel 3 recibe la lista ordenada sin preambulo.
    Los dos documentos tiran para lados distintos aca (diagnostico.md define un
    informe unico, metodo-de-ensenanza.md pide diferenciarlo): se resuelve
    manteniendo las 5 secciones y su orden para todos, y variando el CONTENIDO
    de la seccion 2 y la presencia del bloque de logros.
  - "El nivel es un permiso, no una nota." Por eso `--nivel` es un PARAMETRO que
    pone el instructor. El informe jamas lo deduce de los numeros.

Uso:
    python tools/informe.py <carpeta_sesion> [--nivel 1|2|3] [--alumno "Nombre"]
                            [--salida ruta.html] [--abrir]
"""
import argparse
import datetime
import html as _html
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_telemetry as A                                    # noqa: E402

SALIDA = os.path.join(HERE, "informes")

# Escalas: son las de docs/evaluacion.md y aca NO se redefinen, solo se dibujan.
# El nombre de la ultima banda difiere a proposito entre las dos: en gap "inicial"
# describe a alguien que esta empezando, pero un CV alto no es ser principiante --
# es dispersion, que es lo que el alumno tiene que oir.
BANDAS_GAP = [("excelente", 0.0, 2.0), ("bueno", 2.0, 3.5),
              ("en desarrollo", 3.5, 5.0), ("inicial", 5.0, 8.0)]
BANDAS_CV = [("excelente", 0.0, 0.3), ("buena", 0.3, 0.7),
             ("en desarrollo", 0.7, 1.5), ("dispersa", 1.5, 3.0)]

# Permisos por nivel (docs/diagnostico.md). El informe los muestra tal cual: lo
# que el alumno gana al subir es esto, no una etiqueta.
PERMISOS = [
    (1, "En el canal de voz durante toda la tanda",
     "Sólo en rectas designadas, y avisando", "Convoy y ejercicios"),
    (2, "Interviene sólo entre tandas",
     "En cualquier parte, todavía avisando", "Tandas libres y carreras cortas"),
    (3, "Autónomo; aparece en el debrief",
     "Abierto", "Carrera completa con clasificación"),
]

PISO_S = 0.05        # bajo esto un tramo esta dentro de la resolucion del delta
PASO_M = 50.0        # grilla de distancia, igual que report_vs


# --- utilidades de formato ------------------------------------------------------

def _m(x):
    """1180 -> '1.180'. Los metros se escriben como en el documento del alumno."""
    return f"{int(round(x)):,}".replace(",", ".")


def _t(s):
    if s is None:
        return "--"
    m, sec = divmod(float(s), 60)
    return f"{int(m)}:{sec:06.3f}" if m else f"{sec:.3f}s"


def _banda(valor, bandas):
    """Nombre de la banda donde cae el valor (la ultima si se pasa de largo)."""
    if valor is None:
        return None
    for nombre, _lo, hi in bandas:
        if valor < hi:
            return nombre
    return bandas[-1][0]


# --- los dos numeros ------------------------------------------------------------

def numeros(folder):
    """gap% y CV% con su banda, o declarados faltantes. Reusa `evaluar`.

    No recalcula el gap: `evaluar` ya resuelve la referencia, el aviso de
    condiciones distintas y el gate de sesion mezclada. Duplicar esa logica aca
    seria la forma mas facil de que el informe y la nota dejaran de coincidir.
    """
    e = A.evaluar(folder)
    mezclada = any("mezcla condiciones" in f for f in e["faltantes"])
    out = {"condiciones": e.get("condiciones"), "mezclada": mezclada,
           "gap": None, "cv": None, "faltan": []}

    tec = e["dimensiones"].get("tecnica")
    if tec:
        out["gap"] = {"valor": tec["valor"], "banda": _banda(max(tec["valor"], 0.0), BANDAS_GAP),
                      "ref_s": tec["ref_s"], "tuyo_s": tec["tuyo_s"], "aviso": tec.get("aviso")}
    else:
        out["faltan"].append("ritmo: todavía no hay una vuelta de referencia de la escuela "
                             "en este auto y esta pista, así que el gap% no se puede calcular")

    c = A.consistency_struct(folder)
    if c and c["califica"]:
        out["cv"] = {"valor": c["cv_pct"], "banda": _banda(c["cv_pct"], BANDAS_CV),
                     "n": c["n"], "mediana_s": c["mediana_s"], "mejor_s": c["mejor_s"],
                     "incidentes": c["incidentes"], "degradando": c["degradando"],
                     "cv_dest": c["cv_destendenciado_pct"]}
    elif c:
        out["faltan"].append("consistencia: " + c["veredicto"].replace("sin veredicto: ", "")
                             .replace("todavia", "todavía").replace("sesion", "sesión"))
    else:
        out["faltan"].append("consistencia: se necesitan 6 o más vueltas cronometradas seguidas")
    out["_consist"] = c
    return out


def foco(gap, cv):
    """La matriz gap x CV de docs/diagnostico.md: en que se trabaja primero.

    None cuando no se puede decidir. Con el ritmo faltante hay una sola celda que
    la matriz igual resuelve -- un CV alto pide consistencia en las tres filas --
    y fuera de ahi inventar un foco seria decidir con la mitad del dato.
    """
    if gap is None and cv is None:
        return None
    if gap is None:
        return "consistencia" if cv > 1.5 else None
    if cv is None:
        return "técnica" if gap > 5.0 else None
    if gap <= 2.0:
        return "listo para el desafío" if cv <= 0.7 else "consistencia"
    if gap <= 5.0:
        return ("técnica" if cv <= 0.7 else
                "las dos parejo" if cv <= 1.5 else "consistencia")
    return "técnica" if cv <= 0.7 else "fundamentos"


# --- de que traza contra que traza ----------------------------------------------

def _tiene_posicion(t):
    """La traza trae posicion util. Una sesion importada (.srt) rellena con ceros."""
    if not t or not t.get("pos_x") or not t.get("pos_z"):
        return False
    return (max(t["pos_x"]) - min(t["pos_x"])) > 10.0 and (max(t["pos_z"]) - min(t["pos_z"])) > 10.0


def _mismo_marco(a, b):
    """Las dos trazas comparten sistema de coordenadas.

    Una referencia importada de otro piloto trae otras convenciones de ejes, y sin
    este chequeo el mapa saldria con dos trazados que no se parecen -- una imagen
    falsa, que es peor que no tener imagen.
    """
    if not (_tiene_posicion(a) and _tiene_posicion(b)):
        return False
    ca = ((min(a["pos_x"]) + max(a["pos_x"])) / 2, (min(a["pos_z"]) + max(a["pos_z"])) / 2)
    cb = ((min(b["pos_x"]) + max(b["pos_x"])) / 2, (min(b["pos_z"]) + max(b["pos_z"])) / 2)
    da = max(max(a["pos_x"]) - min(a["pos_x"]), max(a["pos_z"]) - min(a["pos_z"]))
    db = max(max(b["pos_x"]) - min(b["pos_x"]), max(b["pos_z"]) - min(b["pos_z"]))
    if da <= 0 or db <= 0 or abs(da - db) / max(da, db) > 0.25:
        return False
    return (abs(ca[0] - cb[0]) + abs(ca[1] - cb[1])) < 0.20 * max(da, db)


def par_trazas(folder):
    """(tuya, contra, etiqueta, es_referencia_escuela) o None si no alcanza.

    Con referencia guardada: tu MEJOR vuelta contra la referencia de la escuela.
    Sin referencia: tu vuelta TIPICA (la del medio de la tanda) contra tu propia
    mejor vuelta. No es el gap de la escuela y se dice asi en el informe, pero es
    exactamente el material que mas sirve: "ya lo hiciste mas rapido".

    Todo sale de la TANDA que califica (las primeras 8 cronometradas), no de la
    sesion entera: si el mapa dibujara una vuelta distinta de la que produjo el
    gap%, el informe estaria explicando un numero con otra vuelta.
    """
    rec = A.mejor_de_la_tanda(folder)
    if not rec:
        return None
    mejor = A._lap_trace(folder, rec)
    if not mejor or not mejor.get("lap_dist"):
        return None
    ref = A.load_reference_trace(folder)
    if ref and ref.get("lap_dist"):
        return mejor, ref, "la referencia de la escuela", True
    # vuelta tipica: la del medio de la tanda, sacando las que se fueron de rango
    # (un incidente no representa como maneja, y de vuelta tipica mentiria)
    reps = sorted((l for l in A.tanda(folder)
                   if l["lap_time"] <= rec["lap_time"] * 1.03), key=lambda l: l["lap_time"])
    if len(reps) < 3:
        return None
    tt = A._lap_trace(folder, reps[len(reps) // 2])
    if not tt or not tt.get("lap_dist"):
        return None
    return tt, mejor, "tu mejor vuelta de la tanda", False


# --- donde se pierde el tiempo, y por que ---------------------------------------

def _delta_grilla(ta, tb, paso=PASO_M):
    """Delta acumulado de ta contra tb sobre una grilla de distancia. + = vas atras."""
    da, t_a = A._mono(ta["lap_dist"], ta["t"])
    db, t_b = A._mono(tb["lap_dist"], tb["t"])
    fin = min(da[-1], db[-1])
    grid = list(range(0, int(fin), int(paso)))
    if len(grid) < 3:
        return [], []
    return grid, [A._interp(da, t_a, x) - A._interp(db, t_b, x) for x in grid]


def _acelera_en(t, apex_m, umbral=0.5, max_m=250.0):
    """Metro donde vuelve el gas despues del apex. None si no se encuentra."""
    d, th = A._mono(t["lap_dist"], t["throttle"])
    if not d:
        return None
    i = min(range(len(d)), key=lambda k: abs(d[k] - apex_m))
    j = i
    while j < len(d) - 1 and th[j] < umbral and d[j] - d[i] < max_m:
        j += 1
    return d[j] if th[j] >= umbral else None


def _vmin(t, apex_m, ventana=60.0):
    d, s = A._mono(t["lap_dist"], t["speed_kmh"])[:2]
    v = [s[i] for i in range(len(d)) if abs(d[i] - apex_m) < ventana]
    return min(v) if v else None


def _causa(ta, tb, ini, fin, curvas, usadas):
    """Por que se pierde en ese tramo, en palabras y con la accion que lo corrige.

    None si no hay causa identificable: un tramo sin causa es una discrepancia sin
    ruta de accion, que es justo el feedback que mide peor. `usadas` son los apex
    ya reportados, para que una misma curva no se liste dos veces.
    """
    dentro = [c for c in curvas if ini <= c["apex"] <= fin and c["apex"] not in usadas]
    cerca = [c for c in curvas if ini - 60 <= c["apex"] <= fin + 60 and c["apex"] not in usadas]
    cands = dentro or cerca
    ap = max(cands, key=lambda c: c["prom"]) if cands else None

    if ap is None:
        # Tramo de recta (o de una curva ya reportada): casi siempre es la salida de
        # la curva anterior. Solo se emite si esa curva no se uso todavia.
        prev = [c for c in curvas if c["apex"] < ini and c["apex"] not in usadas]
        if not prev:
            return None
        p = max(prev, key=lambda c: c["apex"])
        return {"apex": p["apex"],
                "titulo": f"Trabaja la salida de la curva del metro {_m(p['apex'])}",
                "texto": (f"En la recta del metro {_m(ini)} llegas atrás, y viene de la salida "
                          f"de la curva del metro {_m(p['apex'])}."),
                "accion": (f"Trabaja la salida de la curva del metro {_m(p['apex'])}: entra más "
                           f"lento para poder abrir el gas antes.")}

    v_tuyo, v_ref = _vmin(ta, ap["apex"]), _vmin(tb, ap["apex"])
    dv = (v_ref - v_tuyo) if (v_tuyo is not None and v_ref is not None) else None
    fa, fb = A._punto_frenada(ta, ap["apex"]), A._punto_frenada(tb, ap["apex"])
    d_fren = (fb - fa) if (fa is not None and fb is not None) else None
    ga, gb = _acelera_en(ta, ap["apex"]), _acelera_en(tb, ap["apex"])
    d_gas = (ga - gb) if (ga is not None and gb is not None) else None
    donde = f"la curva del metro {_m(ap['apex'])}"

    # Orden por confianza y por lo ejecutable que es la correccion. "Frena 20 m mas
    # alla" es un metro concreto; "carga mas velocidad de paso" es un consejo sin
    # cuando, y por eso queda de ultimo aunque el sintoma se vea primero.
    if d_fren is not None and d_fren >= 12:
        txt = f"En {donde} frenas {_m(d_fren)} m antes de lo necesario"
        if dv is not None and dv >= 2:
            txt += f", y llegas al ápex {dv:.0f} km/h más lento"
        return {"apex": ap["apex"],
                "titulo": f"Atrasa la frenada de la curva del metro {_m(ap['apex'])}",
                "texto": txt + ".",
                "accion": (f"Atrasa la frenada de {donde} de a 10 m por vez, no de golpe, "
                           f"hasta que el auto deje de llegarte sobrado al ápex.")}
    if d_gas is not None and d_gas >= 15:
        return {"apex": ap["apex"],
                "titulo": f"Abre el gas antes al salir de la curva del metro {_m(ap['apex'])}",
                "texto": f"En {donde} vuelves al gas {_m(d_gas)} m más tarde a la salida.",
                "accion": (f"En {donde}, suelta el volante antes y abre el gas de forma "
                           f"progresiva apenas veas la salida. Entrar más lento ayuda.")}
    if dv is not None and dv >= 3:
        return {"apex": ap["apex"],
                "titulo": f"Sostén velocidad de paso en la curva del metro {_m(ap['apex'])}",
                "texto": (f"En {donde} pasas {dv:.0f} km/h más lento por el ápex "
                          f"({v_tuyo:.0f} contra {v_ref:.0f})."),
                "accion": (f"En {donde}, gira antes para abrir la salida y sostener velocidad "
                           f"de paso. Menos freno arrastrado hasta el ápex.")}
    if d_fren is not None and d_fren <= -12:
        return {"apex": ap["apex"],
                "titulo": f"Adelanta la frenada de la curva del metro {_m(ap['apex'])}",
                "texto": (f"En {donde} frenas {_m(-d_fren)} m más tarde, y lo pagas "
                          f"en la salida."),
                "accion": (f"En {donde} estás pasado de frenada: adelanta el punto 10 m y "
                           f"prioriza salir bien antes que entrar rápido.")}
    return None


def _runs(grid, delta, signo):
    """Tramos contiguos donde el delta crece (signo=+1) o baja (signo=-1)."""
    out, cur = [], None
    for i in range(1, len(delta)):
        inc = (delta[i] - delta[i - 1]) * signo
        if inc > 0:
            if cur is None:
                cur = {"ini": grid[i - 1], "fin": grid[i], "s": inc}
            else:
                cur["fin"], cur["s"] = grid[i], cur["s"] + inc
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return sorted(out, key=lambda r: r["s"], reverse=True)


def perdidas(folder, tope=3, piso=PISO_S):
    """Los tramos donde mas tiempo se pierde, contiguos, con causa y accion.

    Los tramos se arman uniendo segmentos consecutivos en los que el delta crece,
    y no tomando las N casillas de 50 m peores: una curva ocupa mas de 50 m, y
    tres casillas sueltas de la misma curva se leerian como tres problemas.
    """
    vacio = {"tramos": [], "contra": None, "es_ref": False, "ganancias": []}
    par = par_trazas(folder)
    if not par:
        return vacio
    ta, tb, etiqueta, es_ref = par
    grid, delta = _delta_grilla(ta, tb)
    if not grid:
        return dict(vacio, contra=etiqueta, es_ref=es_ref)
    db, sb = A._mono(tb["lap_dist"], tb["speed_kmh"])[:2]
    curvas = A._corners(db, sb)

    tramos, usadas = [], set()
    for r in _runs(grid, delta, +1):
        if r["s"] < piso:
            break
        c = _causa(ta, tb, r["ini"], r["fin"], curvas, usadas)
        if not c:
            continue                       # sin causa no se lista (regla del metodo)
        usadas.add(c["apex"])
        tramos.append({"ini": r["ini"], "fin": r["fin"], "s": round(r["s"], 3),
                       "apex": c["apex"], "titulo": c["titulo"],
                       "texto": c["texto"], "accion": c["accion"]})
        if len(tramos) >= tope:
            break

    # Donde GANAS: el mismo calculo al reves. Solo tiene sentido contra la escuela;
    # contra tu propia mejor vuelta, "ganar" es el ruido de la vuelta tipica.
    ganancias = []
    if es_ref:
        for g in _runs(grid, delta, -1)[:2]:
            if g["s"] >= piso:
                ganancias.append({"ini": g["ini"], "fin": g["fin"], "s": round(g["s"], 3)})
    return {"tramos": tramos, "contra": etiqueta, "es_ref": es_ref, "ganancias": ganancias}


# --- una sola cosa --------------------------------------------------------------

def una_cosa(folder, per, num):
    """La UNICA correccion del informe, elegida para que sea coherente con el foco.

    Preferimos metas de PROCESO (donde frenas, que repitas el punto) sobre metas de
    desempeno (bajar el tiempo): el alumno las controla en la proxima vuelta, y la
    evidencia sobre metas es la parte mas solida de todo lo que revisamos.
    """
    bs = A.braking_struct(folder)
    peor = max(bs["curvas"], key=lambda r: r["sigma_m"]) if (bs and bs["curvas"]) else None
    quiere_consistencia = num.get("foco") in ("consistencia", "las dos parejo", "fundamentos")

    # 1. Falta de referencia de frenada: proceso puro, y ademas es la causa raiz
    #    tipica del alumno rapido pero disperso.
    if peor and peor["sigma_m"] >= 10 and (quiere_consistencia or peor["sigma_m"] >= 20):
        return {
            "titulo": f"Frena siempre en el mismo punto en la curva del metro {_m(peor['apex'])}",
            "porque": (f"Hoy frenas en un rango de {_m(peor['rango_m'])} m entre vueltas "
                       f"(en promedio, en el metro {_m(peor['media_m'])}). No es que frenes mal: "
                       f"es que cada vuelta frenas en otro lado, y por eso el auto te llega "
                       f"distinto cada vez."),
            "como": ("Busca una referencia FIJA antes de esa curva —un cartel, una junta del "
                     "pavimento, un cambio de piso— y frena siempre ahí, aunque las primeras "
                     "vueltas te queden lentas. Primero el mismo punto; después, más tarde."),
            "medida": f"σ de {peor['sigma_m']:.0f} m en el punto de frenada"}

    # 2. Si hay un tramo con causa clara, esa es la cosa.
    if per["tramos"]:
        t = per["tramos"][0]
        return {"titulo": t["titulo"],
                "porque": t["texto"] + f" Ahí se van {t['s']:.2f} s.",
                "como": t["accion"],
                "medida": f"{t['s']:.2f} s en el tramo del metro {_m(t['ini'])}"}

    # 3. Sin tramos y sin dispersion de frenada: la tanda todavia no da material.
    c = num.get("_consist")
    if c and not c["califica"]:
        return {"titulo": "Repite la tanda cuando el ritmo se te estabilice",
                "porque": ("Dentro de esta tanda todavía estás bajando tiempos, así que no se "
                           "puede separar aprender el circuito de ser irregular."),
                "como": ("Da unas vueltas más hasta que dejes de mejorar, y ahí recién "
                         "cronometra las 8 seguidas."),
                "medida": None}
    return {"titulo": "Junta una tanda de 8 vueltas seguidas",
            "porque": ("Sin 8 vueltas cronometradas seguidas no hay consistencia que medir, "
                       "y es la mitad del diagnóstico."),
            "como": "Cinco vueltas de reconocimiento y después 8 seguidas sin parar.",
            "medida": None}


# --- lo que ya te sale ----------------------------------------------------------

def _sesion_previa(folder):
    """La sesion anterior del MISMO auto y pista, si existe y es comparable."""
    combo = A._combo_of(folder)
    if not combo:
        return None
    fs = A._group_combos().get(combo, [])
    obj = os.path.normcase(os.path.abspath(folder))
    idx = next((i for i, f in enumerate(fs)
                if os.path.normcase(os.path.abspath(f)) == obj), None)
    if idx is None or idx == 0:
        return None
    ant = fs[idx - 1]
    da, _ = A.comparabilidad(folder)
    db, _ = A.comparabilidad(ant)
    if not da or not db:
        return None
    if any(da.get(k) != db.get(k) for k in ("mojado", "tc", "abs", "compuesto")
           if da.get(k) is not None and db.get(k) is not None):
        return None                        # condiciones distintas: no es una comparacion
    return ant


def logros(folder, per, num):
    """Lo que ya le sale, medido.

    Nunca elogio suelto: el elogio solo es la peor forma de feedback medida (21% de
    los casos con efecto motivacional NEGATIVO). Cada linea de aca es un numero de
    su propia sesion, no un adjetivo.
    """
    out = []
    c = num.get("_consist")
    if c and c["deriva_pct_vuelta"] <= -0.05 and len(c["tiempos"]) >= 3:
        baja = c["tiempos"][0] - min(c["tiempos"])
        if baja > 0.15:
            out.append(f"Dentro de esta tanda bajaste {baja:.2f} s desde tu primera vuelta: "
                       f"estás aprendiendo el circuito mientras giras.")
    bs = A.braking_struct(folder)
    if bs and bs["curvas"]:
        firmes = [r for r in bs["curvas"] if r["sigma_m"] < 5 and r["n"] >= 3]
        if firmes:
            mejor = min(firmes, key=lambda r: r["sigma_m"])
            out.append(f"En la curva del metro {_m(mejor['apex'])} ya tienes referencia firme: "
                       f"frenas siempre dentro de {_m(max(mejor['rango_m'], 1))} m.")
    for g in per.get("ganancias", [])[:1]:
        out.append(f"Entre el metro {_m(g['ini'])} y el {_m(g['fin'])} le ganas "
                   f"{g['s']:.2f} s a la referencia.")
    ant = _sesion_previa(folder)
    if ant:
        a = A.clean_laps(ant).get("best_lap_time")
        b = A.clean_laps(folder).get("best_lap_time")
        if a and b and a - b > 0.10:
            f = os.path.basename(ant).split("__")[-1][:8]
            fecha = f"{f[6:8]}-{f[4:6]}" if len(f) == 8 and f.isdigit() else f
            out.append(f"Tu mejor vuelta acá bajó {a - b:.2f} s desde la sesión del {fecha}.")
    return out


# --- el mapa --------------------------------------------------------------------

def mapa(folder, per, ancho=680, alto=340, margen=18):
    """SVG del trazado con tu vuelta, la referencia encima y los tramos marcados.

    None si la sesion no trae posicion util (pasa con sesiones importadas de otros
    pilotos, que rellenan los canales con ceros).
    """
    par = par_trazas(folder)
    if not par:
        return None
    ta, tb, _etq, es_ref = par
    if not _tiene_posicion(ta):
        return None
    d, x, z = A._mono(ta["lap_dist"], ta["pos_x"], ta["pos_z"])
    if len(d) < 50:
        return None

    xs, zs = list(x), list(z)
    superpone = es_ref and _mismo_marco(ta, tb)
    d2 = x2 = z2 = None
    if superpone:
        d2, x2, z2 = A._mono(tb["lap_dist"], tb["pos_x"], tb["pos_z"])
        xs += list(x2)
        zs += list(z2)
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    # Escala por EJE y no por el lado mayor: un circuito ancho y bajo como Spielberg
    # se ajustaba a la altura y dejaba el mapa flotando en un tercio del ancho.
    esc = min((ancho - 2 * margen) / ((x1 - x0) or 1.0),
              (alto - 2 * margen) / ((z1 - z0) or 1.0))
    ox = margen + ((ancho - 2 * margen) - (x1 - x0) * esc) / 2
    oz = margen + ((alto - 2 * margen) - (z1 - z0) * esc) / 2

    def proj(px, pz):
        # z crece hacia el norte del mundo y en SVG la y crece hacia abajo: se
        # invierte para que el mapa no salga en espejo respecto del juego.
        return ox + (px - x0) * esc, alto - (oz + (pz - z0) * esc)

    def puntos(dd, xx, zz, a=None, b=None, maximo=600):
        idx = [i for i in range(len(dd)) if a is None or a <= dd[i] <= b]
        if len(idx) > maximo:
            idx = idx[::len(idx) // maximo + 1]
        return " ".join(f"{p[0]:.1f},{p[1]:.1f}" for p in (proj(xx[i], zz[i]) for i in idx))

    partes = []
    if superpone:
        partes.append(f'<polyline class="ref" points="{puntos(d2, x2, z2)}"/>')
    partes.append(f'<polyline class="tuya" points="{puntos(d, x, z)}"/>')
    for n, tr in enumerate(per["tramos"], 1):
        pts = puntos(d, x, z, tr["ini"], tr["fin"])
        if not pts:
            continue
        partes.append(f'<polyline class="perdida" points="{pts}"/>')
        i = min(range(len(d)), key=lambda k: abs(d[k] - (tr["ini"] + tr["fin"]) / 2))
        px, pz = proj(x[i], z[i])
        partes.append(f'<circle class="pin" cx="{px:.1f}" cy="{pz:.1f}" r="10"/>'
                      f'<text class="pin-n" x="{px:.1f}" y="{pz + 4:.1f}">{n}</text>')
    px, pz = proj(x[0], z[0])
    partes.append(f'<circle class="meta" cx="{px:.1f}" cy="{pz:.1f}" r="4.5"/>')
    return {"svg": "".join(partes), "ancho": ancho, "alto": alto, "superpone": superpone}


# --- armado ---------------------------------------------------------------------

def _variante(meta):
    """La variante SOLO si agrega algo al nombre de la pista ('Spielberg_Modern' ->
    'Modern'). Repetir el circuito entre parentesis es ruido."""
    var = (meta.get("track_variation_tr") or meta.get("track_variation") or "").strip()
    track = (meta.get("track_tr") or meta.get("track") or "").strip()
    if not var or var == track:
        return ""
    if track and var.lower().startswith(track.lower()):
        var = var[len(track):].lstrip("_- ")
    return var.replace("_", " ").strip()


def armar(folder, nivel=None, alumno=None):
    """Todo el informe como datos. El HTML es solo una vista de esto."""
    meta, _laps = A._load(folder)
    num = numeros(folder)
    num["foco"] = foco(num["gap"]["valor"] if num["gap"] else None,
                       num["cv"]["valor"] if num["cv"] else None)
    per = perdidas(folder)
    return {
        "alumno": alumno,
        "nivel": nivel,
        "sesion": os.path.basename(os.path.abspath(folder)),
        "auto": (meta.get("car_tr") or meta.get("car") or "?").replace("_", " "),
        "pista": (meta.get("track_tr") or meta.get("track") or "?").replace("_", " "),
        "variante": _variante(meta),
        "fecha": (meta.get("started") or "")[:10],
        "generado": datetime.datetime.now().strftime("%d-%m-%Y"),
        "numeros": num,
        "perdidas": per,
        "cosa": una_cosa(folder, per, num),
        "logros": logros(folder, per, num) if nivel != 3 else [],
        "mapa": mapa(folder, per),
        "invalidacion": A.tasa_invalidacion(folder),
    }


# --- vista ----------------------------------------------------------------------

_CSS = """
:root{
  --papel:#F3F4F7; --tarjeta:#FFFFFF; --tinta:#14181D; --tinta2:#5C646F;
  --linea:#DBDEE4; --acento:#0F5C5B; --acento-suave:#E3EDEC;
  --perdida:#A8321E; --ganancia:#2E6F4E; --ref:#98A0AC;
}
*{box-sizing:border-box}
body{margin:0;background:var(--papel);color:var(--tinta);
  font:15px/1.55 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-print-color-adjust:exact;print-color-adjust:exact}
.hoja{max-width:800px;margin:0 auto;padding:26px 20px 60px}
h1,h2{font-family:Georgia,"Iowan Old Style","Times New Roman",serif;font-weight:600;
  text-wrap:balance;margin:0}
.num{font-variant-numeric:tabular-nums}

header{border-bottom:3px solid var(--tinta);padding-bottom:14px;margin-bottom:22px;
  display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap}
header h1{font-size:27px;letter-spacing:-.01em}
header .sub{color:var(--tinta2);font-size:13px;margin-top:4px}
header .sello{font-size:11px;text-transform:uppercase;letter-spacing:.14em;
  color:var(--acento);text-align:right;line-height:1.7}

section{margin:0 0 26px}
.ojo{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:var(--tinta2);
  display:flex;align-items:center;gap:10px;margin-bottom:12px}
.ojo::after{content:"";flex:1;height:1px;background:var(--linea)}
.ojo b{color:var(--acento);font-weight:600}

.dos{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
.caja{background:var(--tarjeta);border:1px solid var(--linea);border-radius:3px;padding:16px 18px}
.caja .que{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:var(--tinta2)}
.caja .cifra{font-size:42px;font-weight:700;line-height:1.05;margin:6px 0 2px;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.caja .cifra small{font-size:20px;font-weight:600;color:var(--tinta2)}
.caja .banda{font-size:14px;color:var(--acento);font-weight:600}
.caja .pie{font-size:12px;color:var(--tinta2);margin-top:9px;line-height:1.45}
.caja.falta .cifra{color:var(--tinta2);font-size:26px;letter-spacing:0}

.regla{display:flex;gap:2px;margin:13px 0 5px}
.regla i{height:7px;flex:1;background:var(--linea);border-radius:1px}
.regla i.on{background:var(--acento)}
.reglaetq{display:flex;font-size:10px;color:var(--tinta2);gap:2px}
.reglaetq span{flex:1;text-align:center;line-height:1.25}
.reglaetq span.on{color:var(--tinta);font-weight:700}

.aviso{background:#FBF2E4;border-left:3px solid #B5762A;padding:10px 13px;font-size:13px;
  margin-top:12px;border-radius:0 3px 3px 0}
.aviso b{color:#8A5719}

ol.tramos{list-style:none;padding:0;margin:0;counter-reset:t}
ol.tramos li{background:var(--tarjeta);border:1px solid var(--linea);border-radius:3px;
  padding:14px 16px 14px 52px;position:relative;margin-bottom:9px}
ol.tramos li::before{counter-increment:t;content:counter(t);position:absolute;left:15px;top:14px;
  width:23px;height:23px;border-radius:50%;background:var(--perdida);color:#fff;
  font-size:13px;font-weight:700;display:flex;align-items:center;justify-content:center}
ol.tramos .cab{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
ol.tramos .donde{font-weight:600;font-size:13px;color:var(--tinta2)}
ol.tramos .cuanto{color:var(--perdida);font-weight:700;font-variant-numeric:tabular-nums;
  white-space:nowrap}
ol.tramos .dice{margin:5px 0 0}
ol.tramos .hacer{margin:8px 0 0;padding-top:8px;border-top:1px dashed var(--linea);
  font-size:14px;color:var(--acento)}

figure{margin:0;background:var(--tarjeta);border:1px solid var(--linea);border-radius:3px;
  padding:8px}
svg{display:block;width:100%;height:auto}
svg polyline{fill:none;stroke-linejoin:round;stroke-linecap:round}
svg .ref{stroke:var(--ref);stroke-width:7;opacity:.45}
svg .tuya{stroke:var(--tinta);stroke-width:2.2}
svg .perdida{stroke:var(--perdida);stroke-width:5}
svg .pin{fill:var(--perdida)}
svg .pin-n{fill:#fff;font-size:12px;font-weight:700;text-anchor:middle;
  font-family:-apple-system,"Segoe UI",sans-serif}
svg .meta{fill:var(--acento)}
figcaption{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--tinta2);
  padding:8px 6px 2px}
figcaption i{display:inline-block;width:15px;height:3px;vertical-align:middle;margin-right:5px}

.cosa{background:var(--acento);color:#fff;border-radius:3px;padding:20px 22px}
.cosa .ojo{color:rgba(255,255,255,.72)}
.cosa .ojo::after{background:rgba(255,255,255,.28)}
.cosa .ojo b{color:#fff}
.cosa h2{font-size:22px;color:#fff;margin-bottom:9px}
.cosa p{margin:0 0 9px;color:rgba(255,255,255,.92)}
.cosa .como{background:rgba(255,255,255,.13);border-radius:3px;padding:11px 13px;margin:0;
  font-size:14px}
.cosa .medida{font-size:11px;text-transform:uppercase;letter-spacing:.1em;
  color:rgba(255,255,255,.6);margin-top:11px}

ul.logros{list-style:none;padding:0;margin:0;background:var(--tarjeta);
  border:1px solid var(--linea);border-radius:3px;padding:4px 16px}
ul.logros li{padding:10px 0 10px 24px;border-bottom:1px solid var(--linea);position:relative;
  font-size:14px}
ul.logros li:last-child{border-bottom:0}
ul.logros li::before{content:"";position:absolute;left:2px;top:17px;width:9px;height:9px;
  border-radius:50%;background:var(--ganancia)}

table.permisos{width:100%;border-collapse:collapse;font-size:13px;background:var(--tarjeta);
  border:1px solid var(--linea)}
table.permisos th,table.permisos td{text-align:left;padding:9px 11px;
  border-bottom:1px solid var(--linea)}
table.permisos tr:last-child td{border-bottom:0}
table.permisos th{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:var(--tinta2);
  font-weight:600}
table.permisos tr.mio{background:var(--acento-suave)}
table.permisos tr.mio td{font-weight:600}
table.permisos td.n{font-weight:700;width:34px;font-variant-numeric:tabular-nums}

.nota{font-size:12px;color:var(--tinta2);line-height:1.5;margin-top:10px}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--linea);
  font-size:11px;color:var(--tinta2);line-height:1.6}

@media (max-width:620px){.dos{grid-template-columns:1fr}header{flex-direction:column;
  align-items:flex-start}header .sello{text-align:left}}
@media print{
  body{background:#fff}
  .hoja{max-width:none;padding:0}
  section,figure,.cosa,ol.tramos li{break-inside:avoid}
}
"""


def _esc(s):
    return _html.escape(str(s), quote=False)


def _regla(valor, bandas, unidad="%"):
    """La escala dibujada al lado del numero: las bandas y la que te toca marcada.

    Va pegada al numero a proposito. Un porcentaje suelto no le dice nada a alguien
    que nunca vio la rubrica, y la banda sola es un veredicto sin contexto.
    """
    activa = _banda(valor, bandas) if valor is not None else None
    barras = "".join(f'<i class="{"on" if n == activa else ""}"></i>' for n, _lo, _hi in bandas)
    etq = "".join(f'<span class="{"on" if n == activa else ""}">{_esc(n)}</span>'
                  for n, _lo, _hi in bandas)
    cortes = " · ".join(f"{_esc(n)} hasta {hi:g}{unidad}" for n, _lo, hi in bandas[:-1])
    return (f'<div class="regla">{barras}</div><div class="reglaetq">{etq}</div>'
            f'<div class="pie">escala: {cortes}</div>')


def _motivo(faltan, clave, poromision):
    m = next((f for f in faltan if f.startswith(clave + ":")), "")
    return m.split(":", 1)[1].strip() if ":" in m else poromision


def _caja_gap(g, faltan):
    if not g:
        return ('<div class="caja falta"><div class="que">Ritmo — gap%</div>'
                '<div class="cifra">sin dato todavía</div><div class="pie">'
                f'{_esc(_motivo(faltan, "ritmo", "falta la referencia del combo"))}. '
                'Se calcula apenas tu instructor grabe la vuelta de referencia; '
                'no se rellena con otra cosa.</div></div>')
    aviso = f'<div class="aviso"><b>Ojo:</b> {_esc(g["aviso"])}.</div>' if g.get("aviso") else ""
    return ('<div class="caja"><div class="que">Ritmo — gap%</div>'
            f'<div class="cifra num">{g["valor"]:+.2f}<small>%</small></div>'
            f'<div class="banda">{_esc(g["banda"])}</div>'
            + _regla(max(g["valor"], 0.0), BANDAS_GAP) +
            f'<div class="pie">tu mejor vuelta de la tanda {_esc(_t(g["tuyo_s"]))} contra la '
            f'referencia {_esc(_t(g["ref_s"]))}. Cuenta la tanda de 8 y no tu mejor vuelta '
            f'del día: si valiera la mejor de todas, el que gira más saldría mejor sin '
            f'manejar mejor.</div>{aviso}</div>')


def _caja_cv(c, faltan):
    if not c:
        return ('<div class="caja falta"><div class="que">Consistencia — CV%</div>'
                '<div class="cifra">sin dato todavía</div><div class="pie">'
                f'{_esc(_motivo(faltan, "consistencia", "no hay tanda suficiente"))}.</div></div>')
    pie = (f'sobre {c["n"]} vueltas · mediana {_esc(_t(c["mediana_s"]))} · '
           f'mejor {_esc(_t(c["mejor_s"]))}')
    if c["incidentes"]:
        pie += f' · {c["incidentes"]} vuelta(s) apartada(s) como incidente'
    extra = ""
    if c["degradando"] and c["cv_dest"] < c["valor"] * 0.6:
        extra = ('<div class="aviso"><b>No es falta de repetibilidad:</b> tus vueltas se van '
                 f'cayendo. Sin esa tendencia el CV baja a {c["cv_dest"]:.2f}%. Eso es '
                 'degradación (goma, combustible o cansancio) y se trabaja distinto.</div>')
    return ('<div class="caja"><div class="que">Consistencia — CV%</div>'
            f'<div class="cifra num">{c["valor"]:.2f}<small>%</small></div>'
            f'<div class="banda">{_esc(c["banda"])}</div>'
            + _regla(c["valor"], BANDAS_CV) +
            f'<div class="pie">{pie}.</div>{extra}</div>')


def _sec_tramos(d):
    per, nivel = d["perdidas"], d["nivel"]
    if not per["tramos"]:
        return ('<section><div class="ojo"><b>2</b> Dónde se te va el tiempo</div>'
                '<div class="caja"><p style="margin:0">Esta tanda todavía no da un tramo con '
                'causa clara. No es que manejes parejo en todos lados: es que la diferencia '
                'entre tus vueltas está repartida y no se puede señalar un lugar sin '
                'inventarlo.</p></div></section>')
    # Nivel 1 recibe UNO. No es esconderle informacion: con ocho correcciones no
    # aplica ninguna, y el mismo informe que sirve a un Nivel 3 dana a un Nivel 1.
    tramos = per["tramos"][:1] if nivel == 1 else per["tramos"]
    titulo = "Dónde hay más para ganar" if nivel == 1 else "Dónde se te va el tiempo"
    filas = "".join(
        f'<li><div class="cab"><span class="donde">metro {_esc(_m(t["ini"]))} '
        f'al {_esc(_m(t["fin"]))}</span>'
        f'<span class="cuanto">{t["s"]:.2f} s</span></div>'
        f'<p class="dice">{_esc(t["texto"])}</p>'
        f'<p class="hacer"><b>Qué hacer:</b> {_esc(t["accion"])}</p></li>' for t in tramos)
    contra = (f'Medido contra {_esc(per["contra"])}.' if per["es_ref"] else
              f'Medido contra {_esc(per["contra"])}: no es el gap contra la escuela, es lo que '
              f'dejas entre tu vuelta típica y tu propia mejor vuelta. Ya lo hiciste más rápido.')
    return (f'<section><div class="ojo"><b>2</b> {titulo}</div>'
            f'<ol class="tramos">{filas}</ol>'
            f'<p class="nota">{contra} Las curvas se nombran por su distancia en metros y no por '
            f'número, porque las herramientas del dash las numeran distinto entre sí. El delta se '
            f'interpola sobre una grilla de {int(PASO_M)} m: sirve para ubicar dónde se pierde, '
            f'no como cronómetro exacto al milésimo.</p></section>')


def _sec_mapa(d):
    mp = d["mapa"]
    if not mp:
        return ""
    leyenda = ['<span><i style="background:#14181D"></i>tu vuelta</span>']
    if mp["superpone"]:
        leyenda.append('<span><i style="background:#98A0AC"></i>referencia</span>')
    if d["perdidas"]["tramos"]:
        leyenda.append('<span><i style="background:#A8321E"></i>dónde pierdes</span>')
    leyenda.append('<span><i style="background:#0F5C5B"></i>meta</span>')
    return ('<section><div class="ojo"><b>3</b> El mapa de tu vuelta</div>'
            f'<figure><svg viewBox="0 0 {mp["ancho"]} {mp["alto"]}" role="img" '
            f'aria-label="Trazado del circuito con los tramos de pérdida marcados">'
            f'{mp["svg"]}</svg><figcaption>{"".join(leyenda)}</figcaption></figure></section>')


def _sec_cosa(d):
    c = d["cosa"]
    medida = f'<div class="medida">lo medido: {_esc(c["medida"])}</div>' if c.get("medida") else ""
    return ('<section class="cosa"><div class="ojo"><b>4</b> Una sola cosa hasta la próxima clase'
            f'</div><h2>{_esc(c["titulo"])}</h2><p>{_esc(c["porque"])}</p>'
            f'<p class="como"><b>Cómo:</b> {_esc(c["como"])}</p>{medida}</section>')


def _sec_nivel(d):
    # sin backslash dentro de la f-string: eso recien es legal en Python 3.12 y el
    # resto del toolkit tiene que poder correr en el interprete que tenga el alumno
    filas = "".join(
        "<tr" + (' class="mio"' if d["nivel"] == n else "") + f'><td class="n">{n}</td>'
        f'<td>{_esc(coach)}</td><td>{_esc(adel)}</td><td>{_esc(fmt)}</td></tr>'
        for n, coach, adel, fmt in PERMISOS)
    cab = (f'Hoy estás en <b>Nivel {d["nivel"]}</b>. Esto es lo que eso te permite:'
           if d["nivel"] else
           'Tu nivel lo define tu instructor con la pauta de observación. Esto es lo que cada '
           'nivel te permite:')
    inv = d.get("invalidacion")
    limpieza = ""
    if inv:
        limpieza = (f'<p class="nota">Limpieza medida en esta sesión: {inv["invalidas"]} de '
                    f'{inv["n"]} vueltas invalidadas por límites de pista ({inv["pct"]:.0f}%). '
                    f'Es uno de los tres antecedentes del permiso.</p>')
    return ('<section><div class="ojo"><b>5</b> Tu nivel, en lo que puedes hacer</div>'
            f'<p style="margin:0 0 11px">{cab}</p>'
            '<table class="permisos"><tr><th></th><th>Coach</th><th>Adelantamiento</th>'
            f'<th>Formato</th></tr>{filas}</table>'
            '<p class="nota">El nivel no lo dan los números de arriba: lo abren la limpieza '
            'medida, la percepción del entorno que observa tu instructor y una evaluación en '
            'vivo con un instructor distinto del tuyo. Un piloto rápido no sube por ser rápido. '
            'Y de Nivel 1 y 2 no se baja nunca: el que está empezando no tiene que tener miedo.'
            f'</p>{limpieza}</section>')


def render(d):
    """Los datos como HTML autocontenido. Sin red: se abre igual sin internet."""
    n = d["numeros"]
    quien = f'{_esc(d["alumno"])} · ' if d.get("alumno") else ""
    variante = f' · {_esc(d["variante"])}' if d["variante"] else ""
    cabecera = ('<header><div>'
                f'<h1>{_esc(d["pista"])}{variante} · {_esc(d["auto"])}</h1>'
                f'<div class="sub">{quien}sesión del {_esc(d["fecha"] or "?")}'
                + (f' · {_esc(n["condiciones"])}' if n.get("condiciones") else "") +
                '</div></div>'
                '<div class="sello">Escuela de Conducción<br>Deportiva AMS2 Chile<br>'
                f'informe del {_esc(d["generado"])}</div></header>')

    if n["mezclada"]:
        # El gate de comparabilidad manda: con condiciones mezcladas cualquier numero
        # sale plausible y equivocado, asi que no se emite ninguno.
        cuerpo = ('<section><div class="aviso"><b>Esta sesión no se puede medir.</b> Cambiaron '
                  'las condiciones a mitad de tanda (estado de pista, compuesto o ayudas), así '
                  'que tus vueltas no son comparables entre sí y cualquier número saldría '
                  'creíble y equivocado. Repite la tanda sin tocar nada.</div></section>')
        return _pagina(cabecera + cuerpo + _sec_nivel(d), d)

    foco_txt = ""
    if n.get("foco"):
        foco_txt = (f'<p class="nota"><b>En qué se trabaja primero:</b> {_esc(n["foco"])}. Sale '
                    'de cruzar los dos números de arriba, y no es una nota: decide el foco de '
                    'las próximas semanas.</p>')
    partes = [cabecera,
              '<section><div class="ojo"><b>1</b> Tus dos números</div><div class="dos">'
              + _caja_gap(n["gap"], n["faltan"]) + _caja_cv(n["cv"], n["faltan"])
              + f'</div>{foco_txt}</section>']
    if d["logros"]:
        li = "".join(f"<li>{_esc(x)}</li>" for x in d["logros"])
        partes.append('<section><div class="ojo">Lo que ya te sale</div>'
                      f'<ul class="logros">{li}</ul></section>')
    partes += [_sec_tramos(d), _sec_mapa(d), _sec_cosa(d), _sec_nivel(d)]
    return _pagina("".join(partes), d)


def _pagina(cuerpo, d):
    pie = ('<footer>Generado desde tu propia telemetría. Todo lo que ves está medido en tus '
           'vueltas: nada está comparado con otro alumno, y no existe una tabla que los ordene. '
           'Si algo no calza con lo que sentiste en pista, eso es material para el debrief — la '
           'diferencia entre lo que crees y lo que muestra el dato es la parte que más enseña.'
           f'<br>{_esc(d["sesion"])}</footer>')
    return ('<!doctype html>\n<html lang="es"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Informe · {_esc(d["pista"])} · {_esc(d["auto"])}</title>'
            f'<style>{_CSS}</style></head><body><div class="hoja">'
            f'{cuerpo}{pie}</div></body></html>\n')


# --- cli ------------------------------------------------------------------------

def generar(folder, nivel=None, alumno=None, salida=None):
    """Genera el informe y devuelve la ruta escrita."""
    d = armar(folder, nivel=nivel, alumno=alumno)
    if not salida:
        os.makedirs(SALIDA, exist_ok=True)
        salida = os.path.join(SALIDA, f"{d['sesion']}__informe.html")
    with open(salida, "w", encoding="utf-8") as f:
        f.write(render(d))
    return salida


def main():
    ap = argparse.ArgumentParser(description="Informe de una pagina para el alumno.")
    ap.add_argument("carpeta", help="carpeta de la sesion (telemetry/...)")
    ap.add_argument("--nivel", type=int, choices=(1, 2, 3), default=None,
                    help="nivel del alumno. Lo pone el instructor: el informe no lo deduce.")
    ap.add_argument("--alumno", default=None, help="nombre del alumno para la cabecera")
    ap.add_argument("--salida", default=None, help="ruta del HTML de salida")
    ap.add_argument("--abrir", action="store_true", help="abrirlo al terminar")
    a = ap.parse_args()
    if not os.path.isdir(a.carpeta):
        print(f"no existe la carpeta: {a.carpeta}")
        return 2
    ruta = generar(a.carpeta, nivel=a.nivel, alumno=a.alumno, salida=a.salida)
    print(f"informe: {ruta}")
    if a.abrir:
        import webbrowser
        webbrowser.open("file:///" + ruta.replace("\\", "/"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
