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

    izq, der, pianos, fuera, nv = A._acumular_bordes(cx, cz, paso, [vuelta(2.0, True), vuelta(-2.0)])
    ok("cuenta las 2 vueltas", nv == 2)
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
    izq2, der2, _, _, _ = A._acumular_bordes(cx, cz, paso, [vuelta(1.0, hasta=100.0)])
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


def main():
    print("=== tests del visor de telemetria ===")
    if _hay_corpus():
        test_stints()
        test_emparejamiento()
        test_remuestreo()
        test_delta()
        test_curvas()
    else:
        print("(sin telemetry/ grabado -- normal en un clon fresco: se saltan los")
        print(" tests que comparan contra el corpus y corren los independientes)")
    test_eventos()
    test_bordes()
    test_tanda_de_verdad()
    test_frontera()
    test_api()
    print(f"\n{'TODO VERDE' if not _fallos else str(_fallos) + ' FALLO(S)'}")
    return 1 if _fallos else 0


if __name__ == "__main__":
    sys.exit(main())
