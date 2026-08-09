#!/usr/bin/env python3
"""Tests de la capa de analisis del visor (ams2_analysis) y de su API HTTP.

Corren contra el CORPUS REAL en telemetry/, no contra mocks: lo que se quiere
verificar aca es precisamente que la lectura del dato grabado cuadra consigo
misma (largo de pista, cronometro, emparejamiento traza<->vuelta). Un mock
diria que si a cualquier cosa.

Correr:  .venv\\Scripts\\python.exe tools/test_analisis.py
"""
import json
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
        ok("/api/sesiones responde", c == 200 and j["sesiones"], c)
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


def main():
    print("=== tests del visor de telemetria ===")
    test_stints()
    test_emparejamiento()
    test_remuestreo()
    test_delta()
    test_curvas()
    test_frontera()
    test_api()
    print(f"\n{'TODO VERDE' if not _fallos else str(_fallos) + ' FALLO(S)'}")
    return 1 if _fallos else 0


if __name__ == "__main__":
    sys.exit(main())
