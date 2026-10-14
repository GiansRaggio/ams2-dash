"""Tests de red_local.py -- lo que el dash deja ver y hacer por la red local.

El modo de falla que cuidan es el que encontro la auditoria del 13-10-2026: el
servidor HTTP del dash (0.0.0.0:8080) entregaba la carpeta entera del proyecto, con
/entrega.json (la credencial del alumno), y el WebSocket aceptaba de cualquiera los
comandos que apagan el servidor o la grabacion. Se prueba con un servidor real sobre
una carpeta falsa (con un entrega.json de mentira) y rutas mandadas EN CRUDO con
http.client, para que ningun cliente "arregle" la ruta antes de llegar al servidor.
"""
import asyncio
import http.client
import http.server
import os
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import red_local                                                 # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OK = FAIL = 0


def _ok(nombre, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {nombre} {extra}")
    else:
        FAIL += 1
        print(f"  [FAIL] {nombre} {extra}")


SECRETO = "esc_TOKEN_DE_MENTIRA_PARA_EL_TEST"


def _carpeta_falsa():
    d = tempfile.mkdtemp(prefix="dash_red_")
    for nombre, cuerpo in [("index.html", "<html>dash</html>"), ("analisis.html", "<html>visor</html>"),
                           ("nosleep.mp4", "mp4"), ("entrega.json", '{"token": "%s"}' % SECRETO),
                           ("player.txt", "Piloto"), ("bridge.log", "log"), ("bridge_shm.py", "codigo"),
                           ("favicon.ico", "ico"), ("index-v1-backup.html", "<html>v1</html>")]:
        with open(os.path.join(d, nombre), "w", encoding="utf-8") as f:
            f.write(cuerpo)
    for sub, nombre, cuerpo in [(".git", "HEAD", "ref: refs/heads/master"), (os.path.join("telemetry", "s1"), "session.json", "{}"),
                                ("tools", "x.py", "x")]:
        os.makedirs(os.path.join(d, sub), exist_ok=True)
        with open(os.path.join(d, sub, nombre), "w", encoding="utf-8") as f:
            f.write(cuerpo)
    return d


def _servidor(handler_cls, carpeta):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **kw: handler_cls(*a, directory=carpeta, **kw))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _pedir(puerto, ruta, metodo="GET"):
    """(status, cuerpo); si el servidor se cae o corta, (-1, error): cuenta como FAIL, no aborta."""
    c = http.client.HTTPConnection("127.0.0.1", puerto, timeout=10)
    try:
        c.putrequest(metodo, ruta, skip_host=False, skip_accept_encoding=True)
        c.endheaders()
        r = c.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    except (OSError, http.client.HTTPException) as e:
        return -1, repr(e)
    finally:
        c.close()


PERMITIDAS = ["/", "/index.html", "/analisis.html", "/analisis.html?s=x&ocultar=1", "/nosleep.mp4", "/index.html#arriba"]
PROHIBIDAS = ["/entrega.json", "/ENTREGA.JSON", "/Entrega.json", "/%65ntrega.json", "/./entrega.json", "//entrega.json",
              "/%2e/entrega.json", "/tools/../entrega.json", "/%2e%2e/entrega.json", "/..%2fentrega.json", "/%5centrega.json",
              "/\\entrega.json", "/entrega.json::$DATA", "/entrega.json.", "/entrega.json%00.html", "/ENTREG~1.JSO",
              "/player.txt", "/bridge.log", "/bridge_shm.py", "/.git/HEAD", "/.git/", "/telemetry/", "/telemetry/s1/session.json",
              "/tools/", "/tools/x.py", "/index.html/", "/index.html/../entrega.json", "/%ff", "/favicon.ico",
              "/index-v1-backup.html"]


def test_http(nombre_mod, handler_cls):
    print(f"\n== HTTP de {nombre_mod}: solo las paginas del dash ==")
    d = _carpeta_falsa()
    srv = _servidor(handler_cls, d)
    try:
        p = srv.server_address[1]
        for ruta in PERMITIDAS:
            c, cuerpo = _pedir(p, ruta)
            _ok(f"{ruta} -> 200", c == 200 and SECRETO not in cuerpo, f"({c})")
        c, cuerpo = _pedir(p, "/")
        _ok("/ entrega index.html (no un listado de carpeta)", "dash" in cuerpo and "entrega.json" not in cuerpo)
        c, _ = _pedir(p, "/index.html", "HEAD")
        _ok("HEAD /index.html -> 200", c == 200, f"({c})")
        for ruta in PROHIBIDAS:
            c, cuerpo = _pedir(p, ruta)
            _ok(f"{ruta} -> 404", c == 404 and SECRETO not in cuerpo, f"({c})")
        c, cuerpo = _pedir(p, "/entrega.json", "HEAD")
        _ok("HEAD /entrega.json -> 404", c == 404, f"({c})")
        c, _ = _pedir(p, "/entrega.json", "POST")
        _ok("POST /entrega.json no entrega nada", c in (404, 501), f"({c})")
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(d, ignore_errors=True)


API_TRAVESIAS = ["/api/../entrega.json", "/api/sesion?s=..", "/api/sesion?s=../entrega.json", "/api/sesion?s=..%5c..",
                 "/api/traza?s=..&t=1", "/api/export?s=..%2f..&t=1", "/api/frenadas?s=..", "/api/mapa?s=../.git",
                 "/api/bordes?s=..", "/api/sesion?s=C:%5cWindows"]


def test_api_sigue():
    """/api/* es la unica ruta que se salta la lista blanca: que siga andando y que no lea fuera de telemetry/."""
    print("\n== /api/* sigue respondiendo y no sale de telemetry/ ==")
    import ams2_analysis
    import bridge_shm
    d = _carpeta_falsa()
    telem_real = ams2_analysis.TELEM
    ams2_analysis.TELEM = os.path.join(d, "telemetry")   # la API lee la carpeta falsa, con el entrega.json de mentira al lado
    srv = _servidor(bridge_shm._NoCacheHandler, d)
    try:
        p = srv.server_address[1]
        c, cuerpo = _pedir(p, "/api/sesiones")
        _ok("/api/sesiones -> 200", c == 200, f"({c})")
        for ruta in API_TRAVESIAS:
            c, cuerpo = _pedir(p, ruta)
            _ok(f"{ruta} no entrega nada de afuera", c not in (200, -1) and SECRETO not in cuerpo and "refs/heads" not in cuerpo, f"({c})")
    finally:
        ams2_analysis.TELEM = telem_real
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(d, ignore_errors=True)


class _Ws:
    """Lo minimo de una conexion de websockets que mira red_local.comandos_permitidos."""

    def __init__(self, origin=None, host="192.168.1.5:8765", remoto=("192.168.1.9", 50000)):
        h = {}
        if origin is not None:
            h["Origin"] = origin
        if host is not None:
            h["Host"] = host
        self.request = type("R", (), {"headers": h})()
        self.remote_address = remoto


def test_compuerta():
    print("\n== Compuerta de comandos del WebSocket ==")
    import socket
    from websockets.datastructures import Headers
    P = 8080
    pc = socket.gethostname()
    dos_host = _Ws("http://127.0.0.1:8080")
    dos_host.request.headers = Headers([("Host", "127.0.0.1:8765"), ("Host", "evil.example:8765"), ("Origin", "http://127.0.0.1:8080")])
    casos = [
        ("la pagina del dash por el nombre del PC", _Ws(f"http://{pc}:8080", f"{pc}:8765"), True),
        ("la pagina del dash por el nombre del PC en mayusculas", _Ws(f"http://{pc.upper()}:8080", f"{pc.upper()}:8765"), True),
        ("la pagina del dash por <nombre del PC>.local", _Ws(f"http://{pc}.local:8080", f"{pc}.local:8765"), True),
        ("otro nombre .local (rebinding desde la red)", _Ws("http://evil.local:8080", "evil.local:8765"), False),
        ("nombre del router (.lan)", _Ws("http://pc.lan:8080", "pc.lan:8765"), False),
        ("Host repetido (cabecera duplicada) -> solo lectura, sin excepcion", dos_host, False),
        ("la pagina del dash desde el celular", _Ws("http://192.168.1.5:8080"), True),
        ("la pagina del dash en el mismo PC (localhost)", _Ws("http://localhost:8080", "localhost:8765", ("127.0.0.1", 1)), True),
        ("la pagina del dash en el mismo PC (127.0.0.1)", _Ws("http://127.0.0.1:8080", "127.0.0.1:8765", ("127.0.0.1", 1)), True),
        ("la pagina del dash por IPv6 ::1", _Ws("http://[::1]:8080", "[::1]:8765", ("::1", 1)), True),
        ("un sitio de internet", _Ws("https://evil.example", "192.168.1.5:8765", ("127.0.0.1", 1)), False),
        ("un sitio de internet en :8080", _Ws("http://evil.example:8080", "192.168.1.5:8765", ("127.0.0.1", 1)), False),
        ("DNS rebinding (dominio ajeno en Host y Origin)", _Ws("http://evil.example:8080", "evil.example:8765", ("127.0.0.1", 1)), False),
        ("otro host de la red", _Ws("http://192.168.1.77:8080", "192.168.1.5:8765"), False),
        ("mismo host, otro puerto", _Ws("http://192.168.1.5:9999"), False),
        ("mismo host, puerto 80 implicito", _Ws("http://192.168.1.5"), False),
        ("https (el dash no usa https)", _Ws("https://192.168.1.5:8080"), False),
        ("Origin null (archivo local / sandbox)", _Ws("null"), False),
        ("Origin basura", _Ws("::::"), False),
        ("sin Host", _Ws("http://192.168.1.5:8080", None), False),
        ("script sin Origin desde este PC", _Ws(None, "127.0.0.1:8765", ("127.0.0.1", 1)), True),
        ("script sin Origin desde IPv4 mapeada", _Ws(None, "127.0.0.1:8765", ("::ffff:127.0.0.1", 1)), True),
        ("script sin Origin desde otro PC de la red", _Ws(None, "192.168.1.5:8765", ("192.168.1.9", 1)), False),
        ("objeto sin request", object(), False),
    ]
    for nombre, ws, esperado in casos:
        r = red_local.comandos_permitidos(ws, P)
        _ok(f"{nombre} -> {'acepta' if esperado else 'rechaza'}", r is esperado, f"(dio {r})")


class _Anota:
    """Hace de telemetry y de cola del bridge: anota cada llamada en vez de grabar o subir."""

    def __init__(self):
        self.llamadas = []

    def __getattr__(self, nombre):
        return lambda *a, **kw: self.llamadas.append(nombre)


COMANDOS = ['{"cmd": "set_telemetry", "mode": "off"}', '{"cmd": "entregar"}', '{"cmd": "set_entrega_auto", "on": true}',
            '{"cmd": "stop_server"}']


def test_ws_real():
    print("\n== WebSocket real de bridge_shm: los comandos segun el Origin ==")
    import bridge_shm
    import websockets
    from websockets.asyncio.client import connect

    async def mandar(uri, origin):
        async with connect(uri, origin=origin) as ws:
            for c in COMANDOS:
                await ws.send(c)
        for _ in range(150):                     # el handler termina (y ya proceso todo) al sacar al cliente
            if not bridge_shm.CLIENTS:
                break
            await asyncio.sleep(0.02)

    async def correr():
        async with websockets.serve(bridge_shm.ws_handler, "127.0.0.1", 0) as srv:
            uri = f"ws://127.0.0.1:{next(iter(srv.sockets)).getsockname()[1]}"
            await mandar(uri, "http://evil.example")
            ajena = (bridge_shm._shutdown, list(bridge_shm.telemetry.llamadas), list(bridge_shm.cola.llamadas))
            bridge_shm._shutdown = False
            await mandar(uri, f"http://127.0.0.1:{bridge_shm.HTTP_PORT}")
            propia = (bridge_shm._shutdown, list(bridge_shm.telemetry.llamadas), list(bridge_shm.cola.llamadas))
            return ajena, propia

    guardado = (bridge_shm.LOG_FILE, bridge_shm.telemetry, bridge_shm.cola, bridge_shm._shutdown)
    log_tmp = tempfile.mkdtemp(prefix="dash_log_")
    bridge_shm.LOG_FILE = os.path.join(log_tmp, "bridge.log")   # nunca el bridge.log real del alumno
    bridge_shm.telemetry, bridge_shm.cola, bridge_shm._shutdown = _Anota(), _Anota(), False
    try:
        ajena, propia = asyncio.run(correr())
    finally:
        bridge_shm.LOG_FILE, bridge_shm.telemetry, bridge_shm.cola, bridge_shm._shutdown = guardado
        shutil.rmtree(log_tmp, ignore_errors=True)
    _ok("desde un sitio ajeno: stop_server no apaga", ajena[0] is False, f"(_shutdown={ajena[0]})")
    _ok("desde un sitio ajeno: ni set_telemetry ni la bandeja reciben nada", not ajena[1] and not ajena[2], f"({ajena[1]} {ajena[2]})")
    _ok("desde la pagina del dash: stop_server apaga", propia[0] is True, f"(_shutdown={propia[0]})")
    _ok("desde la pagina del dash: set_telemetry llega", propia[1] == ["set_mode"], f"({propia[1]})")
    _ok("desde la pagina del dash: entregar y set_entrega_auto llegan", propia[2] == ["entregar_ahora", "set_auto"], f"({propia[2]})")


if __name__ == "__main__":
    import bridge
    import bridge_shm
    test_http("bridge_shm", bridge_shm._NoCacheHandler)
    test_http("bridge (UDP)", bridge._NoCacheHandler)
    test_api_sigue()
    test_compuerta()
    test_ws_real()
    print(f"\ndone. {OK} PASS · {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)
