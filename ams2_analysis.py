#!/usr/bin/env python3
"""Capa de analisis para el VISOR de telemetria: stints, mapa de pista y trazas
alineadas por distancia.

Lo que graba `ams2_telemetry.py` ya trae todo lo necesario (95 canales a 50 Hz,
incluida la posicion en el mundo). Este modulo no mide nada nuevo: ORDENA lo
grabado para que el visor lo pueda dibujar y comparar.

Tres ideas, y las tres importan:

1. STINT = tanda entre boxes. Se deriva de `timeline.jsonl`, que trae UNA linea
   por CADA vuelta cruzada (out / flying / invalid / pit) mas los eventos de
   pit_in / pit_out. Una tanda abre en la vuelta de salida (`out`) y cierra en la
   vuelta en que se entro a boxes (`pit`).

2. LA IDENTIDAD DE UNA VUELTA NO ES SU NUMERO. Al volver al garage AMS2 REINICIA
   el contador: una practica de Road Atlanta tiene seis tandas y tres "vuelta 1"
   distintas. La identidad es el `uid` monotonico del recorder. `timeline.jsonl`
   no lo trae en las sesiones viejas, asi que se reconstruye emparejando en orden
   contra `summary.jsonl` (que si lo trae, junto al nombre de la traza).

3. COMPARAR ES POR DISTANCIA, NO POR TIEMPO. Dos vueltas duran distinto, asi que
   superponerlas por tiempo las desfasa mas y mas. Todo se remuestrea a una
   grilla de metros y ahi si se pueden restar: el delta en el metro X es
   `t_A(X) - t_B(X)`, que es exactamente "cuanto tiempo llevo perdido aca".
   El delta es EXACTO (ambas vueltas son propias, a 50 Hz), no una estimacion.

Solo stdlib. Los primitivos de lectura/interpolacion se reusan de
`tools/analyze_telemetry.py` para no tener dos verdades del mismo dato.
"""
from __future__ import annotations

import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TELEM = os.path.join(HERE, "telemetry")
sys.path.insert(0, os.path.join(HERE, "tools"))

import analyze_telemetry as AT   # noqa: E402  (necesita el sys.path de arriba)
import ams2_srt                  # noqa: E402  (importaciones de otros pilotos)

# Las sesiones de OTROS pilotos (.srt importados) viven aparte de telemetry/ y se
# listan igual en el visor. Separarlas no es cosmetico: telemetry/ es el corpus con
# el que tyre_replay valida los analizadores, y una sesion ajena trae canales que
# nosotros no tenemos (van en 0.0) que envenenarian ese corpus en silencio.
RAICES = (TELEM, ams2_srt.IMPORT_DIR)


def _dirs():
    """Todas las carpetas de sesion, propias e importadas."""
    out = []
    for raiz in RAICES:
        if os.path.isdir(raiz):
            out += [os.path.join(raiz, d) for d in os.listdir(raiz)
                    if os.path.isdir(os.path.join(raiz, d))]
    return out

# Canales que el visor pide por defecto. `t` NO es opcional: es el reloj con el
# que se calcula el delta entre vueltas. `pos_x`/`pos_z` tampoco son un extra:
# son el RECORRIDO REAL de esa vuelta, y sin ellos el mapa solo puede dibujar un
# trazado (el de la vuelta de referencia) y dos trazadas distintas se ven iguales.
CANALES_VISOR = ("t", "speed_kmh", "throttle", "brake", "gear", "rpm", "steer",
                 "pos_x", "pos_z")
PASO_M = 2.0          # grilla de distancia del visor (2 m ~= 36 ms a 200 km/h)
MAPA_PUNTOS = 900     # puntos del trazado que se mandan al navegador


# ---------------------------------------------------------------- sesiones ---
def _meta(folder: str) -> dict:
    p = os.path.join(folder, "session.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def sesiones(limite: int = 40) -> list[dict]:
    """Sesiones grabadas, de la mas reciente a la mas vieja."""
    out = []
    for d in sorted(_dirs(), key=os.path.getmtime, reverse=True)[:limite]:
        m = _meta(d)
        vueltas = AT._read_jsonl(os.path.join(d, "summary.jsonl"))
        out.append({
            "carpeta": os.path.basename(d),
            "pista": m.get("track"), "variante": m.get("track_variation"),
            "auto": m.get("car"), "tipo": m.get("session"),
            "largo_m": m.get("track_length_m"),
            "inicio": m.get("started"),
            "origen": m.get("origen"), "piloto": m.get("piloto"),
            "vueltas": len(vueltas),
            "mejor": min((l["lap_time"] for l in vueltas if l.get("lap_time")), default=None),
        })
    return out


def _carpeta(nombre: str) -> str:
    """Resuelve el nombre de una sesion a ruta absoluta, sin dejar salir de telemetry/.

    El nombre viene de una peticion HTTP, asi que se valida como frontera de
    confianza: cualquier cosa que no sea un hijo directo de telemetry/ se
    rechaza (un `..` colado serviria cualquier archivo del disco por la LAN).
    """
    if not nombre or os.path.isabs(nombre) or os.sep in nombre or "/" in nombre or ".." in nombre:
        raise ValueError("nombre de sesion invalido")
    for raiz in RAICES:
        ruta = os.path.join(raiz, nombre)
        if os.path.isdir(ruta):
            return ruta
    raise ValueError("no existe esa sesion")


# ------------------------------------------------------------------ vuelta ---
def _vueltas(folder: str) -> list[dict]:
    """Todas las vueltas cruzadas, en orden, con su traza resuelta si la tiene.

    `timeline.jsonl` tiene la ESTRUCTURA (que vuelta fue de salida, cual entro a
    boxes, cual se invalido) y `summary.jsonl` tiene la IDENTIDAD y el archivo.
    Se emparejan en orden: cada vuelta `flying` de la linea de tiempo consume el
    siguiente registro del resumen. Se cruza ademas por (numero, tiempo) para
    detectar un desfase en vez de arrastrarlo callado.
    """
    tl = AT._read_jsonl(os.path.join(folder, "timeline.jsonl"))
    res = AT._read_jsonl(os.path.join(folder, "summary.jsonl"))
    secs = {r.get("uid"): r for r in AT._read_jsonl(os.path.join(folder, "sectors.jsonl"))}
    laps = [r for r in tl if r.get("type") == "lap"]

    # Sesiones viejas (anteriores a timeline.jsonl): el resumen es lo unico que hay.
    if not laps:
        return [{
            "uid": r.get("uid"), "vuelta": r.get("lap"), "tiempo": r.get("lap_time"),
            "tipo": "flying", "valida": True, "traza": r.get("trace"),
            "sectores": r.get("sectors"), "compuesto": r.get("compound"),
            "combustible": r.get("fuel_used"),
        } for r in res]

    def _existe(nombre):
        return bool(nombre) and os.path.exists(os.path.join(folder, nombre))

    out, k = [], 0
    for r in laps:
        v = {
            "uid": r.get("uid"), "vuelta": r.get("lap"), "tiempo": r.get("lap_time"),
            "tipo": r.get("kind"), "valida": not r.get("invalid"),
            "boxes": bool(r.get("pit")), "salida": bool(r.get("out")),
            "traza": None, "sectores": None,
            "combustible": (round(r["fuel_start"] - r["fuel_end"], 2)
                            if r.get("fuel_start") is not None and r.get("fuel_end") is not None
                            else None),
        }
        # sesiones nuevas: la linea de tiempo ya trae uid y archivo, incluido el
        # de las vueltas invalidadas (X###), que el resumen nunca lista.
        #
        # Se COMPRUEBA que el archivo exista, en los DOS caminos. El nombre se
        # anota antes de volcar el .gz, asi que hay una ventana; y si el volcado
        # falla (disco lleno) el nombre queda apuntando a un fantasma para
        # siempre. Mirando la sesion en vivo, mientras giras, esa ventana se
        # puede pisar de verdad.
        if _existe(r.get("trace")):
            v["traza"] = r["trace"]
        if r.get("kind") == "flying" and k < len(res):
            s = res[k]
            # sesiones viejas (sin uid en la linea de tiempo): emparejar en orden.
            # El desfase se detecta, no se asume: si no cuadran numero y tiempo,
            # esta vuelta se queda sin traza en vez de mostrar la de otra.
            if s.get("lap") == r.get("lap") and abs((s.get("lap_time") or 0) - (r.get("lap_time") or 0)) < 0.002:
                v["uid"] = v["uid"] or s.get("uid")
                if not v["traza"] and _existe(s.get("trace")):
                    v["traza"] = s["trace"]
                v["sectores"] = s.get("sectors")
                v["compuesto"] = s.get("compound")
                k += 1
        if v["uid"] in secs:
            v["sectores"] = secs[v["uid"]].get("sectors") or v["sectores"]
            v["sec_validos"] = secs[v["uid"]].get("sec_valid")
        out.append(v)
    return out


def stints(folder: str) -> list[dict]:
    """Tandas de la sesion: la corrida de pista entre una salida de boxes y la
    vuelta siguiente.

    Las vueltas de tipo `pit` NO son parte de ninguna tanda: son el borde. Y
    tienen que quedar fuera de verdad, no solo marcadas. Entrar al garage y
    volver a salir cruza meta un par de veces con `pit` y `salida` a la vez, y
    tratando cada una como inicio de tanda esta misma practica de Road Atlanta
    daba ONCE tandas -- cinco de ellas de una sola vuelta, que nunca existieron.
    Son seis corridas reales.
    """
    out, cur = [], None
    for v in _vueltas(folder):
        if v["tipo"] == "pit":            # borde: cierra la corrida, no abre otra
            if cur is not None:
                cur["cerrado"] = True
            cur = None
            continue
        if cur is None or v.get("salida"):
            if cur is not None:
                cur["cerrado"] = True     # la abrio una salida de boxes: la anterior termino
            cur = {"n": len(out) + 1, "vueltas": [], "cerrado": False}
            out.append(cur)
        cur["vueltas"].append(v)
    for s in out:
        crono = [v["tiempo"] for v in s["vueltas"]
                 if v["tipo"] == "flying" and v.get("tiempo")]
        s["n_vueltas"] = len(s["vueltas"])
        s["n_cronometradas"] = len(crono)
        s["mejor"] = min(crono, default=None)
        s["con_traza"] = sum(1 for v in s["vueltas"] if v.get("traza"))
        s["gasto_l"] = round(sum(v["combustible"] or 0 for v in s["vueltas"]), 2)
    return out


# ---------------------------------------------- remuestreo por distancia ---
def _rejilla(dist, cols, paso, largo=None):
    """Remuestrea `cols` sobre una grilla de distancia de `paso` metros.

    Una sola pasada aprovechando que ambas series estan ordenadas. Devuelve
    (metros, columnas). Descarta el envoltorio de meta con `_mono`, que es lo que
    evita que la vuelta "vuelva" al metro 0 en medio del grafico.
    """
    d, *cs = AT._mono(dist, *cols)
    if len(d) < 2:
        return [], [[] for _ in cols]
    fin = largo if largo and largo > d[-1] * 0.9 else d[-1]
    n = int(fin // paso) + 1
    xs = [i * paso for i in range(n)]
    out = [[0.0] * n for _ in cs]
    j = 0
    for i, x in enumerate(xs):
        while j + 2 < len(d) and d[j + 1] < x:
            j += 1
        span = d[j + 1] - d[j]
        f = (x - d[j]) / span if span else 0.0
        f = 0.0 if f < 0 else (1.0 if f > 1 else f)
        for k, c in enumerate(cs):
            out[k][i] = c[j] + f * (c[j + 1] - c[j])
    return xs, out


def _cerrar_en_meta(data, canales, largo, tiempo):
    """Completa la vuelta hasta la linea con el dato del CRONOMETRO.

    La traza casi nunca llega a meta: AMS2 congela la shared memory al cruzar y
    el recorder guarda los ultimos frames repetidos, asi que la vuelta termina
    unos metros antes. Medido en 70 vueltas de 25 sesiones: mediana 0.8 m, p90
    1.7 m -- despreciable. Pero se midio una de carrera cortada a 12.5 m de
    meta, y esos 12.5 m a 178 km/h son 227 ms que aparecian como un delta falso
    contra otra vuelta cortada en otro punto.

    No hace falta estimarlos: el tiempo de vuelta se conoce exacto. Se agrega UNA
    muestra de cierre en (largo de pista, tiempo de vuelta) y el resto de los
    canales sostiene su ultimo valor. Devuelve ademas cuanto tiempo hubo que
    completar, que es la medida de cuan truncada venia la traza.
    """
    d = data["lap_dist"]
    if not largo or not tiempo or not d:
        return data, 0.0
    falta_m = largo - max(d)
    falta_s = tiempo - max(data["t"])
    # un hueco grande no es truncamiento: es otra cosa (vuelta parcial, reset a
    # boxes). Ahi NO se inventa el cierre, se deja la vuelta como esta.
    if not (0 < falta_m <= 60 and 0 < falta_s <= 5):
        return data, 0.0
    out = {c: list(v) for c, v in data.items() if c in canales or c == "lap_dist"}
    for c, v in out.items():
        v.append(largo if c == "lap_dist" else (tiempo if c == "t" else v[-1]))
    return out, round(falta_s, 3)


def traza(folder: str, nombre: str, canales=CANALES_VISOR, paso=PASO_M) -> dict:
    """Una vuelta remuestreada por distancia, lista para graficar.

    Trae ademas `eventos`: los cuatro puntos de cada curva (frenada, giro, apex,
    a fondo) de ESTA vuelta. Se calculan ACA y no en el navegador por tres
    razones concretas:

      · UNA sola definicion. "Donde frena" tiene un criterio fino (caminar hacia
        atras desde el apex tolerando modulacion) que ya vive en
        `analyze_telemetry._punto_frenada`. Reimplementarlo en JS garantiza que
        el visor y el informe escrito digan numeros distintos de la misma vuelta,
        que es la peor forma de estar equivocado.
      · Es TESTEABLE. Con una traza sintetica se sabe el metro exacto esperado;
        en el canvas solo se puede mirar y opinar.
      · Se REUSA. `tools/informe.py` va a pedir lo mismo para escribir el
        resumen de la tanda, sin pasar por el navegador.

    Si el mapa falla por lo que sea, `eventos` va vacio: es un adorno del visor,
    no puede tumbar la traza (que es lo que sostiene los graficos y el delta).
    """
    ruta = os.path.join(folder, os.path.basename(nombre))
    data = AT._read_trace(ruta)
    if not data:
        raise ValueError("no existe esa traza")
    largo = _meta(folder).get("track_length_m")
    reg = next((v for v in _vueltas(folder) if v.get("traza") == os.path.basename(nombre)), None)
    muestras = len(data["lap_dist"])
    pedidos = [c for c in canales if c in data]
    data, cierre = _cerrar_en_meta(data, pedidos, largo, reg.get("tiempo") if reg else None)
    xs, cols = _rejilla(data["lap_dist"], [data[c] for c in pedidos], paso, largo)
    dec = {"t": 3, "throttle": 3, "brake": 3, "steer": 3}
    out = {
        "traza": os.path.basename(nombre),
        "vuelta": reg.get("vuelta") if reg else None,
        "tiempo": reg.get("tiempo") if reg else None,
        "valida": reg.get("valida") if reg else None,
        "paso_m": paso,
        "cierre_s": cierre,          # tiempo completado con el cronometro (0 = traza entera)
        "metros": [round(x, 1) for x in xs],
        "canales": {c: [round(v, dec.get(c, 1)) for v in col]
                    for c, col in zip(pedidos, cols)},
        "muestras": muestras,
    }
    try:
        out["eventos"] = eventos(out, _curvas_cacheadas(folder))
    except Exception:                # noqa: BLE001  (adorno: nunca tumba la traza)
        out["eventos"] = []
    return out


def delta(a: dict, b: dict) -> list[float]:
    """Delta de tiempo acumulado A - B metro a metro (segundos, + = A va perdiendo).

    Ambas vueltas ya vienen en la misma grilla, asi que es una resta directa de
    sus relojes. NO es una estimacion: `t` es el tiempo real de vuelta de cada
    muestra a 50 Hz.
    """
    ta, tb = a["canales"].get("t"), b["canales"].get("t")
    if not ta or not tb:
        return []
    n = min(len(ta), len(tb))
    return [round(ta[i] - tb[i], 3) for i in range(n)]


# ------------------------------------------------- puntos clave por curva ---
_CACHE_CURVAS: dict = {}


def _curvas_cacheadas(folder: str) -> list:
    """Las curvas del mapa de la sesion, memorizadas por carpeta.

    `traza()` las necesita para cada vuelta, y recalcularlas significa releer y
    remuestrear el .gz de la vuelta de referencia CADA vez -- comparando dos
    vueltas eso es el doble de trabajo, y el test del corpus pide ~140 trazas
    seguidas. La llave incluye el mtime de la carpeta: mientras giras entran
    trazas nuevas, la vuelta de referencia puede cambiar y el cache tiene que
    caducar solo en vez de congelar la numeracion de la primera consulta.
    """
    try:
        llave = (folder, os.path.getmtime(folder))
    except OSError:
        return mapa(folder)["curvas"]
    if llave not in _CACHE_CURVAS:
        _CACHE_CURVAS.clear()        # una sesion a la vez; no hay nada que acumular
        _CACHE_CURVAS[llave] = mapa(folder)["curvas"]
    return _CACHE_CURVAS[llave]


def eventos(tr: dict, curvas: list) -> list[dict]:
    """Los cuatro puntos con los que se habla de una curva, vuelta a vuelta.

    Un piloto no compara "la trazada" en abstracto: compara DONDE frena, DONDE
    dobla, DONDE esta el apex y DONDE vuelve a fondo. Esos cuatro metros por
    curva son lo unico que hace comparable una vuelta con otra en palabras
    ("frenaste 12 m antes y saliste a fondo 8 m despues"), y son justo lo que el
    mapa no puede mostrar solo con colorear la linea.

    `tr` es la salida de `traza()` (grilla regular de `paso_m` metros) y `curvas`
    la de `mapa()["curvas"]`. Devuelve una fila por curva que caiga dentro de la
    traza:

        {"n": 1, "frenada_m": 812.0, "giro_m": 856.0, "apex_m": 901.0,
         "vmin_kmh": 96.3, "gas_m": 930.0}

    Cualquiera de los metros puede ser None y eso NO es un error: una curva de
    apoyo se pasa sin tocar el freno (`frenada_m` None) y una curva encadenada
    con la siguiente nunca llega a fondo antes del tope (`gas_m` None). Mostrar
    None es informacion; inventar un numero, no.
    """
    metros = tr.get("metros") or []
    ch = tr.get("canales") or {}
    spd = ch.get("speed_kmh")
    if not metros or not spd:
        return []
    brk, thr, st = ch.get("brake"), ch.get("throttle"), ch.get("steer")
    paso = tr.get("paso_m") or (metros[1] - metros[0] if len(metros) > 1 else 1.0)
    n = len(metros)
    fin_traza = metros[-1]

    def ix(m):
        """Indice de grilla del metro `m`. La grilla arranca en 0 y es regular."""
        return max(0, min(n - 1, int(round(m / paso))))

    out, ap_prev = [], -1
    for cu in curvas or []:
        ini, fin = cu.get("inicio"), cu.get("fin")
        if ini is None or fin is None or ini - 20 > fin_traza:
            continue                 # curva fuera de esta traza (vuelta truncada)
        a, b = ix(ini - 20), ix(fin + 20)
        if b <= a:
            b = min(n - 1, a + 1)
        ap = min(range(a, b + 1), key=lambda i: spd[i])
        apex_m = metros[ap]

        # --- frenada: hacia ATRAS desde el apex ---------------------------------
        # Mismo criterio que `analyze_telemetry._punto_frenada`: buscar hacia
        # adelante agarra la frenada de la curva ANTERIOR en cuanto hay chicanas,
        # y cortar en el primer hueco parte una frenada modulada en dos.
        #
        # Con un tope EXTRA que aquel no necesita: no se cruza el apex de la curva
        # anterior. `_punto_frenada` se llama con curvas sacadas de los minimos de
        # velocidad (todas frenadas de verdad); aca las curvas salen de la
        # CURVATURA, asi que la lista incluye kinks que se pasan a fondo. Medido
        # en Cordoba: T4 se pasa plana y el tope de 400 m dejaba que la busqueda
        # se comiera entera la frenada de T3 y anotara "T4 frena a 398 m del
        # apex" -- un punto que esta 200 m dentro de la curva anterior. Con el
        # tope, T4 queda en None, que es la verdad: no frena.
        frenada_m = None
        if brk:
            tope = max(0, ap_prev + 1, ap - int(round(400.0 / paso)))
            j = ap
            while j > tope and brk[j] <= 0.05:
                j -= 1
            if brk[j] > 0.05:
                ultimo = j
                while j > tope:
                    if brk[j] > 0.05:
                        ultimo = j
                    elif metros[ultimo] - metros[j] > 15.0:
                        break        # 15 m de freno suelto ya no es modular
                    j -= 1
                frenada_m = metros[ultimo]

        # --- giro (turn-in): primer volante sostenido --------------------------
        # El umbral es ADAPTATIVO (20% del maximo de la propia curva) porque un
        # valor fijo no sirve para las dos puntas del rango: una horquilla de
        # primera llega a |steer| 0.9 y una curva rapida de apoyo apenas roza
        # 0.12. Con umbral fijo bajo, la rapida marca giro en cualquier
        # correccion de recta; con umbral fijo alto, desaparece.
        giro_m = None
        if st:
            w0, w1 = ix(ini - 120), ix(fin + 20)
            mx = max((abs(st[i]) for i in range(w0, w1 + 1)), default=0.0)
            umb = max(0.05, 0.20 * mx)
            base = frenada_m if frenada_m is not None else ini - 120
            desde = max(ap_prev + 1, ix(max(base, apex_m - 400.0)))
            lim = min(ap, n - 3)
            for i in range(desde, lim + 1):
                # tres muestras seguidas (6 m): un pico suelto es ruido del canal
                if abs(st[i]) >= umb and abs(st[i + 1]) >= umb and abs(st[i + 2]) >= umb:
                    giro_m = metros[i]
                    break

        # --- a fondo: primer acelerador clavado a la salida --------------------
        gas_m = None
        if thr:
            lim = min(ix(fin + 250), n - 3)
            for i in range(ap, lim + 1):
                if thr[i] >= 0.90 and thr[i + 1] >= 0.90 and thr[i + 2] >= 0.90:
                    gas_m = metros[i]
                    break

        out.append({
            "n": cu.get("n"),
            "frenada_m": round(frenada_m, 1) if frenada_m is not None else None,
            "giro_m": round(giro_m, 1) if giro_m is not None else None,
            "apex_m": round(apex_m, 1),
            "vmin_kmh": round(spd[ap], 1),
            "gas_m": round(gas_m, 1) if gas_m is not None else None,
        })
        ap_prev = ap
    return out


# --------------------------------------------------------- mapa de pista ---
def _curvatura(xs, zs, paso, base_m=12.0):
    """Curvatura (rad/m) del trazado. El signo dice el lado: + izquierda.

    El rumbo NO se mide entre puntos adyacentes sino sobre una BASE FISICA de
    `base_m` metros. Es la diferencia entre funcionar y no funcionar con datos
    de otra fuente: la app del otro piloto muestrea a 20 Hz, asi que a 240 km/h
    sus puntos van cada 3.3 m; al llevarlos a la grilla de 2 m se interpola y
    queda una poligonal con codos -- curvatura cero dentro de cada tramo y un
    pico brutal en cada vertice. Medido: con rumbo entre adyacentes Spielberg
    daba 41 curvas con radios de 0.1 m y giros de 116.000 grados. Sobre una base
    de 12 m el estimador ignora el paso de la fuente y mide la geometria real.
    """
    n = len(xs)
    w = max(1, int(round(base_m / max(paso, 0.1) / 2)))
    rumbo = [0.0] * n
    for i in range(n):
        a, b = max(0, i - w), min(n - 1, i + w)
        if b > a:
            rumbo[i] = math.atan2(zs[b] - zs[a], xs[b] - xs[a])
    k = [0.0] * n
    for i in range(n):
        a, b = max(0, i - w), min(n - 1, i + w)
        dth = rumbo[b] - rumbo[a]
        while dth > math.pi:
            dth -= 2 * math.pi
        while dth < -math.pi:
            dth += 2 * math.pi
        ds = (b - a) * paso
        k[i] = dth / ds if ds > 0.5 else 0.0
    return AT._smooth(k, 5)


def _curvas(metros, xs, zs, radio_max=250.0, giro_min=10.0, sep_min=45.0):
    """Numera las curvas T1..Tn por CURVATURA del trazado, no por minimos de
    velocidad.

    Por que no por velocidad: una curva rapida de apoyo no baja la velocidad lo
    suficiente para hacer un minimo prominente y desaparece de la numeracion.
    Medido en el proyecto hermano de LMU sobre Bahrein: por velocidad salieron 4
    de 11 curvas; por curvatura, las 11 en orden. La geometria de la pista no
    depende de como la manejaste ese dia, que es justo lo que uno quiere para
    NUMERAR.

    DOS filtros, y el segundo es el que hace el trabajo fino:

      · `radio_max` (m): que tan cerrado tiene que ir el trazado para contar.
      · `giro_min` (grados): cuanto tiene que GIRAR el auto en el tramo.

    El radio solo no alcanza. Subirlo para que aparezcan las curvas rapidas hace
    aparecer tambien cada quiebre de una recta; bajarlo para limpiar los quiebres
    borra las rapidas. Medido en Spa con radio solo: 14 curvas de ~19, con
    Eau Rouge y Blanchimont perdidas por ser de radio grande. El giro total
    las separa bien porque una curva rapida gira mucho aunque sea abierta,
    y un quiebre de recta gira poco aunque sea brusco.

    El numero NO es una verdad de catalogo: es lo que la geometria de TU vuelta
    sostiene. En el mapa se ve cada T dibujada sobre la pista, asi que el error
    se detecta mirando, no confiando.
    """
    paso = (metros[1] - metros[0]) if len(metros) > 1 else 1.0
    k = _curvatura(xs, zs, paso)
    umbral = 1.0 / radio_max
    curvas, dentro = [], None
    for i, kv in enumerate(k):
        if abs(kv) >= umbral:
            if dentro is None:
                dentro = [i, i]
            else:
                dentro[1] = i
        elif dentro is not None:
            curvas.append(tuple(dentro))
            dentro = None
    if dentro is not None:
        curvas.append(tuple(dentro))

    out = []
    for a, b in curvas:
        pico = max(range(a, b + 1), key=lambda i: abs(k[i]))
        giro = math.degrees(abs(sum(k[a:b + 1]) * paso))
        if out and metros[pico] - out[-1]["metro"] < sep_min:
            # dos tramos separados por un respiro corto son LA MISMA curva
            # (una chicana leida asi se parte en dos y corre toda la numeracion)
            if abs(k[pico]) > abs(out[-1]["k"]):
                out[-1].update(metro=metros[pico], k=k[pico], i=pico)
            out[-1]["fin"] = metros[b]
            out[-1]["giro"] += giro
            continue
        out.append({"metro": metros[pico], "k": k[pico], "i": pico, "giro": giro,
                    "inicio": metros[a], "fin": metros[b]})
    out = [c for c in out if c["giro"] >= giro_min]
    return [{"n": i + 1, "metro": round(c["metro"], 1), "giro": round(c["giro"]),
             "inicio": round(c["inicio"], 1), "fin": round(c["fin"], 1),
             "lado": "izq" if c["k"] > 0 else "der",
             "radio_m": round(1.0 / abs(c["k"]), 1) if c["k"] else None,
             "x": round(xs[c["i"]], 1), "z": round(zs[c["i"]], 1)}
            for i, c in enumerate(out)]


def mapa(folder: str, nombre: str | None = None) -> dict:
    """Trazado de la pista en coordenadas de mundo + numeracion de curvas.

    Se dibuja con la vuelta mas rapida con traza de la sesion: es la linea que de
    verdad manejaste, no un trazado de catalogo.
    """
    vs = [v for v in _vueltas(folder) if v.get("traza")]
    if not vs:
        raise ValueError("la sesion no tiene ninguna traza guardada")
    if nombre:
        elegida = next((v for v in vs if v["traza"] == os.path.basename(nombre)), None)
        if elegida is None:
            raise ValueError("esa vuelta no tiene traza")
    else:
        elegida = min(vs, key=lambda v: v.get("tiempo") or 9e9)
    data = AT._read_trace(os.path.join(folder, elegida["traza"]))
    largo = _meta(folder).get("track_length_m")
    xs_m, (px, pz) = _rejilla(data["lap_dist"], [data["pos_x"], data["pos_z"]], PASO_M, largo)
    curvas = _curvas(xs_m, px, pz)

    # a la pantalla va un trazado aligerado; las curvas se calculan con TODO el
    # detalle (aligerar antes de derivar se come las curvas cerradas)
    salto = max(1, len(xs_m) // MAPA_PUNTOS)
    return {
        "vuelta": elegida["vuelta"], "traza": elegida["traza"], "tiempo": elegida["tiempo"],
        "largo_m": round(xs_m[-1], 1) if xs_m else None,
        "largo_pista_m": largo,
        "metros": [round(xs_m[i], 1) for i in range(0, len(xs_m), salto)],
        "x": [round(px[i], 1) for i in range(0, len(px), salto)],
        "z": [round(pz[i], 1) for i in range(0, len(pz), salto)],
        "curvas": curvas,
    }


# ------------------------------------------------------------ export CSV ---
def stint_csv(folder: str, n: int, solo_validas: bool = False):
    """Genera el CSV de una tanda completa, vuelta por vuelta, para abrirlo en
    Excel / MoTeC / pandas.

    Va como generador de lineas para no armar 30 MB en memoria antes de mandar
    el primer byte. Se antepone la columna `lap` (numero de vuelta) y `uid` para
    que el archivo se pueda separar despues: sin eso, concatenar vueltas produce
    un CSV donde no se sabe donde termina una y empieza la otra.
    """
    ss = stints(folder)
    if not 1 <= n <= len(ss):
        raise ValueError("no existe esa tanda")
    vs = [v for v in ss[n - 1]["vueltas"] if v.get("traza")
          and (v.get("valida") or not solo_validas)]
    if not vs:
        raise ValueError("la tanda no tiene ninguna vuelta con traza")
    import gzip
    primera = True
    for v in vs:
        ruta = os.path.join(folder, v["traza"])
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            cab = f.readline().rstrip("\n")
            if primera:
                yield "lap,uid," + cab + "\n"
                primera = False
            pre = f"{v['vuelta']},{v.get('uid') or 0},"
            for ln in f:
                if ln.strip():
                    yield pre + ln
