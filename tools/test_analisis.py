#!/usr/bin/env python3
"""Tests de la capa de analisis del visor (ams2_analysis) y de su API HTTP.

Corren contra el CORPUS REAL en telemetry/, no contra mocks: lo que se quiere
verificar aca es precisamente que la lectura del dato grabado cuadra consigo
misma (largo de pista, cronometro, emparejamiento traza<->vuelta). Un mock
diria que si a cualquier cosa.

Correr:  .venv\\Scripts\\python.exe tools/test_analisis.py
"""
import json
import math
import os
import sys
import threading
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

import ams2_analysis as A          # noqa: E402
import analyze_telemetry as AT     # noqa: E402

PUERTO = 8099                      # NO 8080: el bridge de verdad puede estar arriba
_fallos = 0


def ok(nombre, cond, extra=""):
    global _fallos
    if not cond:
        _fallos += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {nombre}{(' -- ' + str(extra)) if extra else ''}")
    return cond


def _sesion_con_tandas():
    """Una sesion del corpus con varias tandas y trazas (para los tests de forma)."""
    for d in sorted(AT._sessions(), key=os.path.getmtime, reverse=True):
        ss = A.stints(d)
        if len(ss) >= 2 and sum(s["con_traza"] for s in ss) >= 4:
            return d
    return None


def _hay_corpus():
    """True si hay sesiones grabadas. En un clon fresco telemetry/ NO existe
    (esta gitignoreado): ahi los tests de corpus se SALTAN en vez de fallar --
    la ausencia de datos no es un defecto del codigo. Reventar aca fue un bug
    real: el traceback abortaba main() en el 4o test y los que NO necesitan
    corpus (tanda sintetica, frontera, API) nunca corrian."""
    return bool(AT._sessions())


def test_stints():
    print("\ntandas (stints):")
    d = _sesion_con_tandas()
    if not ok("hay una sesion del corpus con >=2 tandas y trazas", d is not None):
        return
    ss = A.stints(d)
    ok("las tandas se numeran 1..n sin huecos", [s["n"] for s in ss] == list(range(1, len(ss) + 1)))
    # una vuelta de boxes NO puede estar dentro de una tanda: es el borde
    dentro = [v for s in ss for v in s["vueltas"] if v["tipo"] == "pit"]
    ok("ninguna vuelta de boxes quedo dentro de una tanda", not dentro, f"{len(dentro)} coladas")
    ok("ninguna tanda queda vacia", all(s["n_vueltas"] > 0 for s in ss))
    # cada vuelta cruzada aparece exactamente una vez o ninguna, nunca dos
    todas = [id(v) for s in ss for v in s["vueltas"]]
    ok("ninguna vuelta esta en dos tandas", len(todas) == len(set(todas)))
    # conservacion: toda vuelta cruzada esta en una tanda O es vuelta de boxes.
    # Es el invariante que caza el bug de las tandas fantasma sin castigar a las
    # tandas de una vuelta, que SI existen (saliste, diste una y volviste).
    malas = 0
    for d in AT._sessions():
        vs = A._vueltas(d)
        en_tandas = sum(s["n_vueltas"] for s in A.stints(d))
        boxes = sum(1 for v in vs if v["tipo"] == "pit")
        if en_tandas + boxes != len(vs):
            malas += 1
    ok("en todo el corpus: vueltas en tandas + vueltas de boxes = vueltas cruzadas",
       malas == 0, f"{malas} sesiones descuadradas")


def test_emparejamiento():
    """La traza que se le asigna a una vuelta tiene que ser LA SUYA.

    El contador de vueltas de AMS2 se reinicia al volver al garage, asi que el
    numero de vuelta no identifica nada. Se verifica en todo el corpus que el
    archivo asignado codifica el mismo numero y el mismo tiempo que la vuelta.
    """
    print("\nemparejamiento vuelta <-> traza (todo el corpus):")
    mal, total, sesiones = [], 0, 0
    for d in AT._sessions():
        vs = [v for v in A._vueltas(d) if v.get("traza")]
        if not vs:
            continue
        sesiones += 1
        for v in vs:
            total += 1
            arch = v["traza"]
            try:
                n = int(arch[1:4])
                t = float(arch.split("_")[1].rstrip("s.csvgz."))
            except (ValueError, IndexError):
                mal.append((d, arch, "nombre ilegible"))
                continue
            if n != v["vuelta"] or abs(t - (v["tiempo"] or 0)) > 0.002:
                mal.append((os.path.basename(d), arch, f"vuelta={v['vuelta']} t={v['tiempo']}"))
    ok(f"{total} trazas de {sesiones} sesiones apuntan a su propia vuelta", not mal, mal[:3])
    ok("hay corpus suficiente para que el test signifique algo", total > 100, total)


def test_remuestreo():
    print("\nremuestreo por distancia:")
    peor = (None, 0.0)
    for d in AT._sessions():
        meta = A._meta(d)
        largo = meta.get("track_length_m")
        vs = [v for v in A._vueltas(d) if v.get("traza")]
        if not largo or not vs:
            continue
        m = A.mapa(d)
        err = abs(m["largo_m"] - largo) / largo
        if err > peor[1]:
            peor = (os.path.basename(d), err)
    ok("el trazado remuestreado cierra el largo de pista (<1.5%)", peor[1] < 0.015,
       f"peor: {peor[0]} {peor[1]*100:.2f}%")


def test_delta():
    """El delta entre dos vueltas propias tiene que cerrar contra el cronometro.

    Es la validacion que decide si el trazo sirve para leer donde se pierde
    tiempo o es decoracion. Si no cierra, el grafico miente en silencio.

    Dos assertions distintas, y la segunda es la que NO es tautologica: el
    cierre en meta fuerza el ultimo punto con el cronometro, asi que el delta
    cierra por construccion. Lo que hay que vigilar es cuanto tiempo hubo que
    completar: si `cierre_s` es chico, la traza cubria la vuelta de verdad y el
    delta ya era correcto antes del cierre. Si creciera, el grafico estaria
    apoyado en una extrapolacion y habria que decirlo.
    """
    print("\ndelta de tiempo contra el cronometro:")
    peor, n, cierres = (None, 0.0), 0, []
    if not _hay_corpus():
        print("  (sin corpus grabado; nada que medir)")
        return
    for d in sorted(AT._sessions(), key=os.path.getmtime, reverse=True)[:30]:
        vs = [v for v in A._vueltas(d) if v.get("traza") and v.get("tiempo")]
        if len(vs) < 2:
            continue
        ref = min(vs, key=lambda v: v["tiempo"])
        tr_ref = A.traza(d, ref["traza"])
        cierres.append(tr_ref["cierre_s"])
        for v in vs:
            if v is ref:
                continue
            tv = A.traza(d, v["traza"])
            cierres.append(tv["cierre_s"])
            dl = A.delta(tv, tr_ref)
            if not dl:
                continue
            n += 1
            err = abs(dl[-1] - (v["tiempo"] - ref["tiempo"]))
            if err > peor[1]:
                peor = (f"{os.path.basename(d)} L{v['vuelta']}", err)
    ok(f"{n} deltas cierran dentro de 25 ms", n > 20 and peor[1] < 0.025,
       f"peor: {peor[0]} {peor[1]*1000:.0f} ms")
    # Umbrales medidos sobre 138 vueltas de 30 sesiones: mediana 0 ms, p90 11 ms,
    # p99 227 ms. Los outliers salen TODOS de una misma carrera de Road Atlanta,
    # la que ademas tiene 44% de muestras duplicadas -> el hilo grabador se quedo
    # sin CPU en esa sesion. Es un problema de esa grabacion, no del pipeline; por
    # eso el test mide la MASA (mediana y percentil) y no el maximo.
    cierres.sort()
    med = cierres[len(cierres) // 2]
    bajo = sum(1 for c in cierres if c < 0.050) / len(cierres)
    ok("la traza cubre la vuelta: cierre mediano bajo un periodo de muestreo (20 ms)",
       med < 0.020, f"mediana={med*1000:.0f} ms")
    ok("90% de las vueltas cierran con menos de 50 ms", bajo >= 0.90,
       f"{bajo*100:.0f}%  max={cierres[-1]*1000:.0f} ms")


def test_curvas():
    print("\nnumeracion de curvas:")
    d = _sesion_con_tandas()
    m = A.mapa(d)
    c = m["curvas"]
    ok("detecta curvas", len(c) >= 5, f"{len(c)} en {A._meta(d).get('track')}")
    ok("van numeradas en orden de pista", [x["n"] for x in c] == sorted(x["n"] for x in c))
    ok("los metros van creciendo", all(c[i]["metro"] < c[i + 1]["metro"] for i in range(len(c) - 1)))
    ok("todas dentro del largo de la vuelta", all(0 <= x["metro"] <= m["largo_m"] for x in c))
    ok("todas tienen lado y radio", all(x["lado"] in ("izq", "der") and x["radio_m"] for x in c))


def test_tanda_de_verdad():
    """Una tanda como la que se gira: sales de boxes, das 5 vueltas (una se
    invalida) y vuelves a entrar. .Que ve el visor?

    Es la unica prueba que recorre el camino NUEVO completo -- grabador ->
    timeline con `trace` -> tandas -> traza remuestreada -> CSV. Todo lo demas
    se valida contra el corpus viejo, que no tiene nada de esto.
    """
    print("\ntanda completa de punta a punta (grabador -> visor -> CSV):")
    import shutil
    import tempfile
    sys.path.insert(0, os.path.join(HERE, "tools"))
    import test_telemetry as TT
    import ams2_telemetry as T

    base = tempfile.mkdtemp(prefix="ams2stint_")
    try:
        log = T.TelemetryLogger(base_dir=base)
        # `feed_lap(N)` alimenta la vuelta con mLapsCompleted=N y `cross_to(N+1)`
        # la cierra: cada par es UNA vuelta completa.
        TT.feed_lap(log, 0, pit=2)              # sales del garage, cruzas en boxes
        TT.cross_to(log, 1)
        TT.feed_lap(log, 1)                     # out-lap (calentamiento)
        TT.cross_to(log, 2)
        for n in range(2, 7):                   # 5 VUELTAS DE PISTA, la 3a invalidada
            TT.feed_lap(log, n, invalid=(n == 4))
            TT.cross_to(log, n + 1)
        TT.feed_lap(log, 7, pit=1)              # vuelta de entrada a boxes
        TT.cross_to(log, 8)

        d = TT.session_dir(base)
        ss = A.stints(d)
        ok("el visor ve UNA tanda", len(ss) == 1, [s["n_vueltas"] for s in ss])
        if not ss:
            return
        s = ss[0]
        tipos = [v["tipo"] for v in s["vueltas"]]
        ok("la tanda arranca en la vuelta de salida", tipos[0] == "out", tipos)
        ok("la vuelta de entrada a boxes NO esta en la tanda", "pit" not in tipos, tipos)
        con = [v for v in s["vueltas"] if v["traza"]]
        ok("de 5 vueltas de pista quedan 5 con traza (incluida la invalidada)",
           len(con) == 5, f"{len(con)} trazas de {s['n_vueltas']} vueltas: {tipos}")
        ok("la out-lap NO tiene traza (es de calentamiento)",
           not s["vueltas"][0]["traza"])
        inval = [v for v in con if not v["valida"]]
        ok("la invalidada esta, marcada como invalida y con traza X",
           len(inval) == 1 and inval[0]["traza"].startswith("X"),
           [v["traza"] for v in inval])
        # el resumen (corpus del resto de las herramientas) NO la incluye
        res = AT._read_jsonl(os.path.join(d, "summary.jsonl"))
        ok("summary.jsonl sigue teniendo solo las 4 limpias", len(res) == 4, len(res))

        tr = A.traza(d, con[0]["traza"])
        ok("la traza se puede remuestrear por distancia", len(tr["metros"]) > 100,
           f"{len(tr['metros'])} puntos")
        filas = list(A.stint_csv(d, 1))
        vueltas_csv = {ln.split(",", 1)[0] for ln in filas[1:]}
        ok("el CSV de la tanda trae las 5 vueltas", len(vueltas_csv) == 5, sorted(vueltas_csv))
        ok("el CSV lleva cabecera con lap y uid", filas[0].startswith("lap,uid,t,"))
        solo_ok = list(A.stint_csv(d, 1, solo_validas=True))
        v_ok = {ln.split(",", 1)[0] for ln in solo_ok[1:]}
        ok("con 'solo validas' quedan 4", len(v_ok) == 4, sorted(v_ok))

        # una traza que se anuncia pero no llego al disco no puede romper el visor
        os.remove(os.path.join(d, con[0]["traza"]))
        ok("si falta el archivo, esa vuelta queda sin traza (no revienta)",
           len([v for v in A.stints(d)[0]["vueltas"] if v["traza"]]) == 4)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _sintetica(frena=True, largo=1200.0, paso=2.0):
    """Una vuelta de laboratorio con los cuatro metros clave PUESTOS a mano.

    Es el unico test del archivo que NO usa el corpus, y a proposito: contra una
    vuelta real solo se puede mirar el numero y opinar si parece razonable. Aca
    la recta, la frenada, el giro, el apex y el a-fondo estan en un metro exacto
    conocido, asi que un cambio de criterio que corra el punto 20 m se cae.

    Con `frena=False` sale una curva DE APOYO: se pasa sin tocar el freno pero
    se dobla igual (el caso que un detector ingenuo marca como "sin curva").
    """
    metros = [round(i * paso, 1) for i in range(int(largo / paso) + 1)]
    spd, thr, brk, st = [], [], [], []
    for m in metros:
        if frena:
            # frena 812 · gira 856 · apex 900 (96.3 km/h) · a fondo 930
            if m < 812:
                v, t, b, s = 200.0, 1.0, 0.0, 0.0
            elif m < 900:
                v = 200.0 - (m - 812) / 88.0 * (200.0 - 96.3)
                t, b = 0.0, 0.6
                s = 0.5 if m >= 856 else 0.0
            elif m < 930:
                v, t, b, s = 96.3 + (m - 900) / 30.0 * 20.0, 0.40, 0.0, 0.5
            else:
                v, t, b = 116.3 + (m - 930) * 0.3, 1.0, 0.0
                s = max(0.0, 0.5 - (m - 930) * 0.01)
        else:
            # curva de apoyo: gira 360 · apex 400 (210 km/h) · a fondo 420, sin freno
            b = 0.0
            if m < 360:
                v, t, s = 240.0 - (m / 360.0) * 20.0, 0.85, 0.0
            elif m < 400:
                v, t, s = 220.0 - (m - 360) / 40.0 * 10.0, 0.85, 0.3
            elif m < 420:
                v, t, s = 210.0 + (m - 400) * 0.2, 0.85, 0.3
            else:
                v, t, s = 214.0 + (m - 420) * 0.1, 1.0, max(0.0, 0.3 - (m - 420) * 0.01)
        spd.append(v); thr.append(t); brk.append(b); st.append(s)
    return {"paso_m": paso, "metros": metros,
            "canales": {"speed_kmh": spd, "throttle": thr, "brake": brk, "steer": st}}


def test_eventos():
    print("\npuntos clave por curva (frenada / giro / apex / a fondo):")
    tr = _sintetica(frena=True)
    cu = [{"n": 1, "inicio": 860.0, "fin": 920.0, "metro": 900.0, "lado": "der"}]
    e = A.eventos(tr, cu)[0]
    ok("apex = minimo de velocidad de la curva", e["apex_m"] == 900.0, e["apex_m"])
    ok("vmin es la velocidad en el apex", abs(e["vmin_kmh"] - 96.3) < 0.05, e["vmin_kmh"])
    ok("frenada = donde EMPIEZA de verdad, no donde se pisa cerca del apex",
       e["frenada_m"] == 812.0, e["frenada_m"])
    ok("giro = primer volante sostenido", e["giro_m"] == 856.0, e["giro_m"])
    ok("a fondo = primer acelerador clavado a la salida", e["gas_m"] == 930.0, e["gas_m"])

    # modulacion: soltar el freno 10 m en mitad de la frenada NO parte el punto
    tr2 = _sintetica(frena=True)
    for i, m in enumerate(tr2["metros"]):
        if 850 <= m < 860:
            tr2["canales"]["brake"][i] = 0.0
    ok("un hueco de 10 m dentro de la frenada no la corta en dos",
       A.eventos(tr2, cu)[0]["frenada_m"] == 812.0, A.eventos(tr2, cu)[0]["frenada_m"])

    # curva de apoyo: sin freno, pero con giro
    tra = _sintetica(frena=False)
    ca = [{"n": 1, "inicio": 370.0, "fin": 430.0, "metro": 400.0, "lado": "izq"}]
    ea = A.eventos(tra, ca)[0]
    ok("curva de apoyo: frenada None (no se invento un punto)", ea["frenada_m"] is None, ea["frenada_m"])
    ok("curva de apoyo: el giro SI se detecta (umbral adaptativo)",
       ea["giro_m"] == 360.0, ea["giro_m"])
    ok("curva de apoyo: apex donde baja la velocidad", ea["apex_m"] == 400.0, ea["apex_m"])
    ok("curva de apoyo: a fondo a la salida", ea["gas_m"] == 420.0, ea["gas_m"])

    # Un kink que se pasa PLANO despues de una curva frenada. Las curvas salen de
    # la curvatura, asi que la lista incluye estos: sin el tope del apex anterior,
    # la busqueda hacia atras se come la frenada de la curva de antes y le anota
    # al kink un punto de frenada que esta dentro de la curva anterior (medido en
    # Cordoba: "T4 frena a 398 m del apex", 200 m adentro de T3).
    dos = A.eventos(tr, [{"n": 1, "inicio": 860.0, "fin": 920.0, "metro": 900.0},
                         {"n": 2, "inicio": 1000.0, "fin": 1060.0, "metro": 1030.0}])
    ok("un kink plano NO hereda la frenada de la curva anterior",
       dos[1]["frenada_m"] is None, dos[1]["frenada_m"])
    ok("y la curva frenada de antes conserva la suya", dos[0]["frenada_m"] == 812.0)

    # bordes: una curva que cae fuera de la traza no puede reventar ni inventar filas
    fuera = A.eventos(tr, [{"n": 9, "inicio": 5000.0, "fin": 5060.0, "metro": 5030.0}])
    ok("una curva fuera de la traza se omite, no revienta", fuera == [], fuera)
    ok("sin canales no devuelve nada", A.eventos({"metros": [], "canales": {}}, cu) == [])
    ok("sin canal de volante el resto sigue saliendo",
       A.eventos({"paso_m": 2.0, "metros": tr["metros"],
                  "canales": {k: v for k, v in tr["canales"].items() if k != "steer"}},
                 cu)[0]["giro_m"] is None)


def test_frontera():
    """La API queda expuesta en la LAN: el nombre de sesion es entrada no confiable."""
    print("\nfrontera de confianza (nombre de sesion):")
    for malo in ("..", "../..", "../../Windows/System32", "/etc/passwd",
                 "C:\\Windows", "algo/../..", "", "sub\\dir"):
        try:
            A._carpeta(malo)
            ok(f"rechaza {malo!r}", False)
        except ValueError:
            ok(f"rechaza {malo!r}", True)


def test_api():
    print("\nAPI HTTP:")
    try:
        import bridge_shm
    except Exception as e:                       # noqa: BLE001
        ok("bridge_shm importa", False, e)
        return
    import http.server
    h = lambda *a, **kw: bridge_shm._NoCacheHandler(*a, directory=HERE, **kw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PUERTO), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{PUERTO}"

    def get(ruta):
        try:
            with urllib.request.urlopen(base + ruta, timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()        # los 4xx son respuestas a verificar, no fallas

    def jget(ruta):
        c, b = get(ruta)
        return c, json.loads(b)
    try:
        c, j = jget("/api/sesiones")
        ok("/api/sesiones responde con lista (vacia es valido: clon fresco)",
           c == 200 and isinstance(j.get("sesiones"), list), c)
        if not j["sesiones"]:
            print("  (sin sesiones; se prueban igual la frontera y los errores)")
            c, _ = jget("/api/sesion?s=..")
            ok("path traversal por HTTP -> 400", c == 400, c)
            c, _ = jget("/api/nada?s=x")
            ok("ruta desconocida sin sesion -> 4xx", c in (400, 404), c)
            return
        s = j["sesiones"][0]["carpeta"]
        # una sesion con trazas, que es lo que el visor necesita
        s = next((x["carpeta"] for x in j["sesiones"] if x["vueltas"] > 1), s)
        c, j = jget(f"/api/sesion?s={s}")
        ok("/api/sesion trae tandas", c == 200 and "tandas" in j, c)
        c, j = jget(f"/api/mapa?s={s}")
        ok("/api/mapa trae trazado y curvas", c == 200 and len(j["x"]) > 100 and j["curvas"], c)
        traza = None
        _, js = jget(f"/api/sesion?s={s}")
        for t in js["tandas"]:
            for v in t["vueltas"]:
                if v.get("traza"):
                    traza = v["traza"]
                    break
            if traza:
                break
        c, j = jget(f"/api/traza?s={s}&t={traza}")
        ok("/api/traza trae canales alineados por metro",
           c == 200 and len(j["metros"]) == len(j["canales"]["speed_kmh"]), c)
        ok("la traza incluye el reloj (sin el no hay delta)", "t" in j["canales"])
        # export: cabecera con lap/uid y filas de mas de una vuelta
        c, b = get(f"/api/export?s={s}&tanda=1")
        txt = b.decode("utf-8")
        lineas = txt.splitlines()
        ok("/api/export devuelve CSV", c == 200 and lineas[0].startswith("lap,uid,"), c)
        vueltas = {ln.split(",", 1)[0] for ln in lineas[1:] if ln}
        ok("el CSV separa las vueltas por la columna lap", len(vueltas) >= 1, sorted(vueltas))
        ok("el CSV tiene filas de verdad", len(lineas) > 500, len(lineas))
        cols = len(lineas[0].split(","))
        ok("todas las filas tienen el mismo ancho que la cabecera",
           all(len(ln.split(",")) == cols for ln in lineas[1:] if ln), cols)
        # errores: no pueden tumbar el server ni devolver 200
        c, j = jget("/api/sesion?s=..")
        ok("path traversal por HTTP -> 400", c == 400, c)
        c, j = jget("/api/nada?s=" + s)
        ok("ruta desconocida -> 404", c == 404, c)
    except urllib.error.HTTPError as e:
        ok("la API no revienta", False, f"{e.code} {e.read()[:200]}")
    finally:
        srv.shutdown()


def test_bordes():
    """Bordes aprendidos sobre una recta sintetica donde se sabe donde esta cada
    rueda: dos vueltas a +2 y -2 m del centro, la de +2 con las ruedas
    izquierdas sobre piano. El asfalto tiene que llegar hasta la rueda mas
    exterior con asfalto y el piano quedar donde estaba la rueda."""
    print("\nbordes aprendidos (sintetico):")
    paso, n = 2.0, 101                          # recta de 200 m sobre +x
    cx = [i * paso for i in range(n)]
    cz = [0.0] * n

    def vuelta(z, piano_izq=False, hasta=200.0):
        xs = [x * 1.0 for x in range(0, int(hasta) + 1, 2)]
        m = len(xs)
        d = {"lap_dist": xs, "pos_x": xs, "pos_z": [z] * m,
             "terrain_FL": [10.0 if piano_izq else 0.0] * m,
             "terrain_RL": [10.0 if piano_izq else 0.0] * m,
             "terrain_FR": [0.0] * m, "terrain_RR": [0.0] * m}
        return d

    izq, der, pianos, fuera, evi_i, evi_d, nv = A._acumular_bordes(cx, cz, paso, [vuelta(2.0, True), vuelta(-2.0)])
    ok("cuenta las 2 vueltas", nv == 2)
    ev = [v for v in evi_i[5:-5] if v is not None]
    ok("la evidencia de borde izquierdo es el inicio del piano (2.85 m, a 0.25 de resolucion)",
       ev and all(abs(v - 2.85) <= 0.15 for v in ev), f"{ev[:1]}")
    ok("sin piano ni pasto a la derecha no hay evidencia de ese lado", all(v is None for v in evi_d))
    # el mismo piano pisado desde los dos lados de la referencia es UN borde:
    # muestras en -0.5 y +0.75 en un metro de curva a la izquierda -> borde
    # izquierdo en -0.5 (donde empieza el piano), nada a la derecha
    ev = [set() for _ in range(5)]
    ev[2] = {-0.5, 0.0, 0.75}
    ei, ed = A._lados_evidencia(ev, [1.0]*5, [-1.0]*5, [0.02]*5)
    ok("un piano que cruza la referencia es un solo borde, del lado interior de la curva",
       ei[2] == -0.5 and ed[2] is None, f"izq={ei[2]} der={ed[2]}")
    ev[2] = {-6.0, -5.5, 5.5, 6.0}
    ei, ed = A._lados_evidencia(ev, [1.0]*5, [-1.0]*5, [0.02]*5)
    ok("un cumulo a cada lado del asfalto son los dos bordes (donde empieza cada uno)",
       ed[2] == -5.5 and ei[2] == 5.5, f"izq={ei[2]} der={ed[2]}")
    # dos cumulos del MISMO lado (piano a 1 m, pasto de un trompo a 8 m): manda
    # el mas cercano al asfalto, y el otro lado queda sin evidencia
    ev[2] = {1.0, 1.25, 8.0, 8.5}
    ei, ed = A._lados_evidencia(ev, [1.0]*5, [-1.0]*5, [0.02]*5)
    ok("dos cumulos del mismo lado no son dos bordes: manda el mas cercano",
       ei[2] == 1.0 and ed[2] is None, f"izq={ei[2]} der={ed[2]}")
    # composicion: con evidencia manda la evidencia (2.85), no la envolvente (1.15)
    bi, bd, esti, estd, ancho, pares = A._componer_bordes(izq, der, evi_i, evi_d)
    ok("el borde izquierdo compuesto es la evidencia, no la envolvente",
       abs(bi[50] - 2.85) <= 0.15 and not esti[50], f"{bi[50]:.2f} est={esti[50]}")
    # sin evidencia de los dos lados en ningun metro, el ancho es el de una
    # pista comun (12 m) y el borde derecho va a esa distancia del izquierdo
    ok("sin pares de evidencia el ancho es el de pista comun", pares == 0 and ancho == A.ANCHO_PISTA_M, f"{ancho} m")
    ok("sin evidencia, el borde derecho va al ancho desde el izquierdo, marcado estimado",
       abs(bd[50] - (2.85 - 12.0)) <= 0.15 and estd[50], f"{bd[50]:.2f} est={estd[50]}")
    # rumbo +x: la izquierda del auto es +z. Asfalto: ruedas derechas de la vuelta
    # +2 (z=1.15) y ruedas derechas de la vuelta -2 (z=-2.85); las izquierdas de
    # la -2 (z=-1.15) tambien son asfalto pero no son el extremo.
    mitad = [v for v in izq[5:-5] if v is not None]
    ok("borde izquierdo del asfalto = rueda derecha de la vuelta +2 (1.15 m)",
       mitad and abs(max(mitad) - (2.0 - A.VIA_2)) < 0.05, f"{max(mitad) if mitad else None:.2f}")
    mitad = [v for v in der[5:-5] if v is not None]
    ok("borde derecho del asfalto = rueda derecha de la vuelta -2 (-2.85 m)",
       mitad and abs(min(mitad) - (-2.0 - A.VIA_2)) < 0.05, f"{min(mitad) if mitad else None:.2f}")
    lat = sorted({d for _, d in pianos})
    ok("el piano queda donde estaba la rueda izquierda de la vuelta +2 (~2.85 m)",
       lat and all(abs(d - 2.85) <= 0.25 for d in lat), lat)
    ok("nada 'fuera' de pista en una recta de asfalto", not fuera)

    # relleno: una vuelta que solo cubre los primeros 100 m deja hueco largo
    izq2, der2, _, _, _, _, _ = A._acumular_bordes(cx, cz, paso, [vuelta(1.0, hasta=100.0)])
    lleno, est = A._rellenar(izq2, paso, 5.0)
    ok("el hueco largo se estima con el valor por defecto y se marca",
       lleno[-1] == 5.0 and est[-1] and not est[10], f"{lleno[-1]} est={est[-1]}")
    # hueco corto (10 m) en medio: se interpola, no se estima
    serie = [1.0] * 40 + [None] * 5 + [2.0] * 40
    lleno, est = A._rellenar(serie, paso, 9.0)
    ok("un hueco de 10 m se interpola entre vecinos", abs(lleno[42] - 1.5) < 0.2 and not est[42],
       f"{lleno[42]:.2f}")

    if _hay_corpus():
        d = _sesion_con_tandas()
        if d:
            import time
            t0 = time.time()
            b = A.bordes(d)
            t1 = time.time() - t0
            b2 = A.bordes(d)
            t2 = time.time() - t0 - t1
            ok("bordes de una sesion real: bordes izq/der del largo del trazado",
               len(b["bi"]) == len(b["metros"]) == len(b["bd"]),
               f"{os.path.basename(d)} · {b['n_vueltas']} vueltas de {b['n_sesiones']} sesiones · "
               f"medido {b['medido_pct']}% · {len(b['pianos'])} puntos de piano · {t1:.1f}s")
            ok("la segunda llamada sale del cache", t2 < 0.5, f"{t2:.2f}s")
            anchos = [math.hypot(bi[0]-bd[0], bi[1]-bd[1]) for bi, bd in zip(b["bi"], b["bd"])]
            med = sorted(anchos)[len(anchos)//2]
            # el corredor es lo que cubrieron las ruedas: con 4 vueltas de una
            # sesion son ~3 m, con 154 de Road Atlanta ~10 (medido 2026-09-10).
            # Lo que se protege es que no sea absurdo (0 o un trompo de 30 m).
            ok("ancho del corredor plausible (1.5-25 m)", 1.5 <= med <= 25, f"mediana {med:.1f} m")


# ------------------------------------------------- tu frenada (por corrida) ---
# Pista de laboratorio: un estadio (dos rectas de 1500 m y dos semicirculos de
# 80 m de radio). Los semicirculos dan dos T por curvatura en el visor, y la
# velocidad baja en el medio de cada uno, asi que el apex de la frenada y el de la
# T caen en el mismo metro: el etiquetado T se puede probar de punta a punta.
_R, _RECTA = 80.0, 1500.0
_LARGO_E = 2 * _RECTA + 2 * math.pi * _R
_APEX_E = (_RECTA + math.pi * _R / 2, 2 * _RECTA + math.pi * _R + math.pi * _R / 2)


def _pos_estadio(d):
    arco = math.pi * _R
    if d < _RECTA:
        return d, 0.0
    d -= _RECTA
    if d < arco:
        a = d / _R
        return _RECTA + _R * math.sin(a), _R - _R * math.cos(a)
    d -= arco
    if d < _RECTA:
        return _RECTA - d, 2 * _R
    d -= _RECTA
    a = d / _R
    return -_R * math.sin(a), _R + _R * math.cos(a)


def _vuelta_estadio(frena, ritmo=1.0, n=1800, contacto=False):
    """Filas CSV de una vuelta que EMPIEZA a frenar `frena` = (m1, m2) metros antes
    de cada apex. `ritmo` escala la velocidad entera: cambia el tiempo de vuelta
    sin mover ni el apex ni la frenada. `n` < 1000 = traza truncada. `contacto` =
    un toque a mitad de vuelta (coll_mag cambia de valor UNA vez, como en AMS2)."""
    import ams2_telemetry as T
    idx = {h: i for i, h in enumerate(T.HEADER)}
    filas, t, ds = [], 0.0, _LARGO_E / n
    for k in range(n):
        d = k * ds
        v = 200.0
        for ap in _APEX_E:
            v -= 120.0 * math.exp(-((d - ap) / 120.0) ** 2)
        v *= ritmo
        frenando = any(ap - f <= d < ap - 30.0 for ap, f in zip(_APEX_E, frena))
        x, z = _pos_estadio(d)
        r = [0.0] * len(T.HEADER)
        r[idx["t"]] = round(t, 4)
        r[idx["lap_dist"]] = round(d, 2)
        r[idx["speed_kmh"]] = round(v, 2)
        r[idx["brake"]] = 1.0 if frenando else 0.0
        r[idx["throttle"]] = 0.0 if frenando else 1.0
        r[idx["gear"]] = 4.0
        r[idx["pos_x"]] = round(x, 2)
        r[idx["pos_z"]] = round(z, 2)
        r[idx["coll_idx"]] = 5.0 if contacto and k >= n // 2 else -1.0
        r[idx["coll_mag"]] = 0.3 if contacto and k >= n // 2 else 0.0
        filas.append(",".join(map(str, r)))
        t += ds / (v / 3.6)
    return filas, t


def _sesion_frenadas(base, nombre, corridas, con_timeline=True, n=1800):
    """Sesion sintetica con CORRIDAS de verdad: cada corrida es una lista de
    (frena_m1, frena_m2, ritmo[, extras]); se escribe la vuelta de salida que la abre
    y la de boxes que la cierra, igual que el grabador. `extras` = {"contacto": True,
    "invalida": True}: una invalidada va como X### y solo en la linea de tiempo."""
    import gzip

    import ams2_telemetry as T
    d = os.path.join(base, nombre)
    os.makedirs(d, exist_ok=True)
    fecha = nombre.split("__")[-1]
    with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as f:
        json.dump({"track": "Estadio", "track_variation": "Estadio_GP", "car": "Auto",
                   "session": "practice", "track_length_m": round(_LARGO_E, 1),
                   "started": f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:8]}T{fecha[9:11]}:{fecha[11:13]}:00",
                   "channels": T.HEADER}, f)
    tl, res, uid, lap = [], [], 0, 0
    for c, vueltas in enumerate(corridas):
        if c:
            lap += 1
            tl.append({"type": "lap", "lap": lap, "lap_time": 120.0, "kind": "pit", "pit": True})
        lap += 1
        tl.append({"type": "lap", "lap": lap, "lap_time": 110.0, "kind": "out", "out": True})
        for frena1, frena2, ritmo, *extra in vueltas:
            ex = extra[0] if extra else {}
            filas, lt = _vuelta_estadio((frena1, frena2), ritmo, n, ex.get("contacto", False))
            lap += 1
            inval = ex.get("invalida", False)
            nom = f"{'X' if inval else 'L'}{lap:03d}_{lt:.3f}s.csv.gz"
            with gzip.open(os.path.join(d, nom), "wt", encoding="utf-8") as f:
                f.write(",".join(T.HEADER) + "\n" + "\n".join(filas) + "\n")
            if inval:                     # como el grabador: traza X y linea de tiempo, sin resumen
                tl.append({"type": "lap", "lap": lap, "lap_time": round(lt, 3), "kind": "invalid",
                           "invalid": True, "trace": nom})
                continue
            uid += 1
            reg = {"uid": uid, "lap": lap, "lap_time": round(lt, 3), "valid": True, "trace": nom}
            res.append(reg)
            tl.append({"type": "lap", "lap": lap, "lap_time": round(lt, 3), "kind": "flying",
                       "uid": uid, "trace": nom})
    lap += 1
    tl.append({"type": "lap", "lap": lap, "lap_time": 120.0, "kind": "pit", "pit": True})
    with open(os.path.join(d, "summary.jsonl"), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in res))
    if con_timeline:
        with open(os.path.join(d, "timeline.jsonl"), "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in tl))
    return d


# el frio: 5 vueltas, frenada de T1 a 150 m +-, la de T2 a 100 m +-, 5% mas lento
_OFF_FRIO = ((-10, 0), (10, 4), (-5, -4), (15, 2), (0, -2))
_FRIO = [(150 + a, 100 + b, 0.95) for a, b in _OFF_FRIO]
# despues del coaching: 8 vueltas clavadas en el mismo punto, a ritmo
_TANDA = [(160, 100, 1.0)] * 8


def _sin_t(cs):
    """Las curvas sin lo que frenadas_struct les agrega (t, antes_m)."""
    return [{k: v for k, v in c.items() if k not in ("t", "antes_m")} for c in cs]


def _braking_original(folder, umbral=0.05):
    """`braking_struct` TAL COMO ERA antes de aceptar un subconjunto de vueltas
    (copia literal del calculo, 2026-09). Es la vara contra la que se cuida que
    `--frenadas` sobre la sesion entera no cambie sus numeros: comparar la funcion
    nueva consigo misma no cuidaria nada."""
    import statistics as stt
    cl = AT.clean_laps(folder)
    if cl["n_clean"] < 2:
        return None
    traces = [(rec, AT._lap_trace(folder, rec)) for rec in cl["clean"]]
    traces = [(rec, t) for rec, t in traces if t and t.get("brake") and t.get("lap_dist")]
    completas = [(rec, t) for rec, t in traces if len(t.get("lap_dist") or []) >= 1000]
    descartadas = len(traces) - len(completas)
    traces = completas
    if len(traces) < 2:
        return {"curvas": [], "descartadas": descartadas, "n_vueltas": len(traces)}
    rt = traces[0][1]
    d, sp = AT._mono(rt["lap_dist"], rt["speed_kmh"])[:2]
    out = []
    for c in AT._corners(d, sp):
        puntos = [x for x in (AT._punto_frenada(t, c["apex"], umbral) for _, t in traces)
                  if x is not None]
        if len(puntos) < 2:
            continue
        buenos, fuera = AT._split_anomalas(puntos)
        if len(buenos) < 2:
            continue
        out.append({"apex": round(c["apex"], 1), "n": len(buenos), "fuera": len(fuera),
                    "media_m": round(stt.mean(buenos), 1),
                    "sigma_m": round(stt.pstdev(buenos), 1),
                    "rango_m": round(max(buenos) - min(buenos), 1),
                    "vmin_kmh": round(c["vmin"], 1) if c.get("vmin") is not None else None})
    return {"curvas": out, "descartadas": descartadas, "n_vueltas": len(traces)}


def _cli_frenadas(folder, corrida):
    """Lo que IMPRIME `--frenadas` (la tabla), parseado: [(T, apex, frena_en, sigma,
    rango, n)] como texto, tal cual sale en pantalla."""
    import contextlib
    import io
    import re
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        AT.report_braking(folder, corrida)
    filas = []
    for ln in buf.getvalue().splitlines():
        m = re.match(r"\s+(T\d+|--)\s+(-?\d+)m\s+(-?\d+)m\s+(-?\d+)m\s+([\d.]+)m\s+(\d+)m\s+(\d+)", ln)
        if m:
            filas.append(m.groups())
    return filas


def _como_cli(curvas):
    """Las curvas del JSON formateadas con los MISMOS f-strings que el CLI."""
    return [(f"T{c['t']}" if c["t"] is not None else "--", f"{c['apex']:.0f}",
             f"{c['media_m']:.0f}", f"{c['antes_m']:.0f}", f"{c['sigma_m']:.1f}",
             f"{c['rango_m']:.0f}", f"{c['n']}")
            for c in curvas]


def test_frenadas():
    print("\ntu frenada: dispersion por corrida, con la T del visor (sintetico):")
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="ams2fren_")
    try:
        d = _sesion_frenadas(base, "Estadio__Auto__practice__20261001_190000", [_FRIO, _TANDA])
        cs, partida = AT.corridas_struct(d)
        ok("parte la sesion en las 2 corridas", partida and [c["cronometradas"] for c in cs] == [5, 8],
           [(c["n"], c["cronometradas"]) for c in cs])
        ok("la Corrida N del panel es la tanda N del visor",
           [s["n_cronometradas"] for s in A.stints(d)][:2] == [5, 8])

        f = AT.frenadas_struct(d)                       # por defecto: el frio
        ok("abre en el frio (la primera corrida con 2+ cronometradas)", f["corrida"] == 1, f["corrida"])
        ok("mide las 5 vueltas del frio", f["n_vueltas"] == 5 and not f["sin_dato"], f["n_vueltas"])
        por_t = {c["t"]: c for c in f["curvas"]}
        ok("las dos curvas frenadas llevan la T del visor (T1 y T2)", sorted(por_t) == [1, 2],
           [(c["t"], c["apex"]) for c in f["curvas"]])
        if sorted(por_t) == [1, 2]:
            t1, t2 = por_t[1], por_t[2]
            # en pista: T1 entre 140 y 165 m (rango 25), T2 entre 96 y 104 (rango 8);
            # la grilla de la traza es de ~2 m, de ahi la holgura
            ok("T1 se mueve lo que se movio en pista (25 m)", abs(t1["rango_m"] - 25) <= 4, t1["rango_m"])
            ok("T2 se mueve 8 m", abs(t2["rango_m"] - 8) <= 4, t2["rango_m"])
            ok("'frenas X m antes del apex' = la media de las 5 (152 m en T1)",
               abs(t1["antes_m"] - 152) <= 4, t1["antes_m"])
            ok("n = las 5 vueltas, ninguna apartada", t1["n"] == 5 and t1["fuera"] == 0, (t1["n"], t1["fuera"]))
            ok("la sigma va en el dato (es del instructor: el panel no la muestra)",
               abs(t1["sigma_m"] - 9.3) <= 1.5, t1["sigma_m"])
        f2 = AT.frenadas_struct(d, 2)
        ok("la corrida 2 no se mezcla con el frio: ahi no se mueve nada",
           f2["curvas"] and all(c["rango_m"] <= 3 for c in f2["curvas"]),
           [c["rango_m"] for c in f2["curvas"]])
        ses = AT.frenadas_struct(d, None)
        ok("sesion entera = braking_struct de siempre (mismos numeros, T aparte)",
           _sin_t(ses["curvas"]) == AT.braking_struct(d)["curvas"])
        ok("y es la vara vieja tal cual (copia del calculo previo al refactor)",
           _sin_t(ses["curvas"]) == _braking_original(d)["curvas"])
        ok("en la sesion entera el frio cae por el 3% contra la mejor del dia "
           "(por eso se mide por corrida)", ses["n_vueltas"] == 8, ses["n_vueltas"])
        try:
            AT.frenadas_struct(d, 9)
            ok("una corrida que no existe -> ValueError", False)
        except ValueError:
            ok("una corrida que no existe -> ValueError", True)

        # el caso medido en pista (P3 en Goiania): 85.9 / 81.1 / 85.0. Con el corte del
        # 3% quedaba UNA vuelta y el frio salia sin dato; una vuelta lenta por un error
        # en otra curva tiene un punto de frenada valido en esta
        dp = _sesion_frenadas(base, "Estadio__Auto__practice__20261005_190000",
                              [[(150, 100, 0.944), (160, 100, 1.0), (140, 100, 0.954)]])
        f = AT.frenadas_struct(dp)
        t1 = next((c for c in f["curvas"] if c["t"] == 1), None)
        ok("frio de 85.9 / 81.1 / 85.0: entran las 3, sin el corte del 3%",
           f["corrida"] == 1 and f["n_vueltas"] == 3 and t1 is not None and t1["n"] == 3,
           (f["n_vueltas"], f["sin_dato"]))
        ok("y el numero es el de las 3 (frenadas a 140 / 150 / 160 -> se mueve 20 m)",
           t1 is not None and abs(t1["rango_m"] - 20) <= 4, t1 and t1["rango_m"])
        ok("la sesion entera (sin --corrida) SI conserva el corte del 3%",
           AT.frenadas_struct(dp, None)["sin_dato"] is not None)

        print("\ntu frenada: sin dato, con el porque:")
        d1 = _sesion_frenadas(base, "Estadio__Auto__practice__20261002_190000", [[_FRIO[0]], _FRIO[:1]])
        f = AT.frenadas_struct(d1)
        ok("ninguna corrida con 2 cronometradas: sin curvas y con mensaje",
           f["curvas"] == [] and f["corrida"] is None and "2 vueltas" in (f["sin_dato"] or ""),
           f["sin_dato"])
        f = AT.frenadas_struct(d, None)
        ok("con dato, sin_dato es None", f["sin_dato"] is None)
        f = AT.frenadas_struct(d1, 1)
        ok("corrida con 1 vuelta: dice cuantas tiene y cuantas hacen falta",
           f["curvas"] == [] and "1 vuelta" in f["sin_dato"] and "2 o más" in f["sin_dato"],
           f["sin_dato"])
        dt = _sesion_frenadas(base, "Estadio__Auto__practice__20261003_190000", [_FRIO], n=500)
        f = AT.frenadas_struct(dt)
        ok("trazas truncadas: lo dice (y no es un panel vacio)",
           f["curvas"] == [] and f["descartadas"] == 5 and "incompletas" in (f["sin_dato"] or ""),
           f["sin_dato"])
        dn = _sesion_frenadas(base, "Estadio__Auto__practice__20261004_190000", [_FRIO, _TANDA],
                              con_timeline=False)
        f = AT.frenadas_struct(dn)
        ok("sin linea de tiempo: la sesion es UNA corrida y se marca",
           f["partida"] is False and len(f["corridas"]) == 1 and f["corridas"][0]["cronometradas"] == 13,
           (f["partida"], f["corridas"]))

        print("\nla T: etiquetado y numeraciones entre sesiones:")
        cs_ = [{"apex": 100}, {"apex": 500}, {"apex": 900}]
        AT._etiquetar_t(cs_, [(1, 103.0), (2, 480.0), (3, 1200.0)])
        ok("cada curva toma la T de apex mas cercano, dentro de la tolerancia",
           [c["t"] for c in cs_] == [1, 2, None], [c["t"] for c in cs_])
        cs_ = [{"apex": 100}, {"apex": 125}]
        AT._etiquetar_t(cs_, [(1, 110.0)])
        ok("una T no se repite: la lleva la curva mas cercana, la otra queda sin T",
           [c["t"] for c in cs_] == [1, None], [c["t"] for c in cs_])
        cs_ = [{"apex": 100}]
        AT._etiquetar_t(cs_, [(1, 100.0 + AT.TOL_T_M + 1)])
        ok("fuera de la tolerancia no se inventa la T", cs_[0]["t"] is None)
        ok("sin apices (sesion sin posicion) nada lleva T",
           [c["t"] for c in AT._etiquetar_t([{"apex": 1}], [])] == [None])

        ta = [{"n": 1, "metro": 200, "inicio": 150, "fin": 260},
              {"n": 2, "metro": 800, "inicio": 760, "fin": 830}]
        tb = [{"n": 1, "metro": 278, "inicio": 160, "fin": 300},
              {"n": 2, "metro": 790, "inicio": 770, "fin": 820}]
        ok("misma numeracion aunque el pico de curvatura se pasee por el arco",
           A._ts_comparables(ta, tb))
        ok("una T de mas en la otra sesion: NO es la misma numeracion",
           not A._ts_comparables(ta, tb + [{"n": 3, "metro": 900, "inicio": 880, "fin": 930}]))
        tc = [{"n": 1, "metro": 500, "inicio": 470, "fin": 530}, tb[1]]
        ok("misma cantidad pero corrida: NO", not A._ts_comparables(ta, tc))
        ok("sin numeracion de un lado: NO", not A._ts_comparables(ta, []))

        hoy = {"ts": ta, "curvas": [{"t": 1, "apex": 210}, {"t": 2, "apex": 805}]}
        otra = {"ts": tb, "curvas": [{"t": 1, "apex": 270}, {"t": 2, "apex": 800}]}
        p = A.emparejar_frenadas(hoy, otra)
        ok("numeraciones iguales: empareja por T (aunque el apex se corra 60 m)",
           p["por"] == "T" and p["aviso"] is None and len(p["filas"]) == 2
           and all(f["hoy"]["t"] == f["otra"]["t"] for f in p["filas"]), p)
        otra2 = {"ts": tb + [{"n": 3, "metro": 900, "inicio": 880, "fin": 930}],
                 "curvas": [{"t": 2, "apex": 220}, {"t": 3, "apex": 790}, {"t": 4, "apex": 1500}]}
        p = A.emparejar_frenadas(hoy, otra2)
        pares = [((f["hoy"] or {}).get("apex"), (f["otra"] or {}).get("apex")) for f in p["filas"]]
        ok("numeraciones distintas: empareja por metro del apex y avisa",
           p["por"] == "metro" and p["aviso"] and pares == [(210, 220), (805, 790), (None, 1500)],
           (p["por"], pares))
        lejos = {"ts": [], "curvas": [{"t": None, "apex": 210 + A.TOL_APEX_SESIONES_M + 5}]}
        p = A.emparejar_frenadas({"ts": [], "curvas": [{"t": None, "apex": 210}]}, lejos)
        ok("dos apex mas lejos que la tolerancia no son la misma curva",
           len(p["filas"]) == 2 and all(not (f["hoy"] and f["otra"]) for f in p["filas"]),
           [(bool(f["hoy"]), bool(f["otra"])) for f in p["filas"]])

        # dos sesiones de la misma pista sintetica: misma geometria, misma numeracion
        raices = A.RAICES
        A.RAICES = (base,) + tuple(raices)
        try:
            d2 = _sesion_frenadas(base, "Estadio__Auto__practice__20261008_190000", [_TANDA[:4]])
            j = A.frenadas(d, None, os.path.basename(d2))
            ok("hoy contra la clase pasada: la otra sesion va al lado",
               j["otra"]["corrida"] == 1 and j["otra"]["n_vueltas"] == 4, j["otra"]["corrida"])
            ok("misma pista y geometria: se empareja por T",
               j["pares"]["por"] == "T" and all(f["hoy"] and f["otra"] for f in j["pares"]["filas"]),
               j["pares"]["por"])
        finally:
            A.RAICES = raices
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_frenadas_corpus():
    """Contra sesiones reales: el refactor no movio los numeros de la sesion
    entera, y la Corrida N del panel es la tanda N del visor."""
    print("\ntu frenada contra el corpus:")
    if not _hay_corpus():
        print("  (sin corpus grabado; nada que medir)")
        return
    ses = sorted(AT._sessions(), key=os.path.getmtime, reverse=True)
    iguales, n = 0, 0
    for d in ses:
        if AT.clean_laps(d)["n_clean"] < 2:
            continue
        n += 1
        vieja, nueva = _braking_original(d), AT.frenadas_struct(d, None)
        if vieja and _sin_t(nueva["curvas"]) == vieja["curvas"] \
                and nueva["n_vueltas"] == vieja["n_vueltas"] and nueva["descartadas"] == vieja["descartadas"]:
            iguales += 1
        if n >= 15:
            break
    ok(f"--frenadas de sesion entera: mismos numeros que antes del refactor ({iguales}/{n})",
       n > 0 and iguales == n)
    # la Corrida N del panel tiene que ser la tanda TN de arriba del visor
    malas, partidas = [], 0
    for d in AT._sessions():
        cs, partida = AT.corridas_struct(d)
        if not partida:
            continue
        partidas += 1
        st = A.stints(d)
        if [c["cronometradas"] for c in cs] != [s["n_cronometradas"] for s in st][:len(cs)]:
            malas.append(os.path.basename(d))
    ok(f"en {partidas} sesiones partidas, cada corrida tiene las vueltas de su tanda del visor",
       partidas > 0 and not malas, malas[:3])


def test_curvas_fijadas():
    """La T tiene que ser la MISMA curva para el instructor y diez alumnos, cada uno
    en su PC, todas las semanas: las curvas se fijan por pista en pistas/ y mandan
    sobre la numeracion de cada sesion."""
    print("\ncurvas fijadas por pista (la T de todos):")
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="ams2pistas_")
    pistas_orig, raices = A.PISTAS, A.RAICES
    A.PISTAS = os.path.join(base, "pistas")
    A.RAICES = (base,) + tuple(raices)
    try:
        d = _sesion_frenadas(base, "Estadio__Auto__practice__20261001_190000", [_FRIO, _TANDA])
        d2 = _sesion_frenadas(base, "Estadio__Auto__practice__20261008_190000", [_TANDA[:4]])
        ok("sin archivo: la numeracion es la de la sesion", A.numeracion(d)["fuente"] == "sesion")
        ruta, doc = A.fijar_curvas(d)
        ok("fijar crea pistas/curvas__<pista>__<variante>__<largo>m.json",
           os.path.basename(ruta) == f"curvas__Estadio__Estadio_GP__{round(_LARGO_E)}m.json"
           and os.path.exists(ruta), os.path.basename(ruta))
        ok("con la vuelta valida mas rapida y el algoritmo del visor (las 2 curvas del estadio)",
           [c["n"] for c in doc["curvas"]] == [1, 2] and doc["fuente"]["sesion"] == os.path.basename(d),
           [c["metro"] for c in doc["curvas"]])
        try:
            A.fijar_curvas(d2)
            ok("no pisa curvas ya fijadas sin --forzar (cambiaria la T de todos)", False)
        except ValueError as e:
            ok("no pisa curvas ya fijadas sin --forzar (cambiaria la T de todos)", "forzar" in str(e))
        ok("con --forzar si (y sobre el MISMO archivo, sin duplicar)",
           A.fijar_curvas(d2, forzar=True)[0] == ruta and len(os.listdir(A.PISTAS)) == 1)

        # El instructor lo corrige a mano (numeracion del catalogo del circuito, por
        # ejemplo): lo que quede escrito es lo que ven todos.
        with open(ruta, encoding="utf-8") as f:
            j = json.load(f)
        j["curvas"][0]["n"], j["curvas"][1]["n"] = 7, 9
        with open(ruta, "w", encoding="utf-8") as f:
            json.dump(j, f)
        m = A.mapa(d)
        ok("el mapa del visor usa las curvas fijadas", m["numeracion"] == "pista"
           and [c["n"] for c in m["curvas"]] == [7, 9] and m["curvas_archivo"] == os.path.basename(ruta),
           (m["numeracion"], [c["n"] for c in m["curvas"]]))
        f1, f2 = AT.frenadas_struct(d), AT.frenadas_struct(d2)
        ok("el panel y --frenadas nombran con la T fijada, en las DOS sesiones",
           sorted(c["t"] for c in f1["curvas"]) == [7, 9] and sorted(c["t"] for c in f2["curvas"]) == [7, 9],
           ([c["t"] for c in f1["curvas"]], [c["t"] for c in f2["curvas"]]))
        ok("y declaran de donde sale la T", f1["numeracion"] == {"fuente": "pista",
                                                                  "archivo": os.path.basename(ruta),
                                                                  "aviso": None}, f1["numeracion"])
        tr = A.traza(d, A.mapa(d)["traza"])
        ok("la tabla por curva del visor (eventos) tambien", [e["n"] for e in tr["eventos"]] == [7, 9])
        p = A.emparejar_frenadas(f1, f2)
        ok("dos sesiones con las curvas fijadas se emparejan por T, sin aviso",
           p["por"] == "T" and p["aviso"] is None
           and all(x["hoy"]["t"] == x["otra"]["t"] for x in p["filas"] if x["hoy"] and x["otra"]))

        # otro LARGO (otra version del trazado) u otra variante: el archivo no aplica
        meta_p = os.path.join(d2, "session.json")
        with open(meta_p, encoding="utf-8") as f:
            meta = json.load(f)
        meta["track_length_m"] += 20
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        ok("pista de otro largo (20 m): no usa las fijadas, cae a la de la sesion",
           A.numeracion(d2)["fuente"] == "sesion" and A.mapa(d2)["numeracion"] == "sesion")
        meta["track_length_m"] -= 20 - 3
        meta["track_variation"] = "Estadio_Corto"
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        ok("otra variante con el mismo nombre de pista: tampoco", A.numeracion(d2)["fuente"] == "sesion")
        meta["track_variation"] = "Estadio_GP"
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        ok("3 m de diferencia de largo (redondeo del juego): si aplica",
           A.numeracion(d2)["fuente"] == "pista")
        sin = _sesion_frenadas(base, "Estadio__Auto__practice__20261009_190000", [_FRIO[:2]], n=500)
        A.PISTAS = os.path.join(base, "pistas_vacio")
        try:
            A.fijar_curvas(sin)
            ok("sin una vuelta completa no se fija nada", False)
        except ValueError as e:
            ok("sin una vuelta completa no se fija nada (y dice por que)", "completa" in str(e), str(e))
    finally:
        A.PISTAS, A.RAICES = pistas_orig, raices
        shutil.rmtree(base, ignore_errors=True)


def test_medicion_y_corridas():
    """Clase 8: la vuelta B de "tu clase 2 contra la de hoy" es la de la MEDICION
    (la mas rapida de las primeras 8 de la tanda puntuada), la sesion de la clase 2
    tiene que aparecer aunque haya 60 mas nuevas, y la compuerta de la clase 7
    cuenta contactos e invalidadas SOLO del convoy (una corrida)."""
    print("\nmedicion, todas las sesiones de la pista y --contactos --corrida:")
    import contextlib
    import io
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="ams2med_")
    raices = A.RAICES
    A.RAICES = (base,) + tuple(raices)
    try:
        # corrida 2 de 10: la 3a es la mas rapida de las PRIMERAS 8; la 9a es la mas
        # rapida de todas, pero queda fuera de la tanda puntuada
        ritmos = [1.0, 1.0, 1.02, 1.0, 1.0, 1.0, 1.0, 1.0, 1.05, 1.0]
        d = _sesion_frenadas(base, "Estadio__Auto__practice__20261001_190000",
                             [_FRIO, [(160, 100, r) for r in ritmos]])
        m = A.medicion(d)
        regs = AT.corridas_struct(d)[0][1]["vueltas"]
        ok("la medicion es la mas rapida de las primeras 8 de la tanda puntuada",
           m.get("corrida") == 2 and m.get("traza") == regs[2]["trace"], (m.get("corrida"), m.get("traza")))
        ok("no la mas rapida de la sesion (la 9a, fuera de las 8)", m.get("traza") != regs[8]["trace"])
        ok("y es la misma vuelta que usa la rubrica (mejor_de_la_tanda)",
           m.get("traza") == AT.mejor_de_la_tanda(d)["trace"])
        d0 = _sesion_frenadas(base, "Estadio__Auto__practice__20261002_190000", [_FRIO])
        m0 = A.medicion(d0)
        ok("sin corrida de 8: no inventa una medicion, dice que falta y por que",
           "traza" not in m0 and "8 vueltas" in (m0.get("falta") or ""), m0)

        # todas las sesiones de la pista, sin el tope del selector de arriba
        for k in range(3):                 # tres sesiones MAS NUEVAS, de otra pista
            do = _sesion_frenadas(base, f"Otra__Auto__practice__2026100{k + 3}_190000", [_FRIO[:2]])
            mp = os.path.join(do, "session.json")
            with open(mp, encoding="utf-8") as f:
                mo = json.load(f)
            mo["track"] = "Otra"
            with open(mp, "w", encoding="utf-8") as f:
                json.dump(mo, f)
        todas = A.sesiones(None, "Estadio", "Estadio_GP")
        ok("sesiones(pista=...) trae TODAS las de esa pista y solo esas",
           sorted(s["carpeta"] for s in todas) == sorted([os.path.basename(d), os.path.basename(d0)]),
           [s["carpeta"] for s in todas])
        ok("aunque el selector de arriba (con tope, aca 1) ya no las alcance",
           not any(s["pista"] == "Estadio" for s in A.sesiones(1)) and len(todas) == 2,
           [s["carpeta"] for s in A.sesiones(1)])

        # la compuerta de la clase 7: el convoy es la corrida 2
        dc = _sesion_frenadas(base, "Estadio__Auto__race__20261009_190000", [
            [(150, 100, 1.0), (150, 100, 1.0, {"contacto": True}), (150, 100, 1.0, {"invalida": True})],
            [(150, 100, 1.0, {"contacto": True}), (150, 100, 1.0), (150, 100, 1.0, {"contacto": True}),
             (150, 100, 1.0, {"invalida": True}), (150, 100, 1.0)]])
        todo, _ = AT.contactos_struct(dc)
        c2, _ = AT.contactos_struct(dc, 2)
        c1, _ = AT.contactos_struct(dc, 1)
        ok("sesion entera: 3 contactos y 2 invalidadas (lo de siempre)",
           todo["n_contactos"] == 3 and todo["invalidadas"] == 2, (todo["n_contactos"], todo["invalidadas"]))
        ok("--corrida 2: solo los 2 contactos y la 1 invalidada del convoy",
           c2["n_contactos"] == 2 and c2["invalidadas"] == 1 and c2["n_vueltas_con_traza"] == 5,
           (c2["n_contactos"], c2["invalidadas"], c2["n_vueltas_con_traza"]))
        ok("la tasa de invalidacion es la de la corrida (1 de 6 vueltas de pista, salida incluida)",
           c2["n_vueltas_pista"] == 6 and abs(c2["tasa_invalidacion_pct"] - 16.7) < 0.1,
           (c2["n_vueltas_pista"], c2["tasa_invalidacion_pct"]))
        ok("--corrida 1 cuenta lo suyo", c1["n_contactos"] == 1 and c1["invalidadas"] == 1)
        try:
            AT.contactos_struct(dc, 7)
            ok("una corrida que no existe -> ValueError (no la sesion entera callada)", False)
        except ValueError:
            ok("una corrida que no existe -> ValueError (no la sesion entera callada)", True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            AT.report_contactos(dc, 2)
        ok("el CLI dice que corrida mide", "corrida 2" in buf.getvalue()
           and "2 contacto(s)" in buf.getvalue(), buf.getvalue()[:120])
    finally:
        A.RAICES = raices
        shutil.rmtree(base, ignore_errors=True)


def _reescribir_timeline(d, filtro=None, cola=""):
    """Reescribe timeline.jsonl dejando solo las lineas que pasan `filtro` y con
    `cola` pegada al final (una linea a medio escribir)."""
    p = os.path.join(d, "timeline.jsonl")
    with open(p, encoding="utf-8") as f:
        recs = [json.loads(x) for x in f if x.strip()]
    if filtro:
        recs = [r for r in recs if filtro(r)]
    with open(p, "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in recs) + cola)


def test_timeline_cortada():
    """Una vuelta que el resumen tiene y la linea de tiempo no (su linea se corto en
    un taskkill): antes la sesion entera pasaba a ser UNA corrida y el panel mezclaba
    el frio con la tanda. Ahora esa vuelta queda fuera y el resto se reparte igual."""
    print("\nlinea de tiempo cortada: se conservan las corridas, la huerfana queda fuera:")
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="ams2tlcort_")
    try:
        d = _sesion_frenadas(base, "Estadio__Auto__practice__20261001_190000", [_FRIO, _TANDA])
        # la ultima vuelta de la tanda (uid 13) pierde su linea, y la siguiente quedo
        # escrita a medias
        _reescribir_timeline(d, lambda r: r.get("uid") != 13, cola='{"type": "lap", "lap": 2')
        cs, partida, motivo, huerf = AT.corridas_struct(d, detalle=True)
        ok("sigue partida en sus 2 corridas", partida and [c["cronometradas"] for c in cs] == [5, 7],
           (partida, [c["cronometradas"] for c in cs], motivo))
        ok("y cuenta la vuelta que quedo sin ubicar", huerf == 1, huerf)
        f = AT.frenadas_struct(d)
        ok("el frio sigue siendo SOLO el frio (5 vueltas, no 12)",
           f["corrida"] == 1 and f["n_vueltas"] == 5 and f["huerfanas"] == 1, (f["corrida"], f["n_vueltas"]))
        # una del FRIO sin su linea: el frio queda con 4, nunca con la tanda mezclada
        d2 = _sesion_frenadas(base, "Estadio__Auto__practice__20261002_190000", [_FRIO, _TANDA])
        _reescribir_timeline(d2, lambda r: r.get("uid") != 3)
        f2 = AT.frenadas_struct(d2)
        ok("una huerfana del frio: el frio se mide con 4, sin mezclar la tanda",
           f2["corrida"] == 1 and f2["n_vueltas"] == 4, f2["n_vueltas"])
        d3 = _sesion_frenadas(base, "Estadio__Auto__practice__20261003_190000",
                              [_FRIO, [(160, 100, 1.0)] * 9])
        _reescribir_timeline(d3, lambda r: r.get("uid") != 14)
        v, origen, k = AT.vueltas_de_la_tanda(d3)
        ok("la rubrica sigue en la tanda (corrida 2), no en las primeras 8 de la sesion",
           k == 2 and len(v) == 8 and "sin ubicar" in origen, (k, origen))
        # linea de tiempo SIN uid (formato viejo) que no cuadra: no se sabe donde va
        # cada vuelta. Eso NO es una sesion sin linea de tiempo.
        d4 = _sesion_frenadas(base, "Estadio__Auto__practice__20261004_190000", [_FRIO, _TANDA])
        p = os.path.join(d4, "timeline.jsonl")
        with open(p, encoding="utf-8") as fh:
            recs = [json.loads(x) for x in fh if x.strip()]
        recs = [dict(r, uid=None) for r in recs]
        recs = [r for i, r in enumerate(recs) if i != 3]          # una flying de menos
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in recs))
        f4 = AT.frenadas_struct(d4)
        ok("timeline sin uid que no cuadra: el panel NO mide la sesion entera como 'la corrida'",
           f4["curvas"] == [] and f4["corridas"] == [] and "no cuadra" in (f4["sin_dato"] or ""),
           f4["sin_dato"])
        v4, origen4, k4 = AT.vueltas_de_la_tanda(d4)
        ok("y la rubrica dice POR QUE cae a las primeras 8 (no 'sin linea de tiempo')",
           k4 is None and "no cuadra" in origen4 and "sin linea de tiempo" not in origen4, origen4)
        ok("la medicion tambien lo dice", "no cuadra" in A.medicion(d4).get("falta", ""))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _gzip_cortado(ruta, filas):
    import gzip
    import io
    import ams2_telemetry as T
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as g:
        g.write((",".join(T.HEADER) + "\n" + "\n".join(filas)).encode("utf-8"))
    with open(ruta, "wb") as f:
        f.write(buf.getvalue()[: len(buf.getvalue()) // 2])


def test_curvas_fijadas_robustas():
    """Archivos de curvas fijadas editados a mano: duplicados (manda el de largo mas
    parecido, con aviso) e invalidos (se rechazan con aviso y se cae a la sesion)."""
    print("\ncurvas fijadas: duplicados y archivos que no validan:")
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="ams2pistas2_")
    pistas_orig = A.PISTAS
    A.PISTAS = os.path.join(base, "pistas")
    try:
        d = _sesion_frenadas(base, "Estadio__Auto__practice__20261001_190000", [_FRIO[:2]])
        ruta, doc = A.fijar_curvas(d)
        largo = doc["largo_m"]

        def escribir(nombre, cambio):
            j = json.loads(json.dumps(doc))
            cambio(j)
            with open(os.path.join(A.PISTAS, nombre), "w", encoding="utf-8") as f:
                json.dump(j, f)
        # otro archivo de la misma pista, alfabeticamente ANTES y con largo 4 m distinto
        escribir("curvas__Estadio__Estadio_GP__0aaa.json",
                 lambda j: (j.update(largo_m=largo + 4), j["curvas"][0].update(n=1),
                            j["curvas"][1].update(n=5)))
        n = A.numeracion(d)
        ok("con dos archivos manda el de largo mas parecido, no el primero alfabetico",
           n["archivo"] == os.path.basename(ruta), n)
        ok("y avisa que hay dos", n["aviso"] and "2 archivos" in n["aviso"], n["aviso"])
        escribir("curvas__Estadio__Estadio_GP__0aaa.json", lambda j: None)   # mismo largo
        n = A.numeracion(d)
        ok("empate exacto de largo: lo dice", n["aviso"] and "empatan" in n["aviso"], n["aviso"])
        os.remove(os.path.join(A.PISTAS, "curvas__Estadio__Estadio_GP__0aaa.json"))

        malos = {
            "n repetida": lambda j: j["curvas"][1].update(n=j["curvas"][0]["n"]),
            "n no entero": lambda j: j["curvas"][0].update(n="T1"),
            "metro fuera del tramo": lambda j: j["curvas"][0].update(metro=j["curvas"][0]["fin"] + 10),
            "fuera de orden": lambda j: j["curvas"].reverse(),
            "numero no finito": lambda j: j["curvas"][0].update(inicio=float("nan")),
            "mas alla del largo": lambda j: j["curvas"][1].update(fin=largo + 50),
            "sin curvas": lambda j: j.update(curvas=[]),
        }
        for nombre, cambio in malos.items():
            escribir(os.path.basename(ruta), cambio)
            n = A.numeracion(d)
            m = A.mapa(d)
            ok(f"archivo invalido ({nombre}): se rechaza, cae a la sesion y avisa",
               n["fuente"] == "sesion" and n["aviso"] and m["numeracion"] == "sesion"
               and m["curvas_aviso"], (n, m.get("curvas_aviso")))
        with open(ruta, "w", encoding="utf-8") as f:
            f.write("{ esto no es json")
        ok("JSON roto: tambien", A.numeracion(d)["fuente"] == "sesion" and A.numeracion(d)["aviso"])
    finally:
        A.PISTAS = pistas_orig
        shutil.rmtree(base, ignore_errors=True)


def test_trazas_a_medias():
    """Trazas que quedaron a medio escribir (sesiones de antes del volcado atomico, o
    un taskkill): los lectores no revientan, y la API contesta 'reintenta' (503)."""
    print("\ntrazas a medio escribir: lectores tolerantes y 503 reintentable:")
    import http.server
    import shutil
    import tempfile
    import bridge_shm
    base = tempfile.mkdtemp(prefix="ams2medias_")
    raices = A.RAICES
    A.RAICES = (base,) + tuple(raices)
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", PUERTO + 2), lambda *a, **kw: bridge_shm._NoCacheHandler(*a, directory=HERE, **kw))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(ruta):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PUERTO + 2}{ruta}", timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        d = _sesion_frenadas(base, "Estadio__Auto__race__20261001_190000", [
            [(150, 100, 1.0), (150, 100, 1.0, {"contacto": True}),
             (150, 100, 1.0, {"invalida": True})]])
        x = next(f for f in os.listdir(d) if f.startswith("X"))
        filas, _ = _vuelta_estadio((150, 100), 1.0)
        _gzip_cortado(os.path.join(d, x), filas)
        try:
            cs, _ = AT.contactos_struct(d)
            ok("--contactos con una traza cortada no revienta: la salta y lo dice",
               cs["n_contactos"] == 1 and any("ilegible" in a for a in cs["avisos"]), cs["avisos"])
        except Exception as e:                       # noqa: BLE001
            ok("--contactos con una traza cortada no revienta", False, repr(e))
        s = os.path.basename(d)
        c, j = get(f"/api/traza?s={s}&t={x}")
        ok("/api/traza sobre un gzip a medias: 503 y 'reintentar', no 500",
           c == 503 and j.get("reintentar") is True, (c, j))
        c, j = get(f"/api/mapa?s={s}&t={x}")
        ok("/api/mapa con t= sobre un gzip a medias: tambien 503", c == 503 and j.get("reintentar"), (c, j))
        c, j = get(f"/api/mapa?s={s}")
        ok("/api/mapa sin t= usa otra vuelta y responde", c == 200 and j["traza"] != x, c)
        # el CSV de la corrida salta la vuelta cortada entera, no la deja a medias
        filas_csv = list(A.stint_csv(d, 1))
        vueltas_csv = {ln.split(",", 1)[0] for ln in filas_csv[1:]}
        ok("el CSV salta la vuelta cortada entera", len(vueltas_csv) == 2, sorted(vueltas_csv))
        # la bandeja no empaqueta .tmp (escrituras que el grabador iba a renombrar)
        import entrega as E
        with open(os.path.join(d, "L099_70.000s.csv.gz.tmp"), "wb") as f:
            f.write(b"a medias")
        ok("la bandeja no mete .tmp en el zip ni en la huella",
           not any(n.endswith(".tmp") for n, _ in E._archivos(d)))
        with open(os.path.join(HERE, "analisis.html"), encoding="utf-8") as f:
            src = f.read()
        ok("el visor no se cae si una traza no se puede leer (try/catch, suelta la letra)",
           'try { S.trazas.set(n, await api("traza"' in src and "S[cual] = null;" in src)
    finally:
        srv.shutdown()
        A.RAICES = raices
        shutil.rmtree(base, ignore_errors=True)


def test_indice_de_sesiones():
    """`/api/sesiones?pista=` recorre todas las carpetas: el session.json de cada una
    se lee una vez y se reusa mientras no cambie."""
    print("\nindice de sesiones memorizado:")
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="ams2idx_")
    raices, meta_orig = A.RAICES, A._meta
    A.RAICES = (base,)
    leidas = []
    A._meta = lambda folder: (leidas.append(folder), meta_orig(folder))[1]
    try:
        d = _sesion_frenadas(base, "Estadio__Auto__practice__20261001_190000", [_FRIO[:2]])
        _sesion_frenadas(base, "Estadio__Auto__practice__20261002_190000", [_FRIO[:2]])
        A.sesiones(None, "Estadio", "Estadio_GP")
        n1 = len(leidas)
        A.sesiones(None, "Estadio", "Estadio_GP")
        ok("la segunda pasada no relee ningun session.json", len(leidas) == n1 and n1 == 2, (n1, len(leidas)))
        p = os.path.join(d, "session.json")
        with open(p, encoding="utf-8") as f:
            m = json.load(f)
        m["track"] = "Otra"
        import time as _t
        _t.sleep(0.05)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(m, f)
        res = A.sesiones(None, "Estadio", "Estadio_GP")
        ok("si el session.json cambia, se relee", len(res) == 1 and len(leidas) == n1 + 1,
           ([s["carpeta"] for s in res], len(leidas)))
    finally:
        A.RAICES, A._meta = raices, meta_orig
        A._CACHE_META.clear()
        shutil.rmtree(base, ignore_errors=True)


def test_frenadas_en_vivo():
    """En clase el alumno abre el panel en el debrief SIN haber salido de la sesion:
    la carpeta sigue abierta en el grabador. Se simula lo peor que puede encontrar
    el visor en ese momento: la linea de tiempo ya anuncio una vuelta cuyo resumen
    todavia no se escribio, su traza a medio volcar (gzip cortado), y la ultima
    linea de timeline.jsonl partida por la mitad."""
    print("\ntu frenada sobre la sesion que se sigue grabando:")
    import gzip
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="ams2frenvivo_")
    try:
        d = _sesion_frenadas(base, "Estadio__Auto__practice__20261001_190000", [_FRIO, _TANDA[:3]])
        # la vuelta que se esta cerrando AHORA: timeline primero (el grabador la
        # anota antes de volcar la traza y el resumen), traza a medio escribir
        filas, lt = _vuelta_estadio((160, 100), 1.02)
        nom = f"L099_{lt:.3f}s.csv.gz"
        import io
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as g:
            g.write(("\n".join(filas)).encode("utf-8"))
        with open(os.path.join(d, nom), "wb") as f:
            f.write(buf.getvalue()[: len(buf.getvalue()) // 2])
        with open(os.path.join(d, "timeline.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "lap", "lap": 99, "lap_time": round(lt, 3),
                                "kind": "flying", "uid": 9, "trace": nom}) + "\n")
            f.write('{"type": "lap", "lap": 100, "kind": "fl')      # escritura a medias
        try:
            cs, partida = AT.corridas_struct(d)
            ok("las corridas se leen igual (la vuelta sin resumen todavia no cuenta)",
               partida and [c["cronometradas"] for c in cs] == [5, 3],
               (partida, [c["cronometradas"] for c in cs]))
            f = AT.frenadas_struct(d)
            ok("el frio sale entero mientras la sesion sigue creciendo",
               f["corrida"] == 1 and f["n_vueltas"] == 5 and len(f["curvas"]) == 2, f["sin_dato"])
            ok("y con los mismos metros que con la carpeta quieta",
               f["curvas"] == AT.frenadas_struct(d, 1)["curvas"])
            # la traza a medio volcar era la MAS RAPIDA: el mapa (y con el la T)
            # tiene que salir de la siguiente, no caerse
            ok("la T sigue saliendo aunque la vuelta mas rapida este a medio escribir",
               sorted(c["t"] for c in f["curvas"]) == [1, 2], [c["t"] for c in f["curvas"]])
            m = A.mapa(d)
            ok("el mapa usa la siguiente mas rapida y avisa que es de respaldo",
               m["traza"] != nom and m["respaldo"] is True, (m["traza"], m.get("respaldo")))
        except Exception as e:                    # noqa: BLE001
            ok("la sesion en curso no revienta el panel", False, repr(e))
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_panel_sin_sigma():
    """El panel "Tu frenada" lo abre y lo comparte el ALUMNO, y el Nivel 1 no ve σ
    (regla del curso). La sigma viaja en el JSON para el instructor, pero el codigo
    del visor no puede leerla ni escribirla, ni pintar veredictos del CLI."""
    import re
    print("\npanel del visor: sin sigma ni veredictos a la vista del alumno:")
    with open(os.path.join(HERE, "analisis.html"), encoding="utf-8") as f:
        src = f.read()
    # fuera los comentarios (ahi SI se explica por que no va)
    codigo = re.sub(r"/\*.*?\*/|<!--.*?-->", "", src, flags=re.DOTALL)
    codigo = re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", codigo)
    for prohibido in ("sigma", "σ", "SIN REFERENCIA", "inconsistente"):
        ok(f"el visor no usa {prohibido!r}", prohibido not in codigo)


def test_modo_compartir():
    """En clase el alumno comparte el visor por Discord y la regla del curso es que
    en los canales no se muestran tiempos. Todo tiempo de vuelta y todo delta en
    segundos que pinte el visor tiene que pasar por el modo compartir: uno pintado
    por fuera se filtra a la transmision sin que nadie lo note. Se revisa el codigo:
    cada t2s() / toFixed(3) fuera de las funciones del modo tiene que tener la
    guarda S.ocultar en su linea o en las 6 anteriores."""
    import re
    print("\nmodo compartir: ningun tiempo se pinta sin pasar por 'ocultar tiempos':")
    with open(os.path.join(HERE, "analisis.html"), encoding="utf-8") as f:
        lineas = f.read().splitlines()
    sueltas = []
    for i, ln in enumerate(lineas):
        if not re.search(r"\bt2s\(|toFixed\(3\)", ln):
            continue
        if re.search(r"const (t2s|tVuelta|tTexto) =", ln) or ln.lstrip().startswith("(t >= 60"):
            continue                                  # las definiciones del propio modo
        if not any("S.ocultar" in x for x in lineas[max(0, i - 6): i + 1]):
            sueltas.append(f"{i + 1}: {ln.strip()[:70]}")
    ok("todos los tiempos y deltas en segundos pasan por la guarda", not sueltas, sueltas[:3])
    src = "\n".join(lineas)
    ok("hay un interruptor visible ('ocultar tiempos')", 'id="ocultar"' in src and "ocultar tiempos" in src)
    ok("se recuerda en localStorage, con try/catch",
       re.search(r"try \{ S\.ocultar = localStorage\.getItem", src) is not None)
    ok("y se puede pedir por URL (&ocultar=1), que las fichas del curso pueden citar",
       'get("ocultar") === "1"' in src)
    ok("con el modo puesto el CSV (que trae el reloj de cada muestra) no se ofrece",
       "csv.disabled = S.ocultar" in src and "if (S.ocultar) return;" in src)


def test_redondeo_panel_igual_cli():
    """El panel pinta los metros con JS y el CLI con f"{x:.0f}" de Python. Math.round
    redondea 12.5 a 13 y Python a 12: con el mismo dato, el alumno diria un numero y
    el instructor leeria otro. Se corre la funcion del panel TAL CUAL esta en
    analisis.html (con node) contra el formato del CLI, sobre todos los valores con
    un decimal hasta 400 m, que es como vienen en el JSON."""
    import re
    import shutil
    import subprocess
    print("\nel panel redondea los metros igual que --frenadas:")
    node = shutil.which("node")
    if not node:
        print("  (sin node en este PC: se salta)")
        return
    with open(os.path.join(HERE, "analisis.html"), encoding="utf-8") as f:
        m = re.search(r"const mFr = v => \{.*?\n\};", f.read(), flags=re.DOTALL)
    if not ok("la funcion de redondeo del panel esta donde se la busca", m is not None):
        return
    valores = [round(k / 10, 1) for k in range(4001)]
    js = m.group(0) + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[1]).map(mFr)));"
    r = subprocess.run([node, "-e", js, json.dumps(valores)], capture_output=True, text=True,
                       check=False)
    try:
        panel = json.loads(r.stdout)
    except ValueError:
        ok("node corre la funcion del panel", False, r.stderr[:200])
        return
    cli = [f"{v:.0f}" for v in valores]
    distintos = [(v, p, c) for v, p, c in zip(valores, panel, cli) if p != c]
    ok(f"{len(valores)} valores: el panel y el CLI dicen el mismo entero", not distintos, distintos[:5])
    ok("incluido el empate (12.5 -> 12, como Python)", panel[125] == "12", panel[125])


def test_api_frenadas():
    """/api/frenadas: forma del JSON, sin dato, errores, y que el numero que llega
    al panel sea EXACTAMENTE el que imprime `--frenadas --corrida`."""
    print("\nAPI /api/frenadas (y el CLI dice lo mismo):")
    try:
        import bridge_shm
    except Exception as e:                       # noqa: BLE001
        ok("bridge_shm importa", False, e)
        return
    import http.server
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="ams2frenapi_")
    raices = A.RAICES
    A.RAICES = (base,) + tuple(raices)
    h = lambda *a, **kw: bridge_shm._NoCacheHandler(*a, directory=HERE, **kw)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PUERTO + 1), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def jget(ruta):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PUERTO + 1}{ruta}", timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        d = _sesion_frenadas(base, "Estadio__Auto__practice__20261001_190000", [_FRIO, _TANDA])
        s = os.path.basename(d)
        c, j = jget(f"/api/frenadas?s={s}")
        ok("responde 200", c == 200, c)
        ok("trae la lista de corridas (n, vueltas cronometradas)",
           [(x["n"], x["cronometradas"]) for x in j.get("corridas", [])]
           == [(1, 5), (2, 8)], j.get("corridas"))
        ok("y dice de donde sale la T (aca, sin curvas fijadas: de la sesion)",
           (j.get("numeracion") or {}).get("fuente") == "sesion", j.get("numeracion"))
        ok("sin corrida pedida abre en el frio", j.get("corrida") == 1)
        claves = {"t", "apex", "antes_m", "media_m", "rango_m", "sigma_m", "n", "fuera"}
        ok("cada curva trae T, apex, donde frena, rango, sigma, n y fuera",
           j["curvas"] and all(claves <= set(x) for x in j["curvas"]),
           j["curvas"][0] if j["curvas"] else None)
        ok("con dato no hay sin_dato", j.get("sin_dato") is None)
        ok("la ruta da lo mismo que frenadas_struct (un solo calculo)",
           j["curvas"] == AT.frenadas_struct(d)["curvas"])
        for k in (1, 2):
            _, jk = jget(f"/api/frenadas?s={s}&corrida={k}")
            ok(f"corrida {k}: lo que imprime --frenadas --corrida {k} == lo que recibe el panel",
               _cli_frenadas(d, k) == _como_cli(jk["curvas"]) and jk["curvas"],
               (_cli_frenadas(d, k), _como_cli(jk["curvas"])))
        ok("--corrida frio imprime lo mismo que el panel por defecto",
           _cli_frenadas(d, "frio") == _como_cli(j["curvas"]))
        c, _ = jget(f"/api/frenadas?s={s}&corrida=9")
        ok("corrida que no existe -> 400", c == 400, c)
        c, _ = jget(f"/api/frenadas?s={s}&corrida=abc")
        ok("corrida que no es numero -> 400", c == 400, c)
        d1 = _sesion_frenadas(base, "Estadio__Auto__practice__20261002_190000", [_FRIO[:1]])
        c, j1 = jget(f"/api/frenadas?s={os.path.basename(d1)}")
        ok("sin dato: 200 con curvas vacias y el porque",
           c == 200 and j1["curvas"] == [] and j1["sin_dato"], (c, j1.get("sin_dato")))
        d2 = _sesion_frenadas(base, "Estadio__Auto__practice__20261008_190000", [_TANDA[:4]])
        c, jv = jget(f"/api/frenadas?s={s}&vs={os.path.basename(d2)}&vc=1")
        ok("con vs: trae la otra sesion y las filas emparejadas",
           c == 200 and jv["otra"]["corrida"] == 1 and jv["pares"]["filas"]
           and set(jv["pares"]) >= {"por", "aviso", "filas"}, c)
        c, _ = jget(f"/api/frenadas?s={s}&vs=..")
        ok("vs con path traversal -> 400", c == 400, c)
        c, js = jget(f"/api/sesion?s={s}")
        ok("/api/sesion trae la vuelta de la medicion (corrida 2, la de 8)",
           c == 200 and js.get("medicion", {}).get("corrida") == 2 and js["medicion"].get("traza"),
           js.get("medicion"))
        c, js = jget("/api/sesiones?pista=Estadio&variante=Estadio_GP")
        ok("/api/sesiones?pista= trae todas las de la pista, sin tope",
           c == 200 and len(js["sesiones"]) == 3 and all(x["pista"] == "Estadio" for x in js["sesiones"]),
           [x["carpeta"] for x in js.get("sesiones", [])])

        # la sesion que el grabador tiene abierta va marcada "en curso"
        class _Grabador:
            def sess_dir(self):
                return d
        antes = bridge_shm.telemetry
        bridge_shm.telemetry = _Grabador()
        try:
            c, js = jget("/api/sesiones?n=200")
            marca = {x["carpeta"]: x.get("en_curso") for x in js["sesiones"]}
            ok("/api/sesiones marca en curso la carpeta abierta del grabador, y solo esa",
               marca.get(s) is True and sum(1 for v in marca.values() if v) == 1,
               marca.get(s))
        finally:
            bridge_shm.telemetry = antes
        c, js = jget("/api/sesiones?n=5")
        ok("sin grabador (bridge sin AMS2) ninguna va en curso",
           c == 200 and not any(x.get("en_curso") for x in js["sesiones"]))

        # en sesiones REALES: cada corrida medible, CLI contra ruta
        if _hay_corpus():
            vistas = 0
            for dr in sorted(AT._sessions(), key=os.path.getmtime, reverse=True):
                cs, partida = AT.corridas_struct(dr)
                med = [x["n"] for x in cs if x["cronometradas"] >= 2]
                if not partida or len(med) < 2:
                    continue
                for k in med:
                    _, jr = jget(f"/api/frenadas?s={os.path.basename(dr)}&corrida={k}")
                    cli = _cli_frenadas(dr, k)
                    ok(f"{os.path.basename(dr)[:40]} corrida {k}: CLI == panel",
                       cli == _como_cli(jr["curvas"]), (cli[:2], _como_cli(jr["curvas"])[:2]))
                vistas += 1
                if vistas >= 2:
                    break
    finally:
        srv.shutdown()
        A.RAICES = raices
        shutil.rmtree(base, ignore_errors=True)


def main():
    print("=== tests del visor de telemetria ===")
    if _hay_corpus():
        test_stints()
        test_emparejamiento()
        test_remuestreo()
        test_delta()
        test_curvas()
        test_frenadas_corpus()
    else:
        print("(sin telemetry/ grabado -- normal en un clon fresco: se saltan los")
        print(" tests que comparan contra el corpus y corren los independientes)")
    test_eventos()
    test_bordes()
    test_tanda_de_verdad()
    test_frontera()
    test_api()
    test_frenadas()
    test_curvas_fijadas()
    test_medicion_y_corridas()
    test_timeline_cortada()
    test_curvas_fijadas_robustas()
    test_trazas_a_medias()
    test_indice_de_sesiones()
    test_frenadas_en_vivo()
    test_panel_sin_sigma()
    test_modo_compartir()
    test_redondeo_panel_igual_cli()
    test_api_frenadas()
    print(f"\n{'TODO VERDE' if not _fallos else str(_fallos) + ' FALLO(S)'}")
    return 1 if _fallos else 0


if __name__ == "__main__":
    sys.exit(main())
