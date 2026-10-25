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

import datetime
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


_CACHE_META: dict = {}


def _meta_indexado(folder: str) -> dict:
    """`_meta` memorizado por el mtime de su session.json. "Comparar con otra
    sesion" filtra TODAS las carpetas por pista en cada carga del visor (cientos de
    session.json): un stat por carpeta es mucho mas barato que parsear cada JSON, y
    el archivo casi nunca cambia (se escribe al abrir la sesion)."""
    try:
        mtime = os.path.getmtime(os.path.join(folder, "session.json"))
    except OSError:
        mtime = None
    c = _CACHE_META.get(folder)
    if c is None or c[0] != mtime:
        c = (mtime, _meta(folder))
        _CACHE_META[folder] = c
    return c[1]


def sesiones(limite: int | None = 40, pista: str | None = None,
             variante: str | None = None) -> list[dict]:
    """Sesiones grabadas, de la mas reciente a la mas vieja.

    Con `pista` (y su `variante`) solo las de esa pista, y `limite` None = TODAS. Es
    lo que pide "comparar con otra sesion": en la clase 8 el alumno busca su
    medicion de la clase 2, un mes atras, y si giro por su cuenta entre medio ya no
    esta entre las 60 mas recientes del selector de arriba."""
    out = []
    con_mt = []
    for d in _dirs():
        try:
            con_mt.append((os.path.getmtime(d), d))
        except OSError:
            continue                  # carpeta que desaparecio entre el listdir y aca
    for mt, d in sorted(con_mt, reverse=True):
        if limite and len(out) >= limite:
            break
        m = _meta_indexado(d)
        if pista is not None and (m.get("track"), m.get("track_variation") or None) \
                != (pista, variante or None):
            continue
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
    fij = curvas_de_pista(folder)          # fijadas: no hay nada que calcular
    if fij:
        return fij["curvas"]
    try:
        llave = (folder, os.path.getmtime(folder))
    except OSError:
        return mapa(folder)["curvas"]
    if llave not in _CACHE_CURVAS:
        m = mapa(folder)
        # una numeracion sacada de la segunda mas rapida (la primera estaba a medio
        # escribir) no se guarda: cuando la traza termine de volcarse la carpeta no
        # cambia de mtime y el cache serviria la numeracion vieja hasta otra vuelta
        if m.get("respaldo"):
            return m["curvas"]
        _CACHE_CURVAS.clear()        # una sesion a la vez; no hay nada que acumular
        _CACHE_CURVAS[llave] = m["curvas"]
    return _CACHE_CURVAS[llave]


def eventos(tr: dict, curvas: list) -> list[dict]:
    """Los puntos con los que se habla de una curva, vuelta a vuelta.

    Un piloto no compara "la trazada" en abstracto: compara DONDE frena, DONDE
    dobla, DONDE esta el apex, DONDE vuelve al gas y DONDE va a fondo. Esos
    metros por curva son lo unico que hace comparable una vuelta con otra en
    palabras ("frenaste 12 m antes y saliste a fondo 8 m despues"), y son justo
    lo que el mapa no puede mostrar solo con colorear la linea.

    `tr` es la salida de `traza()` (grilla regular de `paso_m` metros) y `curvas`
    la de `mapa()["curvas"]`. Devuelve una fila por curva que caiga dentro de la
    traza:

        {"n": 1, "frenada_m": 812.0, "giro_m": 856.0, "apex_m": 901.0,
         "vmin_kmh": 96.3, "acel_m": 904.0, "gas_m": 930.0}

    Cualquiera de los metros puede ser None y eso NO es un error: una curva de
    apoyo se pasa sin tocar el freno (`frenada_m` None), una curva a fondo nunca
    suelta el gas (`acel_m` None) y una curva encadenada con la siguiente nunca
    llega a fondo antes del tope (`gas_m` None). Mostrar None es informacion;
    inventar un numero, no.
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
                ultimo = fin_fr = j
                while j > tope:
                    if brk[j] > 0.05:
                        ultimo = j
                    elif metros[ultimo] - metros[j] > 15.0:
                        break        # 15 m de freno suelto ya no es modular
                    j -= 1
                # el toque tardio de una frenada mas grande no es "donde frena"
                # (misma regla que el informe: `AT._saltar_toque`)
                ultimo = AT._saltar_toque(metros, brk, spd, ultimo, fin_fr, tope + 1)
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
        gas_m, g = None, None
        if thr:
            lim = min(ix(fin + 250), n - 3)
            for i in range(ap, lim + 1):
                if thr[i] >= 0.90 and thr[i + 1] >= 0.90 and thr[i + 2] >= 0.90:
                    gas_m, g = metros[i], i
                    break

        # --- acelera: donde VUELVE al gas, aunque no sea a fondo ----------------
        # "A fondo" dice cuando se compromete; "acelera" dice cuando deja de
        # esperar, y en el debrief es la pregunta de la salida ("abres el gas en
        # el apex o 20 m despues?"). Medido en la referencia del curso (Spielberg
        # corta, T1): acelera 28 m ANTES del apex y llega a fondo justo en el
        # apex, asi que buscarlo desde el apex, como `gas_m`, lo pierde.
        #
        # Se busca despues del ULTIMO momento con freno pisado o sin gas (< 10%)
        # entre la frenada y 20 m pasado el apex, sin pasar de "a fondo". Asi no
        # cuentan los golpes de gas del rebaje (AMS2 mete el auto-blip en el pedal
        # crudo y caen DENTRO de la frenada) ni un gas de mantenimiento que se
        # vuelve a soltar antes del apex. None = nunca solto el gas en esa curva.
        acel_m = None
        if thr:
            fin_off = None
            w0 = max(ap_prev + 1, ix(frenada_m) if frenada_m is not None else ix(ini - 120))
            w1 = min(ap + int(round(20.0 / paso)), n - 1)
            if g is not None:
                w1 = min(w1, g - 1)
            for i in range(w0, w1 + 1):
                if thr[i] < 0.10 or (brk and brk[i] > 0.05):
                    fin_off = i
            if fin_off is not None:
                lim = g if g is not None else min(ix(fin + 250), n - 3)
                for i in range(fin_off + 1, lim + 1):
                    if all(thr[k] >= 0.10 and not (brk and brk[k] > 0.05) for k in (i, i + 1, i + 2)):
                        acel_m = metros[i]
                        break

        out.append({
            "n": cu.get("n"),
            "frenada_m": round(frenada_m, 1) if frenada_m is not None else None,
            "giro_m": round(giro_m, 1) if giro_m is not None else None,
            "apex_m": round(apex_m, 1),
            "vmin_kmh": round(spd[ap], 1),
            "acel_m": round(acel_m, 1) if acel_m is not None else None,
            "gas_m": round(gas_m, 1) if gas_m is not None else None,
        })
        ap_prev = ap
    return out


# ------------------------------------------------- tu frenada (por corrida) ---
# Emparejar una curva de hoy con la de otra sesion por el metro del apex: hasta
# cuanto se puede mover el apex de la MISMA curva entre dos sesiones. Medido sobre
# 799 curvas frenadas de sesiones de la misma pista (autos distintos incluidos):
# p50 14 m, y la masa de "misma curva" se acaba a los ~50 m (0-10: 316, 10-20: 168,
# 20-30: 74, 30-40: 24, 40-50: 12, 50-60: 2); lo que sigue son curvas que la otra
# sesion no frena. Dos curvas frenadas estan a 80 m o mas (`_corners`), asi que con
# 45 m una curva sin pareja no alcanza a robarse la de al lado.
TOL_APEX_SESIONES_M = 45.0


def medicion(folder: str) -> dict:
    """La vuelta de la MEDICION de la sesion: la mas rapida de las primeras 8 de la
    tanda puntuada (`analyze_telemetry.vueltas_de_la_tanda` / `mejor_de_la_tanda`,
    la misma muestra con que se calculan el gap% y el CV%).

    Es la vuelta B del grafico "tu clase 2 contra la de hoy" de la clase 8: si el
    alumno la elige a ojo, elige la mas rapida de la sesion -- que no es la que
    puntuo, y el informe y el visor contarian dos historias. Sin tanda puntuada
    (ninguna corrida llega a 8, o sesion sin linea de tiempo) NO se elige otra:
    se dice que falta, con el motivo.
    """
    regs, origen, k = AT.vueltas_de_la_tanda(folder)
    if k is None or not regs:
        if "sin corrida" in origen:
            falta = ("esta sesión no tiene tanda puntuada: ninguna corrida llega a 8 vueltas "
                     "cronometradas seguidas")
        elif "no cuadra" in origen:
            falta = ("esta sesión no tiene tanda puntuada: su línea de tiempo no cuadra con el "
                     "resumen y no se puede partir en corridas")
        else:
            falta = ("esta sesión no tiene tanda puntuada (es anterior a la línea de tiempo y "
                     "no se puede partir en corridas)")
        return {"falta": falta}
    mejor = min(regs, key=lambda l: l["lap_time"])
    return {"corrida": k, "traza": mejor.get("trace"), "vuelta": mejor.get("lap"),
            "tiempo": mejor.get("lap_time"), "origen": origen}


def _corrida_param(v):
    """El parametro `corrida` tal como llega por HTTP: vacio = el frio, o un numero."""
    if v in (None, "", "frio"):
        return "frio"
    if not str(v).isdigit():
        raise ValueError("corrida invalida")
    return int(v)


def _ts_comparables(ta, tb):
    """True si "T5" significa la misma curva en las dos sesiones.

    La numeracion del visor se calcula POR SESION (`mapa()`: la curvatura de la
    vuelta mas rapida de esa carpeta), asi que una curva de apoyo que un dia pasa
    el umbral y otro no corre todas las T que vienen despues. No se asume: se
    compara la lista entera -- mismo largo, y cada T solapando el tramo
    (inicio..fin) de su homonima.

    Por solape y no por distancia entre picos: en una curva larga el pico de
    curvatura se pasea por el arco segun la trazada (Mosport, misma pista y auto:
    T1 en el metro 200 un dia y en el 278 otro, misma curva), y un tope de 40 m
    entre picos declaraba distintas numeraciones que eran la misma. Medido el
    2026-10-11 sobre 642 pares de sesiones de la misma pista y largo: 38% tienen la
    misma cantidad de T, 25% numeran igual por solape (32% si ademas es el mismo
    auto) contra 7% con el tope de 40 m; ningun par que solapa tiene picos a mas
    de 150 m.
    O sea: lo normal es que NO coincidan, y el emparejado por metro es el de
    todos los dias.
    """
    if not ta or not tb or len(ta) != len(tb):
        return False
    try:
        return all(a["inicio"] <= b["fin"] and b["inicio"] <= a["fin"] for a, b in zip(ta, tb))
    except (KeyError, TypeError):
        return False


def emparejar_frenadas(hoy: dict, otra: dict) -> dict:
    """Filas "hoy contra la clase pasada": cada curva de hoy con la misma curva de la
    otra sesion.

    Por T si las dos numeraciones son la misma (`_ts_comparables`); si no, por el
    metro del apex, y se avisa -- emparejar por T dos numeraciones distintas pone
    lado a lado curvas distintas con el mismo nombre, y el numero sale creible. Las
    curvas sin pareja quedan igual en la tabla, con el otro lado vacio: que una
    curva hoy no tenga dato tambien es informacion.
    """
    ca, cb = hoy.get("curvas") or [], otra.get("curvas") or []
    # con las curvas fijadas de la pista las dos usan EL MISMO archivo: misma T por
    # construccion, sin nada que comparar
    na, nb = hoy.get("numeracion") or {}, otra.get("numeracion") or {}
    misma_pista = (na.get("fuente") == "pista" and nb.get("fuente") == "pista"
                   and na.get("archivo") == nb.get("archivo"))
    por_t = misma_pista or _ts_comparables(hoy.get("ts"), otra.get("ts"))
    pareja, usadas = {}, set()
    if por_t:
        idx_b = {c["t"]: j for j, c in enumerate(cb) if c.get("t") is not None}
        for i, c in enumerate(ca):
            j = idx_b.get(c.get("t")) if c.get("t") is not None else None
            if j is not None:
                pareja[i] = j
                usadas.add(j)
    # lo que quedo (todo, si las T no son comparables): por metro, la pareja mas
    # cercana primero y cada curva una sola vez
    cand = sorted((abs(a["apex"] - b["apex"]), i, j)
                  for i, a in enumerate(ca) if i not in pareja
                  for j, b in enumerate(cb) if j not in usadas
                  and abs(a["apex"] - b["apex"]) <= TOL_APEX_SESIONES_M)
    for _, i, j in cand:
        if i in pareja or j in usadas:
            continue
        pareja[i] = j
        usadas.add(j)
    filas = [{"hoy": c, "otra": cb[pareja[i]] if i in pareja else None}
             for i, c in enumerate(ca)]
    filas += [{"hoy": None, "otra": c} for j, c in enumerate(cb) if j not in usadas]
    filas.sort(key=lambda f: (f["hoy"] or f["otra"])["apex"])
    aviso = None
    if not por_t and ca and cb:
        aviso = ("Las dos sesiones no numeran las curvas igual (la pista no tiene curvas "
                 "fijadas y cada sesión se numera con su propia vuelta más rápida), así que "
                 "se emparejaron por distancia. La T es la de hoy.")
    return {"por": "T" if por_t else "metro", "aviso": aviso, "filas": filas}


def frenadas(folder: str, corrida=None, otra: str | None = None, otra_corrida=None) -> dict:
    """Lo que pide el panel "Tu frenada" del visor (`/api/frenadas`).

    El calculo es `analyze_telemetry.frenadas_struct`, el MISMO que imprime
    `--frenadas --corrida`: aca solo se le agrega la otra sesion y el emparejado.
    `corrida` vacia = el frio (la primera corrida con 2+ vueltas cronometradas).
    """
    out = {"carpeta": os.path.basename(folder),
           **AT.frenadas_struct(folder, _corrida_param(corrida))}
    if otra:
        of = _carpeta(otra)
        ma, mb = _meta(folder), _meta(of)
        if (ma.get("track"), ma.get("track_variation")) != (mb.get("track"), mb.get("track_variation")):
            raise ValueError("la otra sesion es de otra pista")
        out["otra"] = {"carpeta": os.path.basename(of), "inicio": mb.get("started"),
                       **AT.frenadas_struct(of, _corrida_param(otra_corrida))}
        out["pares"] = emparejar_frenadas(out, out["otra"])
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


# ------------------------------------------- curvas fijadas por PISTA ---
# La numeracion de `_curvas` sale de la vuelta mas rapida de CADA sesion, asi que
# cambia de una sesion a otra (medido: solo 1 de cada 4 pares de sesiones de la
# misma pista numera igual, ver _ts_comparables). En el curso el instructor y diez
# alumnos, cada uno en su PC, tienen que decir "la T3" y hablar de la misma curva
# todas las semanas. Por eso las curvas de una pista se FIJAN: un archivo por pista
# en pistas/, versionado en el repo, que llega a todos con la actualizacion del
# dash. Cuando existe manda en todo -- mapa, chips, tabla por curva, panel de
# frenada, `--frenadas`, informe -- y solo si no existe se cae a la de la sesion.
#
# Se empareja por METRO de pista sin normalizar nada: `lap_dist` es la coordenada
# del juego SOBRE LA PISTA, no la distancia que recorrio el auto. Medido el
# 2026-10-11 en 91 pares de sesiones de la misma pista y largo (autos distintos
# incluidos): el punto de pista mas cercano cae en el MISMO metro de la grilla de 2 m
# en el 99% de los casos, y ningun par tiene corrimiento sistematico. Lo unico que
# invalida un archivo es otro LARGO (otra version del trazado): por eso el largo va
# en el nombre y se exige que cuadre.
PISTAS = os.path.join(HERE, "pistas")
TOL_LARGO_M = 5.0         # el mismo tope con que el visor acepta comparar dos sesiones
_CACHE_PISTAS: dict = {}


def _limpio(s) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(s or ""))


def _finito(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _validar_curvas(j) -> str | None:
    """None si el archivo de curvas fijadas sirve; si no, el motivo.

    Se edita a mano a proposito (una T que sobra, numeracion de catalogo), asi que
    se valida al cargar: un archivo roto no puede llegar al mapa de diez alumnos.
    Reglas: `largo_m` positivo; cada curva con `n` entero positivo y unico, y
    `metro`/`inicio`/`fin` finitos con 0 <= inicio <= metro <= fin <= largo_m; las
    curvas en orden creciente de metro Y de n (un n que retrocede es un error de
    tipeo, no una numeracion de catalogo).
    """
    if not isinstance(j, dict):
        return "no es un objeto JSON"
    largo = j.get("largo_m")
    if not _finito(largo) or largo <= 0:
        return "largo_m no es un numero positivo"
    cs = j.get("curvas")
    if not isinstance(cs, list) or not cs:
        return "no trae curvas"
    vistos, prev = set(), None
    for i, c in enumerate(cs, 1):
        if not isinstance(c, dict):
            return f"la curva {i} no es un objeto"
        n = c.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            return f"la curva {i} no tiene un n entero positivo"
        if n in vistos:
            return f"T{n} esta repetida"
        for k in ("metro", "inicio", "fin"):
            if not _finito(c.get(k)):
                return f"T{n}: {k} no es un numero"
        for k in ("x", "z", "radio_m", "giro"):
            if c.get(k) is not None and not _finito(c[k]):
                return f"T{n}: {k} no es un numero"
        if not 0 <= c["inicio"] <= c["metro"] <= c["fin"] <= largo:
            return f"T{n}: no cumple 0 <= inicio <= metro <= fin <= largo_m"
        if prev is not None and (c["metro"] <= prev[1] or n <= prev[0]):
            return f"T{n}: las curvas no van en orden creciente (de metro y de n)"
        vistos.add(n)
        prev = (n, c["metro"])
    return None


def _leer_pista(ruta: str):
    """Un archivo de curvas fijadas, memorizado por mtime (se pide en cada traza).
    Devuelve (documento|None, motivo|None): motivo = por que se rechazo."""
    try:
        llave = (ruta, os.path.getmtime(ruta))
    except OSError:
        return None, "no se pudo leer"
    if llave not in _CACHE_PISTAS:
        try:
            with open(ruta, encoding="utf-8") as f:
                j = json.load(f)
            motivo = _validar_curvas(j)
        except (OSError, ValueError) as e:
            j, motivo = None, f"no es JSON valido ({type(e).__name__})"
        if motivo is not None:
            j = None
        else:
            j["_ruta"], j["_archivo"] = ruta, os.path.basename(ruta)
        _CACHE_PISTAS[llave] = (j, motivo)
    return _CACHE_PISTAS[llave]


def _buscar_curvas(folder: str):
    """(curvas fijadas de la pista de esta sesion o None, [avisos]).

    Si hay mas de un archivo para la pista (dos versiones del largo, una copia
    vieja), manda el de MENOR diferencia de largo, no el primero alfabetico, y se
    avisa: dos archivos para una pista es un error de quien los versiona. Un archivo
    que no valida se rechaza con aviso; si no queda otro, se cae a la numeracion de
    la sesion.
    """
    meta = _meta(folder)
    largo = meta.get("track_length_m")
    if not meta.get("track") or not largo or not os.path.isdir(PISTAS):
        return None, []
    pre = f"curvas__{_limpio(meta['track'])}__{_limpio(meta.get('track_variation'))}__"
    validos, avisos = [], []
    for f in sorted(os.listdir(PISTAS)):
        if not (f.startswith(pre) and f.endswith(".json")):
            continue
        j, motivo = _leer_pista(os.path.join(PISTAS, f))
        if j is None:
            avisos.append(f"{f} no se usa: {motivo}")
            continue
        if (j.get("pista") == meta["track"] and j.get("variante") == meta.get("track_variation")
                and abs(j["largo_m"] - largo) <= TOL_LARGO_M):
            validos.append((abs(j["largo_m"] - largo), f, j))
    if not validos:
        return None, avisos
    validos.sort(key=lambda x: (x[0], x[1]))
    if len(validos) > 1 and validos[0][0] == validos[1][0]:
        # Empate de largo: no hay criterio para elegir, y elegir por nombre de archivo
        # le daria a unos alumnos una T y a otros otra segun que copia les llego. Se
        # rechazan los dos y se dice: lo arregla quien versiona el repo.
        empatados = [f for dif, f, _ in validos if dif == validos[0][0]]
        avisos.append(f"{len(empatados)} archivos de curvas empatan para esta pista "
                      f"({', '.join(empatados)}): no se usa ninguno hasta que quede uno")
        return None, avisos
    if len(validos) > 1:
        avisos.append(f"hay {len(validos)} archivos de curvas para esta pista; se usa "
                      f"{validos[0][1]}, el de largo mas parecido")
    return validos[0][2], avisos


def curvas_de_pista(folder: str):
    """Las curvas fijadas de la pista de esta sesion (pista, variante y largo), o None."""
    return _buscar_curvas(folder)[0]


def numeracion(folder: str) -> dict:
    """De donde sale la T de esta sesion: {"fuente": "pista"|"sesion", "archivo",
    "aviso"}. `aviso` != None si hay archivos rechazados o duplicados."""
    j, avisos = _buscar_curvas(folder)
    return {"fuente": "pista" if j else "sesion", "archivo": j["_archivo"] if j else None,
            "aviso": "; ".join(avisos) or None}


def fijar_curvas(folder: str, forzar: bool = False):
    """Fija la numeracion de curvas de la pista de esta sesion en pistas/.

    Usa la vuelta mas rapida VALIDA con traza completa y posicion, con el MISMO
    algoritmo que el visor (`_curvas`): lo que se fija es exactamente lo que el
    instructor esta mirando en el mapa de esa sesion. Si la pista ya tiene curvas
    fijadas no las pisa sin `forzar`: reemplazarlas cambia la T de todos los alumnos
    en la proxima actualizacion, y eso no puede pasar por un comando repetido.

    El archivo es JSON legible a proposito: si una curva de apoyo sobra o falta, se
    corrige a mano y se versiona, y el `n` que quede escrito es el que se usa.
    Devuelve (ruta, documento). ValueError con el motivo si no se puede.
    """
    meta = _meta(folder)
    largo = meta.get("track_length_m")
    if not meta.get("track") or not largo:
        raise ValueError("la sesion no dice en que pista es o cuanto mide")
    existente = curvas_de_pista(folder)
    if existente and not forzar:
        raise ValueError(f"esta pista ya tiene curvas fijadas ({existente['_archivo']}); "
                         f"reemplazarlas cambia la T de todos: usa --forzar si es a proposito")
    vs = [v for v in _vueltas(folder) if v.get("traza") and v.get("valida") and v.get("tiempo")]
    elegida = data = None
    for v in sorted(vs, key=lambda v: v["tiempo"]):
        try:
            d = AT._read_trace(os.path.join(folder, v["traza"]))
        except (OSError, EOFError):
            continue
        # completa (no truncada por CPU) y con posicion de verdad (las importadas de
        # otras apps la traen en cero, y sin posicion no hay curvatura)
        if d and len(d.get("lap_dist") or []) >= 1000 and any(abs(x) > 1.0 for x in d.get("pos_x") or []):
            elegida, data = v, d
            break
    if elegida is None:
        raise ValueError("ninguna vuelta valida con traza completa y posicion en esta sesion")
    xs_m, (px, pz) = _rejilla(data["lap_dist"], [data["pos_x"], data["pos_z"]], PASO_M, largo)
    curvas = _curvas(xs_m, px, pz)
    if not curvas:
        raise ValueError("la vuelta no tiene curvas que numerar")
    doc = {
        "pista": meta["track"], "variante": meta.get("track_variation"),
        "largo_m": round(largo, 1),
        "fijadas": datetime.date.today().isoformat(),
        "fuente": {"sesion": os.path.basename(folder), "auto": meta.get("car"),
                   "vuelta": elegida.get("vuelta"), "tiempo": elegida.get("tiempo"),
                   "traza": elegida["traza"]},
        "algoritmo": "curvatura del trazado (ams2_analysis._curvas: radio_max 250 m, "
                     "giro_min 10 grados, sep_min 45 m)",
        "curvas": curvas,
    }
    ruta = (existente["_ruta"] if existente else
            os.path.join(PISTAS, f"curvas__{_limpio(meta['track'])}__"
                                 f"{_limpio(meta.get('track_variation'))}__{round(largo)}m.json"))
    os.makedirs(PISTAS, exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return ruta, doc


def mapa(folder: str, nombre: str | None = None) -> dict:
    """Trazado de la pista en coordenadas de mundo + numeracion de curvas.

    Se dibuja con la vuelta mas rapida con traza de la sesion: es la linea que de
    verdad manejaste, no un trazado de catalogo.
    """
    vs = [v for v in _vueltas(folder) if v.get("traza")]
    if not vs:
        raise ValueError("la sesion no tiene ninguna traza guardada")
    saltadas = 0
    if nombre:
        elegida = next((v for v in vs if v["traza"] == os.path.basename(nombre)), None)
        if elegida is None:
            raise ValueError("esa vuelta no tiene traza")
        data = AT._read_trace(os.path.join(folder, elegida["traza"]))
    else:
        # La mas rapida que se pueda LEER. Con la sesion abierta en el grabador (el
        # alumno mira el visor sin salir de la sesion), la linea de tiempo anuncia
        # la traza de la vuelta recien cerrada ANTES de terminar de volcarla, y ese
        # gzip a medias revienta al leerlo. Si justo era la mas rapida, el mapa y la
        # numeracion T caian enteros por unos cientos de ms; ahora se usa la
        # siguiente y la proxima consulta ya la encuentra completa.
        data, saltadas = None, 0
        for elegida in sorted(vs, key=lambda v: v.get("tiempo") or 9e9):
            try:
                data = AT._read_trace(os.path.join(folder, elegida["traza"]))
                break
            except (OSError, EOFError):
                saltadas += 1
        if data is None:
            raise ValueError("ninguna traza de la sesion se puede leer")
    largo = _meta(folder).get("track_length_m")
    xs_m, (px, pz) = _rejilla(data["lap_dist"], [data["pos_x"], data["pos_z"]], PASO_M, largo)
    # las curvas fijadas de la pista mandan; la numeracion de esta vuelta es el respaldo
    fij, avisos_fij = _buscar_curvas(folder)
    curvas = fij["curvas"] if fij else _curvas(xs_m, px, pz)

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
        # "pista" = curvas fijadas en pistas/ (las mismas para todos); "sesion" = las
        # de esta vuelta, que otra sesion puede numerar distinto (el visor lo avisa)
        "numeracion": "pista" if fij else "sesion",
        "curvas_archivo": fij["_archivo"] if fij else None,
        # archivos de curvas rechazados (no validan) o duplicados: el visor lo dice
        "curvas_aviso": "; ".join(avisos_fij) or None,
        # True = la mas rapida no se pudo leer (a medio volcar) y se uso otra: es
        # una numeracion de paso, que _curvas_cacheadas no debe congelar
        "respaldo": saltadas > 0 and not fij,
    }


# -------------------------------------------------- bordes aprendidos ---
# El juego NO entrega la geometria de la pista: la shared memory no trae bordes
# ni ancho, y los archivos de pista van cifrados. Lo que SI trae es `mTerrain`
# por rueda -- que superficie hay debajo de CADA neumatico, a 50 Hz. Con eso los
# bordes se APRENDEN: cada vuelta grabada en esa pista (limpia o anulada, de
# cualquier sesion) deja cuatro puntos por muestra que dicen "aca habia asfalto",
# "aca habia piano" o "aca habia pasto". Sumando todas las vueltas sale hasta
# donde llega el asfalto a cada lado y donde estan los pianos: no es el mapa del
# juego, es la pista tal como la vieron las ruedas, y con pocas vueltas es un
# corredor angosto que se ensancha solo a medida que se gira.
#
# Codigos del enum TerrainMaterials del SDK de Project CARS 2 (AMS2 lo hereda).
# Verificados el 2026-09-10 contra el header CREST2-AMS2 (SHARED_MEMORY_VERSION
# 14): 0 ROAD, 7 GRASS, 10 RUMBLE_STRIPS, 32 ASTROTURF, 40/41 B1/B2RUMBLES, 46
# RUNOFF_ROAD, 47 ILLEGAL_STRIP, 49 PAINT_CONCRETE_ILLEGAL, 50 RALLY_TARMAC. Y
# contra el corpus: 0 en toda la recta, 10 y 41 solo en pianos, 46 en los
# escapes de Mosport, 7 en el pasto, 49 en la linea blanca de Cordoba.
TERRENO_ASFALTO = {0, 1, 2, 3, 4, 21, 29, 35, 36, 37, 45, 48, 50}
TERRENO_PIANO = {10, 25, 40, 41}
TERRENO_FUERA = {5, 6, 7, 8, 9, 15, 16, 17, 18, 19, 20, 22, 24, 26, 27, 28,
                 30, 31, 32, 33, 34, 42, 43, 46, 47, 49}
# Geometria del auto, que la traza no trae: medio ancho de via y media distancia
# entre ejes. Un GT3 mide ~1.0 / 1.35 m, un F-Inter ~0.8 / 1.4. El error de usar
# un valor unico es de decimas de metro sobre un borde que se lee a metros.
VIA_2 = 0.85
EJE_2 = 1.35
BORDE_MAX_M = 15.0        # mas lejos que esto del trazado no es pista, es un trompo
RELLENO_MAX_M = 40.0      # hueco sin dato que se rellena interpolando; mas, se estima


def _leer_columnas(ruta, columnas):
    """Solo las columnas pedidas de una traza .csv.gz, como listas de float.

    `_read_trace` convierte las 107 columnas y para los bordes hay que leer TODAS
    las vueltas de una pista (Road Atlanta: 154). Con 7 columnas de 107 son 15
    veces menos conversiones, y es la diferencia entre 5 s y mas de un minuto.
    """
    import csv
    import gzip
    out = {c: [] for c in columnas}
    try:
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            r = csv.reader(f)
            cab = next(r)
            idx = [(c, cab.index(c)) for c in columnas if c in cab]
            if len(idx) != len(columnas):
                return None
            for fila in r:
                for c, i in idx:
                    try:
                        out[c].append(float(fila[i]))
                    except (ValueError, IndexError):
                        out[c].append(0.0)
    except (OSError, EOFError, StopIteration):
        return None
    return out


def _centro_ref(folder):
    """Linea de referencia de la sesion (la vuelta mas rapida con traza), a PASO_M."""
    vs = [v for v in _vueltas(folder) if v.get("traza")]
    if not vs:
        raise ValueError("la sesion no tiene ninguna traza guardada")
    elegida = min(vs, key=lambda v: v.get("tiempo") or 9e9)
    data = AT._read_trace(os.path.join(folder, elegida["traza"]))
    largo = _meta(folder).get("track_length_m")
    xs_m, (px, pz) = _rejilla(data["lap_dist"], [data["pos_x"], data["pos_z"]], PASO_M, largo)
    return xs_m, px, pz


def _acumular_bordes(cx, cz, paso, trazas):
    """Extension del asfalto y puntos de piano/fuera por metro de la referencia.

    `trazas` es un iterable de dicts con lap_dist, pos_x, pos_z y terrain_FL/FR/
    RL/RR. Funcion PURA (sin disco) para poder probarla con una pista sintetica
    donde el borde se conoce exacto.

    Devuelve (izq, der, pianos, fuera, evi_izq, evi_der, n): `izq[i]`/`der[i]`
    son el maximo y el minimo desplazamiento lateral (m, signo de la normal
    izquierda del trazado) con asfalto bajo una rueda en el metro i, None donde
    ninguna rueda paso -- es la ENVOLVENTE de lo recorrido, o sea un minimo del
    ancho real, no el borde. `evi_izq`/`evi_der` son el BORDE con evidencia: la
    muestra de piano o de fuera de pista mas cercana al trazado en ese metro
    (el piano empieza donde termina el asfalto; el pasto, 0.3 m mas alla). Con
    pocas vueltas la envolvente es un pasillo angosto y la trazada queda pegada
    a su borde por construccion; la evidencia es lo que dice donde esta el
    borde de verdad, aunque nadie haya pasado por ahi con las cuatro ruedas.
    `pianos`/`fuera` son listas de (i, d) ya adelgazadas a 0.5 m, para dibujar.
    """
    n = len(cx)
    # tangente y normal del trazado en cada metro
    tx, tz = [0.0] * n, [0.0] * n
    for i in range(n):
        a, b = max(0, i - 1), min(n - 1, i + 1)
        dx, dz = cx[b] - cx[a], cz[b] - cz[a]
        L = math.hypot(dx, dz) or 1.0
        tx[i], tz[i] = dx / L, dz / L
    izq, der = [None] * n, [None] * n
    evid = [set() for _ in range(n)]         # muestras de borde por metro (d a 0.25 m)
    pianos, fuera = set(), set()
    vueltas = 0
    ruedas = (("terrain_FL", EJE_2, VIA_2), ("terrain_FR", EJE_2, -VIA_2),
              ("terrain_RL", -EJE_2, VIA_2), ("terrain_RR", -EJE_2, -VIA_2))
    for d in trazas:
        if not d or len(d.get("lap_dist", ())) < 3:
            continue
        vueltas += 1
        ld, px, pz = d["lap_dist"], d["pos_x"], d["pos_z"]
        ter = [d[c] for c, _, _ in ruedas]
        m = len(ld)
        for k in range(m):
            i = int(round(ld[k] / paso))
            if i < 0 or i >= n:
                continue
            # rumbo del auto desde sus propias posiciones (no el del trazado:
            # en un trompo o un contravolante no coinciden)
            a, b = max(0, k - 1), min(m - 1, k + 1)
            hx, hz = px[b] - px[a], pz[b] - pz[a]
            L = math.hypot(hx, hz)
            if L < 0.2:
                hx, hz = tx[i], tz[i]
            else:
                hx, hz = hx / L, hz / L
            lx, lz = -hz, hx                    # izquierda del auto
            for w, (col, ade, lat) in enumerate(ruedas):
                wx = px[k] + hx * ade + lx * lat
                wz = pz[k] + hz * ade + lz * lat
                # proyeccion sobre el trazado: adelante/atras corrige el indice,
                # el lateral es el dato
                rx, rz = wx - cx[i], wz - cz[i]
                j = i + int(round((rx * tx[i] + rz * tz[i]) / paso))
                j = 0 if j < 0 else (n - 1 if j >= n else j)
                lat_m = (wx - cx[j]) * -tz[j] + (wz - cz[j]) * tx[j]
                if abs(lat_m) > BORDE_MAX_M:
                    continue
                code = int(ter[w][k])
                if code in TERRENO_ASFALTO:
                    if izq[j] is None or lat_m > izq[j]:
                        izq[j] = lat_m
                    if der[j] is None or lat_m < der[j]:
                        der[j] = lat_m
                elif code in TERRENO_PIANO or code in TERRENO_FUERA:
                    piano = code in TERRENO_PIANO
                    (pianos if piano else fuera).add((j, round(lat_m * 2) / 2))
                    # el pasto ya es un poco mas alla del borde: se acerca 0.3 m
                    # hacia el trazado. De que LADO esta cada muestra se decide
                    # despues, por metro, mirando todas juntas (_lados_evidencia)
                    borde = lat_m if piano else lat_m - (0.3 if lat_m > 0 else -0.3)
                    evid[j].add(round(borde * 4) / 4)
    evi_izq, evi_der = _lados_evidencia(evid, izq, der, _curvatura(cx, cz, paso))
    return izq, der, sorted(pianos), sorted(fuera), evi_izq, evi_der, vueltas


def _lados_evidencia(evid, env_i, env_d, curv, hueco_m=5.0):
    """A que borde pertenece cada muestra de piano/pasto, metro a metro.

    NO se puede decidir por el signo del desplazamiento: la linea de referencia
    es una linea de carrera y en el apex va ENCIMA del piano interior, asi que
    las ruedas de otras vueltas sobre ese mismo piano caen a ambos lados de ella
    -- y leidas por signo son "borde izquierdo" y "borde derecho" a 1.5 m uno
    del otro. Medido en Silverstone: ancho mediano de 4 m por esto.

    Se agrupan las muestras del metro en cumulos separados por mas de `hueco_m`
    (dos bordes de una pista nunca estan a menos de 5 m). Dos cumulos = los dos
    bordes. Uno solo = un borde, y de que lado se decide contra el CENTRO del
    asfalto recorrido; si lo cruza (el auto iba sobre el piano), por la
    curvatura: en una curva, el piano que se pisa es el interior.

    El borde es donde EMPIEZA el piano: el extremo del cumulo mas cercano al
    asfalto (el minimo si es el izquierdo, el maximo si es el derecho).
    """
    n = len(evid)
    evi_i, evi_d = [None] * n, [None] * n
    for j in range(n):
        ds = sorted(evid[j])
        if not ds:
            continue
        cumulos, actual = [], [ds[0]]
        for d in ds[1:]:
            if d - actual[-1] > hueco_m:
                cumulos.append(actual)
                actual = [d]
            else:
                actual.append(d)
        cumulos.append(actual)
        centro = ((env_i[j] + env_d[j]) / 2.0
                  if env_i[j] is not None and env_d[j] is not None else 0.0)
        # cada cumulo va al lado que le toca; si hay varios del mismo lado (el
        # piano y, 7 m mas alla, el pasto de un trompo) manda el mas cercano al
        # asfalto: el borde es el primero que se encuentra saliendo
        for c in cumulos:
            medio = (c[0] + c[-1]) / 2.0
            if medio > centro + 0.5:
                izq = True
            elif medio < centro - 0.5:
                izq = False
            else:                               # cruza la referencia: en curva, piano interior
                izq = curv[j] > 0
            if izq:
                if evi_i[j] is None or min(c) < evi_i[j]:
                    evi_i[j] = min(c)
            elif evi_d[j] is None or max(c) > evi_d[j]:
                evi_d[j] = max(c)
    return evi_i, evi_d


def _rellenar(serie, paso, defecto, maximo=RELLENO_MAX_M):
    """Interpola huecos cortos (< `maximo` m) y estima los largos con `defecto`
    (que puede ser None: queda el hueco). Devuelve (serie, [bool] estimado)."""
    n = len(serie)
    RELLENO_MAX_M_ = maximo
    out, est = list(serie), [False] * n
    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        a = out[i - 1] if i > 0 else None
        b = out[j] if j < n else None
        largo = (j - i) * paso
        for k in range(i, j):
            if a is not None and b is not None and largo <= RELLENO_MAX_M_:
                out[k] = a + (b - a) * (k - i + 1) / (j - i + 1)
            elif a is not None and b is None and largo <= RELLENO_MAX_M_:
                out[k] = a
            elif b is not None and a is None and largo <= RELLENO_MAX_M_:
                out[k] = b
            else:
                out[k] = defecto
                est[k] = True
        i = j
    return out, est


# Entre dos evidencias de borde del mismo lado se interpola hasta aca. CORTO a
# proposito: los desplazamientos son relativos a la linea de referencia, que es
# una linea de carrera y cruza la pista de lado a lado en cada curva. Interpolar
# a 150 m unia el piano exterior de la entrada con el interior de la salida y
# daba un "borde" que copiaba la trazada -- Cordoba salia con 2.8 m de ancho
# con evidencia en el 100% de los metros. En 30 m la referencia se mueve poco.
EVIDENCIA_MAX_M = 30.0
ANCHO_PISTA_M = 12.0       # ancho de una pista comun, cuando no hay con que medirlo


def _mediana(xs, defecto=None):
    xs = sorted(x for x in xs if x is not None)
    return xs[len(xs) // 2] if xs else defecto


def _componer_bordes(env_i, env_d, evi_i, evi_d):
    """Los dos bordes, metro a metro, con tres fuentes en este orden:

      1. EVIDENCIA (piano o pasto bajo una rueda), interpolada entre puntos
         hasta EVIDENCIA_MAX_M. Es el borde real.
      2. La ENVOLVENTE de asfalto recorrido, si sale mas afuera que lo anterior
         (una rueda con asfalto debajo mas alla de un "piano" es asfalto, y
         punto). Sin evidencia cerca, la envolvente es un piso, no el borde.
      3. Donde falta evidencia de UN lado: ese lado va al ANCHO MEDIANO de la
         pista desde el lado conocido. Donde faltan los dos: el ancho mediano
         centrado en lo recorrido. Ambos marcados como estimados.

    Por que el ancho y no "la mediana de cada lado": la linea de referencia va
    pegada a los pianos, asi que la distancia mediana de ella a la evidencia
    de un lado es chica y no dice nada del otro lado. El ancho entre las dos
    evidencias si es una propiedad de la pista. Antes se usaba la envolvente
    sola y con 4 vueltas la trazada quedaba pegada al "borde" en toda la
    salida de la curva (T5 de Cordoba: se sale por la mitad y el mapa decia que
    por el limite).

    Devuelve (izq, der, [bool] estimado izq, [bool] estimado der).
    """
    n = len(env_i)
    ei, sin_i = _rellenar(evi_i, PASO_M, None, EVIDENCIA_MAX_M)
    ed, sin_d = _rellenar(evi_d, PASO_M, None, EVIDENCIA_MAX_M)
    # ancho mediano: evidencia de los dos lados en el mismo metro; si no hay,
    # la envolvente mas un metro por lado; si tampoco, 10 m (una pista comun)
    # el ancho se mide solo donde hay evidencia de los DOS lados en el mismo
    # metro; con pocas vueltas eso puede no pasar nunca, y ahi vale mas un ancho
    # de pista comun que la envolvente de 4 vueltas
    # y solo pares de mas de 6 m: menos que eso no es una pista, es un piano
    # leido desde los dos lados que se le escapo a _lados_evidencia
    pares = [ei[i] - ed[i] for i in range(n)
             if not sin_i[i] and not sin_d[i] and ei[i] is not None and ed[i] is not None
             and ei[i] - ed[i] > 6.0]
    ancho = _mediana(pares) if len(pares) >= 10 else ANCHO_PISTA_M
    env_ancho = _mediana([env_i[i] - env_d[i] for i in range(n)
                          if env_i[i] is not None and env_d[i] is not None], 0.0)
    ancho = max(ancho, env_ancho + 1.0)
    izq, der = [None] * n, [None] * n
    est_i, est_d = [True] * n, [True] * n
    for i in range(n):
        li = None if sin_i[i] else ei[i]
        ld = None if sin_d[i] else ed[i]
        if li is not None and ld is None:
            ld = li - ancho
        elif ld is not None and li is None:
            li = ld + ancho
        est_i[i], est_d[i] = sin_i[i], sin_d[i]
        izq[i], der[i] = li, ld
    # Los metros sin evidencia de NINGUN lado quedan en None y se interpolan
    # entre los bordes ya compuestos (hasta 400 m): un borde de pista es una
    # curva continua, y saltar de "piano + ancho" a "centrado en lo recorrido"
    # dibujaba escalones de 3 m justo donde termina el piano de salida
    # (Cordoba T5). Solo donde ni asi hay dato se centra en lo recorrido.
    izq, _ = _rellenar(izq, PASO_M, None, 400.0)
    der, _ = _rellenar(der, PASO_M, None, 400.0)
    for i in range(n):
        if izq[i] is None or der[i] is None:
            c = ((env_i[i] + env_d[i]) / 2.0
                 if env_i[i] is not None and env_d[i] is not None else 0.0)
            izq[i], der[i] = c + ancho / 2.0, c - ancho / 2.0
        # la envolvente de asfalto siempre manda si sale mas afuera
        if env_i[i] is not None and env_i[i] > izq[i]:
            izq[i], est_i[i] = env_i[i], False
        if env_d[i] is not None and env_d[i] < der[i]:
            der[i], est_d[i] = env_d[i], False
    # suavizado corto (10 m): la evidencia va a 0.25 m y los empalmes entre
    # tramos con distinta fuente dejan dientes que no son de la pista
    izq = _suavizar(izq, 2)
    der = _suavizar(der, 2)
    return izq, der, est_i, est_d, round(ancho, 1), len(pares)


def _suavizar(xs, r):
    n = len(xs)
    out = list(xs)
    for i in range(n):
        a, b = max(0, i - r), min(n, i + r + 1)
        out[i] = sum(xs[a:b]) / (b - a)
    return out


def bordes(folder: str) -> dict:
    """Los bordes aprendidos de la pista de esta sesion, para dibujar debajo de
    las trazadas. Se calculan con TODAS las vueltas grabadas en esa pista y
    variante (propias, de cualquier sesion) y se cachean en telemetry/_cache.
    """
    meta = _meta(folder)
    clave = (meta.get("track"), meta.get("track_variation"))
    if not clave[0]:
        raise ValueError("la sesion no dice en que pista es")
    # todas las trazas de esa pista, en el corpus propio (las importadas de .srt
    # traen el terreno en 0.0 y dirian que todo es asfalto)
    archivos = []
    if os.path.isdir(TELEM):
        for d in os.listdir(TELEM):
            ruta = os.path.join(TELEM, d)
            if not os.path.isdir(ruta) or d.startswith("_"):
                continue
            m = _meta(ruta)
            if (m.get("track"), m.get("track_variation")) != clave:
                continue
            for f in os.listdir(ruta):
                if f.endswith(".csv.gz") and f[:1] in "LX":
                    archivos.append(os.path.join(ruta, f))
    # la version va en la firma: cambiar el calculo sin cambiarla serviria el
    # cache viejo hasta que entre una traza nueva
    firma = ["v7", len(archivos), max((os.path.getmtime(f) for f in archivos), default=0)]
    limpio = "".join(c if c.isalnum() else "_" for c in f"{clave[0]}__{clave[1]}")
    cache_dir = os.path.join(TELEM, "_cache")
    cache = os.path.join(cache_dir, f"bordes__{limpio}.json")
    try:
        with open(cache, encoding="utf-8") as f:
            j = json.load(f)
        if j.get("firma") == firma and j.get("ref") == os.path.basename(folder):
            return j
    except (OSError, ValueError):
        pass

    xs_m, cx, cz = _centro_ref(folder)
    cols = ("lap_dist", "pos_x", "pos_z", "terrain_FL", "terrain_FR", "terrain_RL", "terrain_RR")
    env_i, env_d, pianos, fuera, evi_i, evi_d, n_v = _acumular_bordes(
        cx, cz, PASO_M, (_leer_columnas(f, cols) for f in archivos))
    izq, der, est_i, est_d, ancho, n_pares = _componer_bordes(env_i, env_d, evi_i, evi_d)
    n = len(cx)

    def punto(i, d):
        a, b = max(0, i - 1), min(n - 1, i + 1)
        dx, dz = cx[b] - cx[a], cz[b] - cz[a]
        L = math.hypot(dx, dz) or 1.0
        return [round(cx[i] - dz / L * d, 1), round(cz[i] + dx / L * d, 1)]

    out = {
        "ref": os.path.basename(folder), "firma": firma,
        "pista": clave[0], "variante": clave[1],
        "n_vueltas": n_v, "n_sesiones": len({os.path.dirname(f) for f in archivos}),
        "paso_m": PASO_M,
        "ancho_m": ancho,                 # ancho mediano usado donde falta evidencia
        "ancho_medido": n_pares >= 10,    # False = ANCHO_PISTA_M por defecto
        "metros": [round(x, 1) for x in xs_m],
        "bi": [punto(i, izq[i]) for i in range(n)],      # borde izquierdo (x, z)
        "bd": [punto(i, der[i]) for i in range(n)],      # borde derecho
        "estimado": [bool(a or b) for a, b in zip(est_i, est_d)],
        "pianos": [punto(i, d) for i, d in pianos],
        "fuera": [punto(i, d) for i, d in fuera],
        "medido_pct": round(100 * (1 - sum(1 for a, b in zip(est_i, est_d) if a or b) / max(n, 1))),
    }
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except OSError:
        pass
    return out


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
    import zlib
    primera = True
    for v in vs:
        ruta = os.path.join(folder, v["traza"])
        # Una vuelta a la vez en memoria (~2 MB), no toda la tanda: asi un gzip
        # cortado (sesion en curso, o un taskkill de antes del volcado atomico) se
        # salta ENTERO en vez de dejar media vuelta en el CSV y reventar el stream.
        try:
            with gzip.open(ruta, "rt", encoding="utf-8") as f:
                cab = f.readline().rstrip("\n")
                lineas = [ln for ln in f if ln.strip()]
        except (OSError, EOFError, zlib.error):
            continue
        if primera:
            yield "lap,uid," + cab + "\n"
            primera = False
        pre = f"{v['vuelta']},{v.get('uid') or 0},"
        for ln in lineas:
            yield pre + ln
